#!/usr/bin/env python3
"""Consume one private task file and run the dedicated HexStrike agent."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


TASKS = Path.home() / ".local/state/hexstrike-agent/tasks"
CODEX = Path.home() / ".local/share/compute-cluster/bin/codex"
MCP_PYTHON = Path.home() / ".local/share/hexstrike-agent/venv/bin/python"
MCP_SERVER = Path.home() / ".local/share/hexstrike-agent/source/hexstrike_mcp.py"
WORK_ROOT = Path.home() / ".local/state/hexstrike-agent/work"
NAME = re.compile(r"[0-9a-f]{32}\.txt\Z")
MAX_TASK = 16 * 1024
API_ENVIRONMENT = {
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
}


def subscription_environment(source=None):
    """Return an environment that cannot silently select API-key billing."""
    environment = dict(os.environ if source is None else source)
    for name in API_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def consume(path_value):
    supplied = Path(path_value)
    if not NAME.fullmatch(supplied.name) or supplied.parent.resolve() != TASKS.resolve():
        raise ValueError("invalid task file")
    directory = os.open(TASKS, os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = os.open(supplied.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("unsafe task file")
            raw = os.read(descriptor, MAX_TASK + 1)
            if len(raw) > MAX_TASK:
                raise ValueError("task is too large")
        finally:
            os.close(descriptor)
        os.unlink(supplied.name, dir_fd=directory)
    finally:
        os.close(directory)
    task = raw.decode("utf-8").strip()
    if not task or "\x00" in task:
        raise ValueError("task is empty or invalid")
    return task


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", required=True)
    args = parser.parse_args()
    task_name = Path(args.task_file).stem
    try:
        task = consume(args.task_file)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Could not consume HexStrike task: {error}", file=sys.stderr)
        return 2
    prompt = (
        "Handle this owner-requested cybersecurity task using the dedicated HexStrike MCP tools. "
        "Only act on targets and scope identified by the owner. Summarize concrete findings, "
        "evidence, and limitations. Do not broaden the target scope.\n\nTask:\n" + task
    )
    work = WORK_ROOT / task_name
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = work / "final.txt"
    try:
        result = subprocess.run(
            [str(CODEX), "exec", "--ignore-user-config", "--ignore-rules",
             "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
             "--color", "never", "-C", str(work), "-m", "gpt-5.6-sol",
             "-c", 'forced_login_method="chatgpt"',
             "-c", 'model_reasoning_effort="low"',
             "-c", f'mcp_servers.hexstrike.command="{MCP_PYTHON}"',
             "-c", ('mcp_servers.hexstrike.args=['
                    f'"{MCP_SERVER}","--server","http://127.0.0.1:8888",'
                    '"--timeout","300"]'),
             "-c", 'mcp_servers.hexstrike.default_tools_approval_mode="approve"',
             "--output-last-message", str(final), "-"],
            input=prompt, text=True, capture_output=True, timeout=1750,
            env=subscription_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"HexStrike agent could not finish: {type(error).__name__}")
        return 1
    try:
        response = final.read_text(encoding="utf-8").strip()
    except OSError:
        response = ""
    if response:
        print(response[-8000:])
    else:
        print((result.stdout or result.stderr or "HexStrike agent returned invalid output")[-8000:])
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
