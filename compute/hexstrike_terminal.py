#!/usr/bin/env python3
"""Run one HexStrike agent task directly from a terminal or VS Code terminal."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


MAX_TASK = 16 * 1024
DEFAULT_HOME = Path.home() / ".local/share/hexstrike-agent"
API_ENVIRONMENT = {
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
}


def agent_home() -> Path:
    return Path(os.environ.get("HEXSTRIKE_AGENT_HOME", DEFAULT_HOME)).expanduser()


def codex_binary() -> Path:
    configured = os.environ.get("CODEX_BIN")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(found) if (found := shutil.which("codex")) else None,
        Path.home() / ".local/share/compute-cluster/bin/codex",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("Codex CLI was not found. Set CODEX_BIN to its executable path.")


def subscription_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in API_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def health() -> int:
    url = os.environ.get("HEXSTRIKE_URL", "http://127.0.0.1:8888").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/health", timeout=7) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"HexStrike API is unavailable: {error}", file=sys.stderr)
        return 1
    print(f"HexStrike {data.get('version', 'unknown')} · {data.get('status', 'unknown')}")
    print(f"Detected tools: {data.get('total_tools_available', 0)}/{data.get('total_tools_count', 0)}")
    for name, values in (data.get("category_stats") or {}).items():
        if isinstance(values, dict) and "available" in values and "total" in values:
            print(f"{name.replace('_', ' ')}: {values['available']}/{values['total']}")
    return 0


def read_task(arguments: list[str]) -> str:
    if arguments:
        task = " ".join(arguments)
    elif sys.stdin.isatty():
        task = input("HexStrike task (include the authorized target and scope): ")
    else:
        task = sys.stdin.read(MAX_TASK + 1)
    task = task.strip()
    if not task or "\x00" in task:
        raise ValueError("Task is empty or invalid.")
    if len(task.encode("utf-8")) > MAX_TASK:
        raise ValueError("Task exceeds 16 KiB.")
    return task


def run(task: str) -> int:
    home = agent_home()
    mcp_python = home / "venv/bin/python"
    mcp_server = home / "source/hexstrike_mcp.py"
    if not mcp_python.is_file() or not mcp_server.is_file():
        raise RuntimeError(f"HexStrike MCP client is incomplete under {home}.")
    work = Path.home() / ".local/state/hexstrike-agent/terminal"
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    url = os.environ.get("HEXSTRIKE_URL", "http://127.0.0.1:8888").rstrip("/")
    model = os.environ.get("HEXSTRIKE_MODEL", "gpt-5.6-sol")
    prompt = (
        "Handle this owner-requested cybersecurity task using the dedicated HexStrike MCP tools. "
        "Only act on targets and scope identified by the owner. Summarize concrete findings, "
        "evidence, and limitations. Do not broaden the target scope.\n\nTask:\n" + task
    )
    command = [
        str(codex_binary()), "exec", "--ignore-user-config", "--ignore-rules",
        "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
        "--color", "never", "-C", str(work), "-m", model,
        "-c", 'forced_login_method="chatgpt"',
        "-c", 'model_reasoning_effort="low"',
        "-c", f"mcp_servers.hexstrike.command={json.dumps(str(mcp_python))}",
        "-c", "mcp_servers.hexstrike.args=" + json.dumps(
            [str(mcp_server), "--server", url, "--timeout", "300"]
        ),
        "-c", 'mcp_servers.hexstrike.default_tools_approval_mode="approve"',
        "-",
    ]
    return subprocess.run(
        command,
        input=prompt,
        text=True,
        env=subscription_environment(),
        check=False,
    ).returncode


def main() -> int:
    if sys.argv[1:] in (["--help"], ["-h"]):
        print("Usage: hexstrike-agent [--health] [TASK]\n"
              "With no TASK, prompts in a terminal or reads standard input.")
        return 0
    if sys.argv[1:] == ["--health"]:
        return health()
    try:
        return run(read_task(sys.argv[1:]))
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"hexstrike-agent: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
