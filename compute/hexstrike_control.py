#!/usr/bin/env python3
"""Owner-gated command adapter for the dedicated HexStrike server agent.

The native OpenClaw plugin authenticates the Telegram sender before invoking this
helper. Tasks are queued through the existing durable compute coordinator and
always target the headless server worker.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
import uuid

from client import rpc


MAX_INPUT = 32 * 1024
MAX_ARGS = 16 * 1024
JOB_ID = re.compile(r"[0-9a-f]{8,32}\Z")
TASK_FILE = re.compile(r"[0-9a-f]{32}\.txt\Z")
RUNNER = str(Path.home() / ".local/share/hexstrike-agent/hexstrike_agent.py")
HELP = """HexStrike security agent
/hexstrike TASK — queue a security task on the headless server
/hexstrike health — show the HexStrike service and installed-tool summary
/hexstrike status — show recent HexStrike jobs
/hexstrike job latest — show the latest result
/hexstrike job JOB_ID — show one result
/hexstrike cancel JOB_ID — cancel a queued or running task

Tasks run through the dedicated HexStrike agent. State the authorized target and desired assessment in the task. Results stay in the private queue and return only to this owner chat."""


def bounded(text, limit=3300):
    text = str(text).replace("\x00", "\\0")
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})\b", "[credential redacted]", text)
    raw = text.encode("utf-16-le", errors="replace")
    return text if len(raw) <= limit * 2 else raw[: (limit - 16) * 2].decode("utf-16-le", errors="ignore") + "\n… truncated"


def state_path():
    root = Path(os.environ.get("HEXSTRIKE_STATE", "~/.local/state/hexstrike-agent")).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


class Controller:
    def __init__(self, call=rpc, state=None, runner=RUNNER):
        self.call = call
        self.state = Path(state) if state is not None else state_path()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tasks = self.state / "tasks"
        self.tasks.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runner = str(runner)

    def is_hexstrike(self, job):
        spec = job.get("spec") or {}
        argv = spec.get("argv") or []
        return (spec.get("kind") == "command" and spec.get("target") == "server" and
                len(argv) == 3 and argv[0] == self.runner and argv[1] == "--task-file" and
                self.task_path(argv[2]) is not None)

    def task_path(self, value):
        try:
            path = Path(value)
            if not TASK_FILE.fullmatch(path.name):
                return None
            if path.parent.resolve() != self.tasks.resolve():
                return None
            return self.tasks / path.name
        except (OSError, RuntimeError, TypeError):
            return None

    def remember(self, job):
        path = self.state / "telegram-latest.json"
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"id": job["id"]}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def latest_id(self):
        try:
            value = json.loads((self.state / "telegram-latest.json").read_text())["id"]
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("No HexStrike task has been submitted yet.") from None
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("The latest HexStrike job record is unavailable.")
        return value

    def resolve_id(self, value):
        if not JOB_ID.fullmatch(value):
            raise ValueError("Use a HexStrike job ID or its unambiguous 8-character prefix.")
        if len(value) == 32:
            return value
        jobs = [job for job in self.call("status")["jobs"] if self.is_hexstrike(job) and job["id"].startswith(value)]
        if len(jobs) != 1:
            raise ValueError("HexStrike job prefix is missing or ambiguous; use the full job ID.")
        return jobs[0]["id"]

    def get_owned(self, job_id):
        job = self.call("get", id=job_id)
        if not self.is_hexstrike(job):
            raise ValueError("That ID is not a HexStrike job.")
        return job

    @staticmethod
    def describe(job, detail=True):
        spec = job.get("spec") or {}
        lines = [f"{job['id'][:8]} · {job['status']} · server", spec.get("title", "")]
        if detail:
            result = job.get("result") or {}
            progress = job.get("progress") or {}
            if result.get("log"):
                lines.append(result["log"][-2600:])
            elif result.get("summary"):
                lines.append(result["summary"])
            elif progress.get("log"):
                lines.append("Live output:\n" + progress["log"][-1800:])
            if job.get("error"):
                lines.append(job["error"])
        return "\n".join(line for line in lines if line)

    def health(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8888/health", timeout=7) as response:
                data = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            raise ValueError("The HexStrike API service is unavailable on the server.") from None
        categories = data.get("category_stats") or {}
        installed = data.get("total_tools_available", 0)
        total = data.get("total_tools_count", 0)
        lines = [f"HexStrike {data.get('version', 'unknown')} · {data.get('status', 'unknown')}",
                 f"Detected tools: {installed}/{total}"]
        for name in ("essential", "network", "web_security", "binary", "forensics"):
            values = categories.get(name) or {}
            if isinstance(values.get("available"), int) and isinstance(values.get("total"), int):
                lines.append(f"{name.replace('_', ' ')}: {values['available']}/{values['total']}")
        return "\n".join(lines)

    def submit(self, task):
        task = task.strip()
        if not task:
            raise ValueError("Usage: /hexstrike TASK")
        if len(task.encode("utf-8")) > MAX_ARGS:
            raise ValueError("HexStrike task is too long.")
        identifier = uuid.uuid4().hex
        path = self.tasks / (identifier + ".txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(task)
                stream.flush()
                os.fsync(stream.fileno())
            title = re.sub(r"\s+", " ", task)[:80]
            job = self.call("submit", spec={
                "kind": "command",
                "role": "compute",
                "title": "HexStrike: " + title,
                "target": "server",
                "argv": [self.runner, "--task-file", str(path)],
                "timeout_seconds": 1800,
            })
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        self.remember(job)
        return f"HexStrike task queued: {job['id']}\nUse /hexstrike job latest for progress and results."

    def handle(self, args):
        if not isinstance(args, str) or "\x00" in args or len(args.encode("utf-8")) > MAX_ARGS:
            raise ValueError("HexStrike command is invalid or too long.")
        text = args.strip()
        command, _, rest = text.partition(" ")
        command = command.lower()
        rest = rest.strip()
        if not text or command == "help":
            if rest:
                raise ValueError("/hexstrike help does not take arguments.")
            return HELP
        if command == "health":
            if rest:
                raise ValueError("/hexstrike health does not take arguments.")
            return self.health()
        if command == "status":
            if rest:
                raise ValueError("/hexstrike status does not take arguments.")
            status = self.call("status")
            node = next((node for node in status["nodes"] if node["node"] == "server"), {})
            cpu = (node.get("info") or {}).get("cpu_percent")
            header = "HexStrike server: " + ("online" if node.get("online") else "offline")
            if type(cpu) in (int, float) and math.isfinite(cpu):
                header += f" · CPU {cpu:.0f}%"
            jobs = [job for job in status["jobs"] if self.is_hexstrike(job)][:8]
            return "\n".join([header] + ([self.describe(job, detail=False) for job in jobs] or ["No recent HexStrike jobs."]))
        if command == "job":
            if not rest:
                raise ValueError("Usage: /hexstrike job latest|JOB_ID")
            job_id = self.latest_id() if rest == "latest" else self.resolve_id(rest)
            return self.describe(self.get_owned(job_id))
        if command == "cancel":
            if not rest:
                raise ValueError("Usage: /hexstrike cancel JOB_ID")
            job = self.get_owned(self.resolve_id(rest))
            path = self.task_path(job["spec"]["argv"][2])
            cancelled = self.call("cancel", id=job["id"])
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return self.describe(cancelled, detail=False)
        if command == "run":
            return self.submit(rest)
        return self.submit(text)


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
                raise ValueError("Another HexStrike command is being processed. Check again shortly.") from None
            response = {"text": bounded(Controller(state=root).handle(request["args"]))}
    except (ValueError, UnicodeError) as error:
        response = {"text": bounded(error), "isError": True}
    except (RuntimeError, OSError):
        response = {"text": "HexStrike or the private queue is unavailable. A task may already be queued; check /hexstrike status before repeating it.", "isError": True}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
