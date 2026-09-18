#!/usr/bin/env python3
"""A single-slot, durable pull worker. Private state is deliberately outside Git."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time

from client import load_config, rpc
from github_context import (GITHUB_TOKEN_VARIABLES, github_environment,
                            github_instructions, validate_github_access)

LOG_LIMIT = 256 * 1024
PROGRESS_LIMIT = 32 * 1024
HEARTBEAT_SECONDS = 5
PATCH_LIMIT = 8 * 1024 * 1024
READ_ONLY_ROLES = {"research", "review"}
AGENT_REPORT_LIMIT = 128 * 1024
AGENT_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["completed", "blocked", "failed"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 12000},
        "tests": {"type": "string", "minLength": 1, "maxLength": 8000},
    },
    "required": ["outcome", "summary", "tests"],
    "additionalProperties": False,
}
STOP = False


def state_root():
    return Path(os.environ.get("CLUSTER_STATE", "~/.local/state/compute-cluster")).expanduser()


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def save_json(path, value):
    private_dir(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def clean_environment():
    env = os.environ.copy()
    for name in list(env):
        upper = name.upper()
        if (upper.endswith("API_KEY") or upper.startswith("GIT_")
                or upper in {"OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID",
                             "OPENAI_PROJECT_ID", "CODEX_HOME", "CODEX_THREAD_ID",
                             "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "ANTHROPIC_BASE_URL",
                             "AZURE_OPENAI_ENDPOINT", "OPENAI_ACCESS_TOKEN"}
                or upper in GITHUB_TOKEN_VARIABLES):
            env.pop(name, None)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="2",
               GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0="/dev/null",
               GIT_CONFIG_KEY_1="protocol.ext.allow", GIT_CONFIG_VALUE_1="never")
    return env


def agent_environment(config):
    env = clean_environment()
    access = validate_github_access(config)
    if access:
        env.update(github_environment(config, env))
        env["PATH"] = str(Path(access["cli"]).parent) + os.pathsep + env.get("PATH", os.defpath)
        # Git uses gh's normal credential helper; the credential stays in the
        # environment, never in command arguments or per-checkout Git config.
        env.update(GIT_CONFIG_COUNT="3",
                   GIT_CONFIG_KEY_2="credential.https://github.com.helper",
                   GIT_CONFIG_VALUE_2="!" + shlex.quote(access["cli"]) + " auth git-credential")
    return env


def agent_permissions(config, role):
    read_only = role in READ_ONLY_ROLES
    if not validate_github_access(config):
        return ["--sandbox", "read-only" if read_only else "workspace-write"]
    # Extend built-ins to retain protected Git/config paths. The network proxy
    # enforces domains; enabled network without the proxy would be unrestricted.
    filters = ("HOME", "PATH", "LANG", "LC_*", "TERM", "TMPDIR", "GH_TOKEN", "GH_HOST",
               "GH_PROMPT_DISABLED", "GH_PAGER", "GIT_CONFIG_*", "GIT_TERMINAL_PROMPT",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "CODEX_CA_CERTIFICATE")
    domains = ("github.com", "api.github.com", "raw.githubusercontent.com",
               "codeload.github.com", "objects.githubusercontent.com",
               "media.githubusercontent.com", "release-assets.githubusercontent.com")
    settings = [
        'default_permissions="cluster_github"',
        'permissions.cluster_github.extends=' + json.dumps(":read-only" if read_only else ":workspace"),
        'features.network_proxy=true',
        'permissions.cluster_github.network.enabled=true',
        'permissions.cluster_github.network.mode="full"',
        'permissions.cluster_github.network.domains={' +
        ",".join(json.dumps(name) + '="allow"' for name in domains) + '}',
        'shell_environment_policy.inherit="all"',
        'shell_environment_policy.ignore_default_excludes=true',
        'shell_environment_policy.filters={' +
        ",".join(json.dumps(name) + '="include"' for name in filters) + '}',
    ]
    return [item for setting in settings for item in ("-c", setting)]


def git(repo, *args, data=None, timeout=45):
    return subprocess.run(["git", "-C", str(repo), *args], input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=clean_environment(), timeout=timeout, check=True).stdout


def safe_path(name):
    path = Path(name)
    return not path.is_absolute() and bool(path.parts) and all(p not in {"..", ".git"} for p in path.parts)


def publishable(name):
    """A conservative export boundary, separate from .gitignore."""
    if not safe_path(name):
        return False
    parts = Path(name).parts
    hidden = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
              ".venv", "venv", ".cache", ".codex", ".openclaw", "credentials", "secrets",
              "logs", "dist", "build", ".ssh", ".aws", ".kube", ".gnupg"}
    for part in parts:
        low = part.lower()
        if (low in hidden or low.startswith(".env") or low.startswith("id_rsa")
                or low.startswith("id_ed25519") or low.endswith((".pem", ".key", ".p12", ".pfx", ".log"))
                or low in {"auth.json", "auth-profiles.json", "credentials.json", "token.json",
                           "tokens.json", "config.local.json"}):
            return False
    return True


def source_paths(repo):
    raw = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(set(os.fsdecode(p) for p in raw.split(b"\0") if p))


def snapshot(repo):
    result = {}
    for name in source_paths(repo):
        if not safe_path(name) or any(p in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
                                      for p in Path(name).parts):
            continue
        path = repo / name
        try:
            path.parent.resolve().relative_to(repo.resolve())
        except (ValueError, RuntimeError):
            result[name] = "unsafe-parent"
            continue
        if path.is_symlink():
            result[name] = "link:" + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            result[name] = (digest.hexdigest(), path.stat().st_mode & 0o111)
        else:
            result[name] = None
    return result


def check_symlinks(repo):
    root = repo.resolve()
    for name in source_paths(repo):
        path = repo / name
        if not safe_path(name):
            raise ValueError("Unsafe source path")
        try:
            path.resolve().relative_to(root)
        except (ValueError, RuntimeError):
            raise ValueError("Project contains a symlink outside its workspace") from None


def collect_patch(repo, revision="HEAD"):
    # Reset the index so excluded paths can never leak from an inherited index.
    # Anchor the diff to the registered commit even if a command moved local HEAD.
    git(repo, "read-tree", revision)
    names = []
    for name in source_paths(repo):
        path = repo / name
        if publishable(name) and not path.is_symlink():
            try:
                path.resolve().relative_to(repo.resolve())
            except (ValueError, RuntimeError):
                continue
            names.append(name)
    if names:
        git(repo, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
            data=b"\0".join(os.fsencode(name) for name in names) + b"\0")
    patch = git(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-renames", revision, "--")
    if len(patch) > PATCH_LIMIT:
        raise ValueError("Result patch exceeds 8 MiB; retain large artifacts on the worker")
    return patch


def redact(text):
    # JSON accepts NUL but the coordinator deliberately refuses it.
    text = text.replace("\x00", "\\0")
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "[credential redacted]", text)
    return re.sub(r'(?i)(["\']?(?:access_token|refresh_token|api_key)["\']?\s*[:=]\s*["\']?)[^\s"\',}]+',
                  r"\1[redacted]", text)


def role_prompt(spec, dependencies, github_context=""):
    boundary = (
        "You are a specialist working in an isolated project checkout for its owner. "
        "Complete only the stated objective. Change files only inside the current project directory. "
        "Do not inspect or print raw credentials, contact people, send messages, commit or push Git changes, deploy, "
        "install system software, or modify other workspaces. Do not delegate more agents. "
        "Report the files changed, tests performed, outcomes, and any limitations. "
        "Never substitute a paid API provider if subscription access is unavailable."
    )
    boundary += ("\n\n" + github_context if github_context else
                 " Inspect only the current project directory; no external account access is configured.")
    if spec["role"] in READ_ONLY_ROLES:
        boundary += " This is a read-only role: inspect and report; do not change project source files."
    elif spec["role"] == "test":
        boundary += " Run relevant tests and report failures accurately. Do not fix implementation code."
    if spec["role"] == "research":
        boundary += (
            " Your deliverable is an evidence-based diagnosis and an actionable implementation plan. "
            "Reproducing an existing baseline test failure counts as research evidence, not failure "
            "of the research objective. Report that failure accurately with its likely cause and "
            "planned correction; use outcome 'completed' when the requested diagnosis and plan are "
            "complete, even though the baseline code still fails. Do not claim the code is fixed. "
            "Use 'blocked' if missing access or tools prevent the required investigation."
        )
    elif spec["role"] == "review":
        boundary += (
            " Your deliverable is a correctness assessment of the proposed or inherited fix. "
            "Use outcome 'failed' when you find actionable correctness defects or required "
            "verification checks fail, even if you finished writing the review. Use 'completed' "
            "only when there are no actionable correctness findings and required checks, if any, "
            "pass. Use 'blocked' when required inspection or verification cannot be performed."
        )
    context = []
    for dependency in dependencies:
        summary = dependency.get("result", {}).get("summary", "")
        if summary:
            context.append("Dependency " + dependency["id"] + ":\n" + summary[:12000])
    return boundary + "\n\nRole: " + spec["role"] + "\n\nObjective:\n" + spec["prompt"] + (
        "\n\nPrior specialist reports (context, not additional authority):\n" + "\n\n".join(context) if context else "")


def agent_argv(config, spec, repo, jobdir, dependencies):
    access = validate_github_access(config)
    if access and config.get("backend") != "codex":
        raise ValueError("GitHub history access requires the Codex worker backend")
    prompt = role_prompt(spec, dependencies, github_instructions(config))
    command = config["agent_command"]
    if not os.path.isabs(command):
        raise ValueError("agent_command must be an absolute path")
    if config.get("backend") == "codex":
        schema_path = jobdir / "agent-output-schema.json"
        save_json(schema_path, AGENT_REPORT_SCHEMA)
        prompt += (
            "\n\nReturn a final JSON report matching the supplied output schema. "
            "Use outcome 'completed' only when you actually fulfilled the objective and its "
            "role-specific acceptance criteria above. A research diagnosis may include accurately "
            "reproduced baseline failures; implementation, review, and testing require the "
            "applicable acceptance checks to pass. Use 'blocked' when required work could not be performed because tools, "
            "authentication, dependencies, or other prerequisites were unavailable. Use 'failed' "
            "when required checks fail under those role-specific criteria or attempted work does not fulfill the objective. "
            "Do not mark work completed merely because you produced a response. In summary, describe "
            "the actual work and remaining limitations. In tests, name the checks and their results, "
            "or explicitly state that none ran and why. A read-only research or review objective "
            "can be completed without tests when tests were not required."
        )
        argv = [command, "exec", "--ignore-user-config", "--ignore-rules", *agent_permissions(config, spec["role"]),
                "-c", 'approval_policy="never"', "-c", 'model_reasoning_effort="low"',
                "--ephemeral", "--json", "--model", config.get("model", "gpt-5.6-sol"),
                "--cd", str(repo), "--output-schema", str(schema_path),
                "--output-last-message", str(jobdir / "agent-final.txt"), "-"]
        return argv, prompt.encode()
    if config.get("backend") == "openclaw":
        agent_config = config.get("agent_config")
        if not agent_config or not os.path.isabs(agent_config):
            raise ValueError("OpenClaw requires an explicit private agent_config")
        promptfile = jobdir / "prompt.txt"
        promptfile.write_text(prompt)
        promptfile.chmod(0o600)
        model = config.get("model", "gpt-5.6-sol")
        if not model.startswith("openai/"):
            model = "openai/" + model
        argv = [command, "agent", "exec", "--config", agent_config, "--cwd", str(repo),
                "--message-file", str(promptfile), "--model", model, "--thinking", "low",
                "--timeout", str(spec["timeout_seconds"]), "--json"]
        return argv, None
    raise ValueError("No supported subscription agent backend configured")


def codex_report(path):
    """Validate structured task outcome separately from CLI process success."""
    if not path.is_file() or path.is_symlink():
        raise ValueError("final report is missing or not a regular file")
    with path.open("rb") as stream:
        raw = stream.read(AGENT_REPORT_LIMIT + 1)
    if len(raw) > AGENT_REPORT_LIMIT:
        raise ValueError("final report exceeds its size limit")
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("final report is not valid JSON") from None
    if not isinstance(report, dict) or set(report) != {"outcome", "summary", "tests"}:
        raise ValueError("final report does not match the required fields")
    if report["outcome"] not in ("completed", "blocked", "failed"):
        raise ValueError("final report has an invalid outcome")
    for key, limit in (("summary", 12000), ("tests", 8000)):
        value = report[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("final report has an invalid " + key)
    return report


def apply_codex_report(result, path):
    prior_failure = result["summary"] if result["exit_code"] != 0 else ""
    try:
        report = codex_report(path)
        readable = ("Agent outcome: " + report["outcome"] + ".\n" + redact(report["summary"])
                    + "\n\nTests: " + redact(report["tests"]))
        if report["outcome"] != "completed" and result["exit_code"] == 0:
            result["exit_code"] = 1
    except (OSError, ValueError) as error:
        readable = "Agent did not provide a valid structured completion report: " + str(error) + "."
        if result["exit_code"] == 0:
            result["exit_code"] = 1
    result["summary"] = prior_failure + "\n" + readable if prior_failure else readable


def process_start(pid):
    try:
        # /proc comm may contain spaces or closing parentheses.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None


def terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # Kill descendants even if the group leader exits before they do.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class TelemetrySampler:
    """Read Linux counters once per heartbeat; percentages use sample deltas."""
    def __init__(self, disk_path, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup"), clock=time.monotonic):
        self.disk_path = disk_path
        self.proc = Path(proc_root)
        self.cgroups = Path(cgroup_root)
        self.clock = clock
        self.previous_cpu = None
        self.previous_worker_cpu = None

    def sample(self):
        info = {"cpu_count": os.cpu_count() or 1}
        try:
            values = [int(v) for v in (self.proc / "stat").read_text().splitlines()[0].split()[1:9]]
            if len(values) >= 5:
                counters = (sum(values), values[3] + values[4])
                if self.previous_cpu:
                    total = counters[0] - self.previous_cpu[0]
                    idle = counters[1] - self.previous_cpu[1]
                    if total > 0 and idle >= 0:
                        info["cpu_percent"] = round(max(0, min(100, 100 * (total - idle) / total)), 2)
                self.previous_cpu = counters
        except (OSError, ValueError, IndexError):
            pass
        try:
            memory = {}
            for line in (self.proc / "meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                memory[key] = int(value.split()[0])
            total = memory["MemTotal"] // 1024
            available = memory.get("MemAvailable", sum(memory.get(k, 0) for k in ("MemFree", "Buffers", "Cached")))
            if total > 0:
                info["memory_total_mb"] = total
                info["memory_used_mb"] = round(max(0, min(total, (memory["MemTotal"] - available) / 1024)), 2)
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            load = float((self.proc / "loadavg").read_text().split()[0])
            if math.isfinite(load) and 0 <= load <= 1000000:
                info["load_1"] = load
        except (OSError, ValueError, IndexError):
            pass
        try:
            disk = shutil.disk_usage(self.disk_path)
            info["disk_total_gb"] = round(disk.total / 1024 ** 3, 3)
            info["disk_used_gb"] = round(disk.used / 1024 ** 3, 3)
        except OSError:
            pass
        try:
            # Cgroup v2 accounts for the worker service and all child processes.
            line = next(line for line in (self.proc / "self/cgroup").read_text().splitlines() if line.startswith("0::"))
            relative = Path(line[3:].lstrip("/"))
            if ".." in relative.parts:
                return info
            group = self.cgroups / relative
            group.resolve().relative_to(self.cgroups.resolve())
            try:
                usage = int((group / "memory.current").read_text())
                if usage >= 0:
                    info["worker_memory_mb"] = round(usage / 1024 ** 2, 2)
            except (OSError, ValueError):
                pass
            counters = dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())
            usage = int(counters["usage_usec"])
            now = self.clock()
            if self.previous_worker_cpu:
                previous_usage, previous_time = self.previous_worker_cpu
                if now > previous_time and usage >= previous_usage:
                    percent = (usage - previous_usage) / 1000000 / (now - previous_time) * 100
                    info["worker_cpu_percent"] = round(min(info["cpu_count"] * 100, percent), 2)
            self.previous_worker_cpu = (usage, now)
        except (OSError, ValueError, KeyError, StopIteration, RuntimeError):
            pass
        return info


def live_progress(log):
    # Redact before taking the UTF-8 tail, so truncation cannot split a credential
    # prefix and accidentally defeat the redaction pattern.
    text = redact(bytes(log).decode("utf-8", errors="replace"))
    text = text.encode("utf-8")[-PROGRESS_LIMIT:].decode("utf-8", errors="ignore")
    return {"log": text, "updated_at": time.time()}


class Worker:
    def __init__(self, config=None):
        self.config = config or load_config()
        self.node = self.config["node_id"]
        self.root = private_dir(state_root())
        self.pending = private_dir(self.root / "pending")
        self.active_path = self.root / "worker-active.json"
        self.capabilities = ["command"]
        if self.config.get("backend") in {"codex", "openclaw"} and self.config.get("agent_command"):
            self.capabilities.append("agent")
        self.last_contact = time.monotonic()
        self.telemetry = TelemetrySampler(self.root)

    def heartbeat(self, claim=None, progress=None):
        fields = {"info": self.telemetry.sample()}
        if progress is not None:
            fields["progress"] = progress
        response = rpc("heartbeat", node=self.node, capabilities=self.capabilities,
                       active_job=claim["id"] if claim else None,
                       lease_token=claim["lease_token"] if claim else None,
                       **fields)
        self.last_contact = time.monotonic()
        return response

    def persist_result(self, claim, result):
        record = {"node": self.node, "id": claim["id"], "lease_token": claim["lease_token"], "result": result}
        save_json(self.root / "work" / claim["id"] / "result.json", result)
        save_json(self.pending / (claim["id"] + ".json"), record)
        self.active_path.unlink(missing_ok=True)

    def flush_pending(self):
        for path in sorted(self.pending.glob("*.json")):
            record = json.loads(path.read_text())
            try:
                rpc("complete", **record)
            except RuntimeError:
                # A lost acknowledgement or expired lease must not rerun the job.
                try:
                    current = rpc("get", id=record["id"])
                except RuntimeError:
                    return False
                if current.get("status") not in {"succeeded", "failed", "cancelled", "interrupted", "blocked"}:
                    return False
            path.unlink(missing_ok=True)
        return True

    def recover(self):
        if not self.active_path.exists():
            return
        active = json.loads(self.active_path.read_text())
        claim = active["claim"]
        pending = self.pending / (claim["id"] + ".json")
        if pending.exists():
            self.active_path.unlink(missing_ok=True)
            return
        pid = active.get("pid")
        if pid and active.get("process_start") and process_start(pid) == active["process_start"]:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.persist_result(claim, {"exit_code": 125, "summary": "Worker restarted; prior job was interrupted and was not rerun.",
                                   "duration_seconds": 0, "log": "", "patch_b64": ""})

    def prepare(self, claim):
        spec = claim["spec"]
        jobdir = private_dir(self.root / "work" / claim["id"])
        repo = jobdir / "repo"
        if repo.exists():
            raise RuntimeError("Existing job directory retained; refusing to execute the same job again")
        if spec.get("source_id"):
            encoded = rpc("project_get", source_id=spec["source_id"])["bundle_b64"]
            bundle = base64.b64decode(encoded, validate=True)
            if len(bundle) > 48 * 1024 * 1024 or hashlib.sha256(bundle).hexdigest() != spec["source_id"]:
                raise ValueError("Project bundle size or checksum is invalid")
            bundle_path = jobdir / "source.bundle"
            bundle_path.write_bytes(bundle)
            bundle_path.chmod(0o600)
            self.check_claim(claim)
            subprocess.run(["git", "clone", "--no-checkout", "--no-local", "--config", "core.hooksPath=/dev/null",
                            str(bundle_path), str(repo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=clean_environment(), timeout=45, check=True)
            self.check_claim(claim)
            revision = spec["revision"]
            resolved = git(repo, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
            if resolved != revision:
                raise ValueError("Project revision must be the exact immutable commit")
            git(repo, "checkout", "--detach", revision)
            # There is no remote to accidentally push to from a job checkout.
            git(repo, "remote", "remove", "origin")
            check_symlinks(repo)
            inherited = spec.get("inherit_from")
            if inherited:
                parent = next(d for d in claim.get("dependencies", []) if d["id"] == inherited)
                patch = base64.b64decode(parent.get("result", {}).get("patch_b64", ""), validate=True)
                if len(patch) > PATCH_LIMIT:
                    raise ValueError("Inherited patch exceeds size limit")
                if patch:
                    git(repo, "apply", "--index", "--binary", "--whitespace=nowarn", "-", data=patch)
                    check_symlinks(repo)
            baseline = snapshot(repo)
        else:
            private_dir(repo)
            baseline = None
        self.check_claim(claim)
        return jobdir, repo, baseline

    def check_claim(self, claim):
        if STOP:
            raise RuntimeError("Worker stopping")
        state = self.heartbeat(claim)
        if not state.get("lease_valid", False):
            raise RuntimeError("Job lease no longer valid")
        if state.get("cancel"):
            raise RuntimeError("Job cancelled")

    def run_process(self, claim, argv, repo, jobdir, stdin=None, env=None):
        log = bytearray()
        start = time.monotonic()
        deadline = start + claim["spec"]["timeout_seconds"]
        next_heartbeat = start
        lost_limit = max(15, min(float(self.config.get("lease_seconds", 90)) - 15, 75))
        reason = ""
        process = subprocess.Popen(argv, cwd=repo, env=env if env is not None else clean_environment(),
                                   stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        selector = selectors.DefaultSelector()
        offset = 0
        ended_at = None
        try:
            save_json(self.active_path, {"claim": claim, "pid": process.pid, "process_start": process_start(process.pid)})
            # Agent prompts are bounded; nonblocking writes avoid pipe deadlocks.
            if stdin is not None:
                input_path = jobdir / "stdin.txt"
                input_path.write_bytes(stdin)
                input_path.chmod(0o600)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            if stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            while True:
                now = time.monotonic()
                if STOP:
                    reason = "Worker stopped; job interrupted"
                elif now >= deadline:
                    reason = "Job exceeded its time limit"
                elif now - self.last_contact >= lost_limit:
                    reason = "Coordinator unavailable; stopped before job lease expiry"
                if not reason and now >= next_heartbeat:
                    next_heartbeat = now + HEARTBEAT_SECONDS
                    try:
                        heartbeat = self.heartbeat(claim, progress=live_progress(log))
                        if not heartbeat.get("lease_valid", False):
                            reason = "Job lease invalidated"
                        elif heartbeat.get("cancel"):
                            reason = "Job cancelled"
                    except RuntimeError:
                        pass
                if reason:
                    terminate_group(process)
                for key, _ in selector.select(0.25):
                    if key.data == "stdin":
                        try:
                            offset += os.write(key.fileobj.fileno(), stdin[offset:offset + 65536])
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError):
                            offset = len(stdin)
                        if offset == len(stdin):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        try:
                            data = os.read(key.fileobj.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if data:
                            log.extend(data)
                            if len(log) > LOG_LIMIT:
                                del log[:-LOG_LIMIT]
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                if process.poll() is not None:
                    ended_at = ended_at or time.monotonic()
                    # Background descendants must not outlive their assigned job.
                    terminate_group(process)
                    if not selector.get_map() or time.monotonic() - ended_at > 1:
                        break
            code = process.wait()
            if reason:
                code = 130 if "cancelled" in reason else 124 if "time limit" in reason else 125
            text = redact(log.decode("utf-8", errors="replace"))
            if reason:
                text += "\n" + reason + "\n"
            (jobdir / "output.log").write_text(text)
            (jobdir / "output.log").chmod(0o600)
            return code, text, reason
        finally:
            selector.close()
            terminate_group(process)

    def execute(self, claim):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", claim["id"]):
            raise ValueError("Invalid job identifier")
        start = time.monotonic()
        result = {"exit_code": 1, "summary": "", "duration_seconds": 0, "log": "", "patch_b64": ""}
        save_json(self.active_path, {"claim": claim})
        try:
            jobdir, repo, baseline = self.prepare(claim)
            spec = claim["spec"]
            if spec["kind"] == "command":
                argv, stdin = spec["argv"], None
                env = None
            else:
                argv, stdin = agent_argv(self.config, spec, repo, jobdir, claim.get("dependencies", []))
                env = agent_environment(self.config)
            code, log, reason = self.run_process(claim, argv, repo, jobdir, stdin, env=env)
            result.update(exit_code=code, log=log, summary=reason or ("Command completed successfully." if code == 0 else f"Process exited with code {code}."))
            if spec["kind"] == "agent":
                final = jobdir / "agent-final.txt"
                if self.config.get("backend") == "codex":
                    apply_codex_report(result, final)
                elif self.config.get("backend") == "openclaw":
                    try:
                        envelope = json.loads(log)
                        payloads = envelope.get("result", {}).get("payloads", envelope.get("payloads", []))
                        messages = [p.get("text", "") for p in payloads if isinstance(p, dict)]
                        if messages:
                            final_text = redact("\n".join(messages)[:24000])
                            result["summary"] = final_text if code == 0 else result["summary"] + "\n" + final_text
                        if envelope.get("ok") is False or envelope.get("status") in {"error", "failed"}:
                            result["exit_code"] = 1
                            result["summary"] = "Agent reported a failure. " + result["summary"]
                    except (ValueError, AttributeError, TypeError):
                        pass
            if baseline is not None:
                after = snapshot(repo)
                if spec.get("role") in READ_ONLY_ROLES and after != baseline:
                    result["exit_code"] = 1
                    result["summary"] = "Read-only role changed source files. " + result["summary"]
                result["patch_b64"] = base64.b64encode(collect_patch(repo, spec["revision"])).decode()
        except Exception as error:
            detail = str(error)
            if isinstance(error, subprocess.CalledProcessError):
                detail = error.stderr.decode(errors="replace")[-4000:]
            result["exit_code"] = 125
            result["summary"] = "Job could not finish: " + redact(detail)[:4000]
            result["log"] += "\n" + result["summary"]
        result["duration_seconds"] = round(time.monotonic() - start, 3)
        result["summary"] = result["summary"].encode("utf-8")[:60000].decode("utf-8", errors="ignore")
        self.persist_result(claim, result)
        self.flush_pending()
        return result

    def run(self, once=False):
        self.recover()
        while not STOP:
            try:
                if self.flush_pending():
                    self.heartbeat()
                    claim = rpc("claim", node=self.node, capabilities=self.capabilities)
                    if claim:
                        self.execute(claim)
                        if once:
                            return
                        continue
            except (RuntimeError, OSError, ValueError) as error:
                # Operational diagnostics only. Never print job input/output.
                print("Worker waiting after coordinator or state error: " + redact(str(error))[:300], file=sys.stderr, flush=True)
            if once:
                return
            end = time.monotonic() + max(1, float(self.config.get("poll_seconds", 5)))
            while not STOP and time.monotonic() < end:
                time.sleep(0.25)


def stop_handler(_signum, _frame):
    global STOP
    STOP = True


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Claim at most one job, then exit")
    args = parser.parse_args()
    worker = Worker()
    lock = open(worker.root / "worker.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another worker already owns this node")
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    worker.run(once=args.once)


if __name__ == "__main__":
    main()
