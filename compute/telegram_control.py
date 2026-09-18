#!/usr/bin/env python3
"""Bounded JSON-stdin command adapter for the existing owner-only Telegram bot.

This helper does not connect to Telegram, read bot tokens, or invoke a model.
Its caller is the native OpenClaw plugin, which verifies the requesting owner.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from client import rpc


MAX_INPUT = 32 * 1024
MAX_ARGS = 16 * 1024
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "blocked"}
NODES = {"any", "server", "workstation"}
ID = re.compile(r"[0-9a-f]{8,32}\Z")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
HELP = """Cluster control
/cluster status — workers and recent jobs
/cluster projects — available project snapshots
/cluster run NODE COMMAND — queue an explicit shell command
/cluster ask PROJECT TASK — research → build → review + test
/cluster agent NODE PROJECT TASK — queue one coding agent
/cluster job ID — progress, result, and patch availability
/cluster job latest — your last submitted job or team
/cluster cancel ID — stop a queued/running job
/cluster retry ID — explicitly create another attempt

NODE is server, workstation, or any. IDs may use an unambiguous 8-character prefix. Register projects in the desktop app first. Jobs use isolated copies; merging patches is manual. Agent work uses the existing subscription with no API fallback."""


def bounded(text, limit=3300):
    text = str(text).replace("\x00", "\\0")
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})\b", "[credential redacted]", text)
    text = re.sub(r'(?i)(["\']?(?:access_token|refresh_token|api_key)["\']?\s*[:=]\s*["\']?)[^\s"\',}]+', r"\1[redacted]", text)
    raw = text.encode("utf-16-le", errors="replace")
    return text if len(raw) <= limit * 2 else raw[: (limit - 25) * 2].decode("utf-16-le", errors="ignore") + "\n… More in the desktop app."


def state_path():
    root = Path(os.environ.get("CLUSTER_STATE", "~/.local/state/compute-cluster")).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


class Controller:
    def __init__(self, call=rpc, state=None, clock=time.monotonic, sleep=time.sleep, wait_seconds=6):
        self.call = call
        self.state = Path(state) if state is not None else state_path()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock, self.sleep = clock, sleep
        self.wait_seconds = max(0, min(8, wait_seconds))

    def remember(self, jobs):
        value = {"jobs": [{"id": job["id"], "role": job["spec"]["role"]} for job in jobs]}
        path = self.state / "telegram-latest.json"
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def source(self, name):
        if not PROJECT.fullmatch(name):
            raise ValueError("Invalid project name. Use /cluster projects.")
        projects = self.call("projects")
        item = next((item for item in projects if item["project"] == name), None)
        if item is None:
            raise ValueError("Project not registered. Use /cluster projects, or add it in the desktop app.")
        return {key: item[key] for key in ("project", "source_id", "revision")}

    @staticmethod
    def node(value):
        if value not in NODES:
            raise ValueError("NODE must be server, workstation, or any; no alternate node was selected.")
        return value

    def resolve_id(self, value):
        if not ID.fullmatch(value):
            raise ValueError("Use a job ID or its unambiguous 8-character prefix.")
        if len(value) == 32:
            return value
        matches = [job["id"] for job in self.call("status")["jobs"] if job["id"].startswith(value)]
        if len(matches) != 1:
            raise ValueError("Job prefix is missing or ambiguous; use the full job ID.")
        return matches[0]

    @staticmethod
    def describe(job, detail=True):
        spec = job.get("spec", {})
        lines = [f"{job['id'][:8]} · {job['status']} · {spec.get('role', 'job')} · {job.get('node') or spec.get('target', 'any')}"]
        if detail:
            lines.append(spec.get("title", ""))
            result = job.get("result") or {}
            if result:
                lines.append(result.get("summary", ""))
                if spec.get("kind") == "command" and result.get("log"):
                    lines.append("Output:\n" + result["log"][-1400:])
                if result.get("patch_b64"):
                    lines.append("Patch available in the desktop app; original source is unchanged.")
            elif (job.get("progress") or {}).get("log"):
                lines.append("Live output:\n" + job["progress"]["log"][-1000:])
            if job.get("error"):
                lines.append(job["error"])
        return "\n".join(line for line in lines if line)

    def show_latest(self):
        try:
            data = json.loads((self.state / "telegram-latest.json").read_text())
            ids = [row["id"] for row in data["jobs"]]
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("No job has been submitted from this bot yet. Use /cluster status for all jobs.") from None
        if not ids or len(ids) > 4 or any(not re.fullmatch(r"[0-9a-f]{32}", item) for item in ids):
            raise ValueError("The last-job record is unavailable; use /cluster status.")
        jobs = [self.call("get", id=job_id) for job_id in ids]
        if len(jobs) == 1:
            return self.describe(jobs[0])
        lines = ["Last specialist team:"] + [self.describe(job, detail=False) for job in jobs]
        build = next((job for job in jobs if job["spec"]["role"] == "build"), None)
        if build and build.get("result"):
            lines.append("\nBuild report:\n" + build["result"].get("summary", "")[:1600])
        lines.append("Use /cluster job ID to inspect one specialist.")
        return "\n".join(lines)

    def submit(self, spec, wait=False):
        job = self.call("submit", spec=spec)
        self.remember([job])
        if wait and self.wait_seconds:
            deadline = self.clock() + self.wait_seconds
            while self.clock() < deadline:
                current = self.call("get", id=job["id"])
                if current["status"] in TERMINAL:
                    return self.describe(current)
                self.sleep(min(0.5, max(0, deadline - self.clock())))
        return f"Queued {job['id']} on {spec['target']}.\nUse /cluster job latest for progress."

    def team(self, project, objective):
        source = self.source(project)
        common = dict(kind="agent", timeout_seconds=1800, **source)
        jobs = []
        instructions = [
            ("research", "workstation", "Inspect the project and propose a focused implementation and tests. Do not edit source."),
            ("build", "server", "Implement the objective using the research report. Run focused tests."),
            ("review", "workstation", "Review the inherited implementation for correctness and regressions. Do not edit source. Report actionable findings."),
            ("test", "server", "Test the inherited implementation using relevant existing tests and focused behavioral checks. Do not modify source. Report evidence and failures."),
        ]
        try:
            for role, node, instruction in instructions:
                parents = [] if role == "research" else [jobs[0]["id"]] if role == "build" else [jobs[1]["id"]]
                inherit = jobs[1]["id"] if role in {"review", "test"} else None
                spec = dict(common, role=role, target=node, title=role.title() + ": " + objective[:80],
                            prompt=instruction + "\n\nObjective:\n" + objective, depends_on=parents, inherit_from=inherit)
                jobs.append(self.call("submit", spec=spec))
                # Persist each accepted ID; never recreate a partially submitted team.
                self.remember(jobs)
        except (RuntimeError, OSError):
            known = "\n".join(f"{job['spec']['role']}: {job['id']}" for job in jobs) or "No IDs were acknowledged."
            return "Team submission was interrupted. Do not resend blindly; inspect /cluster status first.\n" + known
        return ("Team queued: research → build → review + test.\n" +
                "\n".join(f"{job['spec']['role']}: {job['id']}" for job in jobs) +
                "\nUse /cluster job latest. Changes remain isolated until you apply a patch.")

    def handle(self, args):
        if not isinstance(args, str) or "\x00" in args or len(args.encode("utf-8")) > MAX_ARGS:
            raise ValueError("Command text is invalid or too long.")
        parts = args.strip().split(None, 1)
        command = parts[0].lower() if parts else "help"
        rest = parts[1] if len(parts) > 1 else ""
        if command in {"help", "status", "projects"} and rest:
            raise ValueError(f"/cluster {command} does not take arguments.")
        if command == "help":
            return HELP
        if command == "status":
            status = self.call("status")
            lines = ["Cluster status"]
            for node in status["nodes"]:
                info = node.get("info", {})
                cpu = info.get("cpu_percent")
                extra = f" · CPU {cpu:.0f}%" if type(cpu) in (int, float) and math.isfinite(cpu) else ""
                lines.append(f"{node['node']}: {'online' if node['online'] else 'offline'}{extra}" +
                             (f" · job {node['active_job'][:8]}" if node.get("active_job") else ""))
            lines.extend(self.describe(job, detail=False) for job in status["jobs"][:8])
            return "\n".join(lines)
        if command == "projects":
            items = self.call("projects")
            return "Project snapshots:\n" + ("\n".join(f"{row['project']} · {row['revision'][:10]}" for row in items) or "None yet. Add a clean Git project in the desktop app.")
        if command == "job":
            if rest == "latest":
                return self.show_latest()
            return self.describe(self.call("get", id=self.resolve_id(rest)))
        if command in {"cancel", "retry"}:
            job = self.call(command, id=self.resolve_id(rest))
            if command == "retry":
                self.remember([job])
            return self.describe(job, detail=False) + ("\nNew attempt: " + job["id"] if command == "retry" else "")
        if command == "run":
            fields = rest.split(None, 1)
            if len(fields) != 2:
                raise ValueError("Usage: /cluster run NODE COMMAND")
            node, script = self.node(fields[0]), fields[1]
            return self.submit(dict(kind="command", role="compute", title="Phone command: " + script[:80],
                                    target=node, argv=["/bin/bash", "-lc", script], timeout_seconds=1800), wait=True)
        if command == "ask":
            fields = rest.split(None, 1)
            if len(fields) != 2:
                raise ValueError("Usage: /cluster ask PROJECT TASK")
            return self.team(fields[0], fields[1])
        if command == "agent":
            fields = rest.split(None, 2)
            if len(fields) != 3:
                raise ValueError("Usage: /cluster agent NODE PROJECT TASK")
            node, project, objective = self.node(fields[0]), fields[1], fields[2]
            return self.submit(dict(kind="agent", role="build", title="Phone task: " + objective[:80],
                                    target=node, prompt=objective, timeout_seconds=1800, **self.source(project)))
        raise ValueError("Unknown command. Use /cluster help. Nothing was queued.")


def main():
    os.umask(0o077)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("Request is too large.")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"args"}:
            raise ValueError("Expected only the command args field.")
        root = state_path()
        with (root / "telegram-control.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another cluster command is being processed. Check again shortly.") from None
            text = Controller(state=root).handle(request["args"])
        response = {"text": bounded(text)}
    except (ValueError, UnicodeError) as error:
        response = {"text": bounded(str(error)), "isError": True}
    except (RuntimeError, OSError):
        response = {"text": "Cluster is unavailable or the request could not finish. A submitted job may still exist; check /cluster status before repeating it.", "isError": True}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
