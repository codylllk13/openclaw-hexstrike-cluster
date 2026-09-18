#!/usr/bin/env bash
# Install the direct HexStrike launcher for terminal and VS Code Remote SSH use.
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agent_home="${HEXSTRIKE_AGENT_HOME:-$HOME/.local/share/hexstrike-agent}"
bin_dir="${HEXSTRIKE_BIN_DIR:-$HOME/.local/bin}"

install -d -m 700 "$agent_home" "$bin_dir"
install -m 700 "$source_dir/hexstrike_terminal.py" "$agent_home/hexstrike_terminal.py"
ln -sfn "$agent_home/hexstrike_terminal.py" "$bin_dir/hexstrike-agent"

printf 'Installed %s\n' "$bin_dir/hexstrike-agent"
"$bin_dir/hexstrike-agent" --health
