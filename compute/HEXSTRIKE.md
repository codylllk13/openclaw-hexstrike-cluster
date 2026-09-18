# Dedicated HexStrike agent for OpenClaw and Telegram

This runbook adds an owner-only `/hexstrike` command to an existing OpenClaw
Telegram assistant. The command submits a bounded job to the existing compute
queue. A one-shot Codex process, authenticated with a ChatGPT subscription,
uses the HexStrike MCP client. The HexStrike API and Kali tools run inside an
unprivileged LXD container.

The integration does not need an OpenAI API key. It does not start another
Telegram poller or change ordinary assistant chat.

Use security tools only on systems you own or are explicitly authorized to
test. Put the authorized target and scope in each task.

## Architecture

```mermaid
flowchart LR
    T[Verified owner in a private Telegram chat]
    O[Existing OpenClaw gateway]
    P[Owner-gated native plugin]
    Q[Durable compute queue]
    C[Ephemeral Codex CLI turn]
    M[HexStrike MCP client]
    L[Host loopback 127.0.0.1:8888]
    K[Unprivileged Kali LXD container]
    A[HexStrike API and Kali tools]

    T --> O --> P --> Q --> C --> M --> L --> K --> A
```

The native plugin verifies all of these facts before it invokes a helper:

- The command arrived through Telegram.
- OpenClaw marked the sender authorized and the owner.
- The sender matches the single private `commands.ownerAllowFrom` entry.
- Both routing endpoints identify that owner's private chat.
- The message is not a group thread or channel route.

The helper writes task text to a private `0600` file and queues only its random
filename. The worker consumes and deletes the file before starting Codex, so the
task does not appear in process arguments. `/hexstrike job` and `/hexstrike
cancel` reject queue entries whose exact runner command does not identify a
HexStrike job.

HexStrike exposes an unauthenticated HTTP API and includes a generic command
tool. Its upstream server also launches tools through a shell. Treat the API as
privileged remote command execution. This design keeps it in an unprivileged
container, binds it to container loopback, and forwards it only to host
loopback. Do not replace either loopback address with `0.0.0.0` or a LAN
address.

## Prerequisites and pinned inputs

The server needs:

- Ubuntu with systemd user services and enough storage for LXD plus the complete
  Kali tool set. Budget at least 45 GiB of free space before installation.
- LXD 5.x or newer and an unprivileged container storage pool.
- The queue in this directory installed with a `server` worker.
- OpenClaw 2026.9.4 with an existing paired, owner-restricted Telegram bot.
- A complete Codex CLI on the server, signed in locally with ChatGPT device
  authentication.
- Git, curl, Python 3 and a Python virtual-environment implementation.

The verified deployment used HexStrike commit
`9b8c780f324ce5145a322bfa23c98886f8424ba3` from
`https://github.com/letrbuck/hexstrike-ai.git`. Pin a reviewed commit instead of
silently following the remote default branch. Record the Kali image fingerprint
from `lxc image info` if an exact rebuild matters; the `current` image alias and
Kali repositories move over time.

The examples use these placeholders:

| Placeholder | Meaning |
| --- | --- |
| `LOGIN_USER` | Normal account that runs OpenClaw and the queue |
| `HEXSTRIKE_REF` | Reviewed HexStrike Git commit |
| `OWNER_TELEGRAM_ID` | Private numeric owner ID stored only in OpenClaw config |

Keep the owner ID, bot token, OAuth store, host inventory, queue state and
network details outside Git.

## Install the Kali container

Run host commands as an administrator only where shown. Run all other host
commands as `LOGIN_USER`.

Install and initialize LXD if the server does not already use it:

```bash
sudo snap install lxd --channel=5.21/stable
sudo usermod -aG lxd LOGIN_USER
newgrp lxd
lxd init --minimal
```

Review an existing LXD installation before running `lxd init`; do not replace
its storage or network configuration. Then create the unprivileged Kali
container and apply bounded resources:

```bash
lxc launch images:kali/current/default/amd64 kali-hexstrike
lxc config set kali-hexstrike limits.cpu 4
lxc config set kali-hexstrike limits.memory 8GiB
lxc config set kali-hexstrike limits.memory.swap false
lxc config set kali-hexstrike boot.autostart true
lxc config device add kali-hexstrike hexstrike-api proxy \
  listen=tcp:127.0.0.1:8888 connect=tcp:127.0.0.1:8888 bind=host
lxc config show kali-hexstrike --expanded
```

Verify that the instance reports `security.privileged: false` or has no
privileged override. Do not add host disks, USB devices, raw sockets or other
device passthrough unless a specific tool needs them and the added access has
been reviewed.

Update Kali and install its complete tool metapackage. The image should use the
official rolling repository with `main contrib non-free non-free-firmware`.
Inspect the existing source file before changing it.

```bash
lxc exec kali-hexstrike -- apt-get update
lxc exec kali-hexstrike -- env \
  DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a \
  apt-get -y --no-install-recommends install kali-linux-everything
lxc exec kali-hexstrike -- apt-get check
lxc exec kali-hexstrike -- dpkg --audit
lxc exec kali-hexstrike -- \
  dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' kali-linux-everything
```

`kali-linux-everything` is large and changes with Kali rolling. A download can
be separated from installation with `apt-get --download-only`. If a temporary
`/usr/sbin/policy-rc.d` is used to prevent package services from starting during
the transaction, preserve any pre-existing file and restore it afterward. The
included `finalize_hexstrike_install.sh` is a recovery helper for a staged
background install; it is not required for a normal foreground installation.

## Install HexStrike inside Kali

Clone the reviewed HexStrike revision on the host, verify it, and copy the
working tree without its Git metadata into the container:

```bash
export HEXSTRIKE_REF=9b8c780f324ce5145a322bfa23c98886f8424ba3
git clone https://github.com/letrbuck/hexstrike-ai.git /tmp/hexstrike-ai
git -C /tmp/hexstrike-ai checkout --detach "$HEXSTRIKE_REF"
test "$(git -C /tmp/hexstrike-ai rev-parse HEAD)" = "$HEXSTRIKE_REF"
lxc exec kali-hexstrike -- mkdir -p /opt/hexstrike/source /var/lib/hexstrike
tar --exclude=.git -C /tmp/hexstrike-ai -cf - . | \
  lxc exec kali-hexstrike -- tar -C /opt/hexstrike/source -xf -
rm -rf /tmp/hexstrike-ai
```

Create an isolated Python 3.12 environment. The `angr` dependency in this
revision needs compatible CFFI and pycparser releases; keep the verified pins
until a later upstream revision is tested.

```bash
lxc exec kali-hexstrike -- apt-get -y install python3.12 python3.12-venv
lxc exec kali-hexstrike -- python3.12 -m venv /opt/hexstrike/venv
lxc exec kali-hexstrike -- /opt/hexstrike/venv/bin/pip install --upgrade pip
lxc exec kali-hexstrike -- /opt/hexstrike/venv/bin/pip install \
  -r /opt/hexstrike/source/requirements.txt waitress \
  'cffi==1.17.1' 'pycparser==2.22'
lxc exec kali-hexstrike -- /opt/hexstrike/venv/bin/python -c \
  'import angr, flask, mcp; print("HexStrike Python imports passed")'
```

If the Kali image does not offer Python 3.12, install a reviewed standalone
runtime or use a supported interpreter after testing `angr`; do not overwrite
the distribution Python.

Create `/etc/systemd/system/hexstrike-api.service` inside the container:

```ini
[Unit]
Description=Loopback-only HexStrike API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/var/lib/hexstrike
Environment=PYTHONPATH=/opt/hexstrike/source
Environment=PATH=/opt/hexstrike/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/opt/hexstrike/venv/bin/waitress-serve --listen=127.0.0.1:8888 hexstrike_server:app
Restart=on-failure
RestartSec=5
TasksMax=512
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

Enable it and verify both sides of the loopback proxy:

```bash
lxc exec kali-hexstrike -- systemctl daemon-reload
lxc exec kali-hexstrike -- systemctl enable --now hexstrike-api.service
lxc exec kali-hexstrike -- systemctl --no-pager --full status hexstrike-api.service
lxc exec kali-hexstrike -- curl -fsS http://127.0.0.1:8888/health
curl -fsS http://127.0.0.1:8888/health
ss -ltnp | grep '127.0.0.1:8888'
```

The host check must show `127.0.0.1:8888`, not `0.0.0.0:8888`, `[::]:8888`, or
a LAN address.

## Install the subscription-backed agent runner

Sign in on the server itself. The supported device flow places the subscription
credential in Codex's private authentication store:

```bash
codex login --device-auth
codex login status
```

Do not set `OPENAI_API_KEY`, copy another machine's OAuth files, or add a paid
provider fallback. When subscription access is exhausted, jobs should fail or
wait until usage resets rather than switch billing paths.

The host MCP process needs the reviewed HexStrike MCP client, but it does not
need Kali tools. Keep a pinned source copy and private environment on the host:

```bash
install -d -m 700 "$HOME/.local/share/hexstrike-agent/source"
git clone https://github.com/letrbuck/hexstrike-ai.git /tmp/hexstrike-mcp
git -C /tmp/hexstrike-mcp checkout --detach "$HEXSTRIKE_REF"
install -m 600 /tmp/hexstrike-mcp/hexstrike_mcp.py \
  "$HOME/.local/share/hexstrike-agent/source/hexstrike_mcp.py"
python3 -m venv "$HOME/.local/share/hexstrike-agent/venv"
"$HOME/.local/share/hexstrike-agent/venv/bin/pip" install \
  'fastmcp>=0.2,<1' 'requests>=2.31,<3'
rm -rf /tmp/hexstrike-mcp
```

Install the runner and helper from this directory:

```bash
install -D -m 700 hexstrike_agent.py \
  "$HOME/.local/share/hexstrike-agent/hexstrike_agent.py"
install -D -m 700 hexstrike_control.py \
  "$HOME/.local/share/compute-cluster/app/hexstrike_control.py"
install -d -m 700 "$HOME/.local/state/hexstrike-agent/tasks" \
  "$HOME/.local/state/hexstrike-agent/work"
```

`hexstrike_agent.py` runs `codex exec` with `--ephemeral`, ignores ambient user
configuration, forces the ChatGPT login method, removes API provider overrides
from the child environment, and enables only the HexStrike MCP server. Codex's
filesystem sandbox is read-only. The MCP server remains powerful because it can
execute commands inside Kali, so the container and loopback boundary are still
required.

If the installed Codex executable is not at
`~/.local/share/compute-cluster/bin/codex`, update the reviewed `CODEX` constant
before installation. Keep the executable and MCP paths absolute.

### Run directly from a terminal or VS Code

The Telegram queue is optional. Install the direct launcher on the server:

```bash
./install_hexstrike_terminal.sh
hexstrike-agent --health
hexstrike-agent "Use server_health and summarize the available tool categories"
```

With no task argument, `hexstrike-agent` prompts for one. It also accepts a task
on standard input, so it works in a normal SSH terminal and the integrated
terminal in VS Code Remote SSH. The launcher uses the same loopback-only API and
ChatGPT subscription login as the queued runner. It removes API-key variables,
starts an ephemeral Codex turn, enables only the HexStrike MCP server, and keeps
the Codex filesystem sandbox read-only.

Set `CODEX_BIN` only when `codex` is not on `PATH` and is not installed at the
verified fallback path. `HEXSTRIKE_MODEL` can select another subscription-backed
Codex model. `HEXSTRIKE_URL` should remain `http://127.0.0.1:8888` unless the
isolation design is deliberately changed.

## Add `/hexstrike` to the existing Telegram bot

Follow [TELEGRAM.md](TELEGRAM.md) to install or update the native plugin. The
same linked plugin registers `/cluster` and `/hexstrike`; do not create a second
bot, token reader or gateway. Install `telegram-plugin/` at its persistent
private location, retain the existing owner-only configuration, validate the
OpenClaw schema, then restart the existing gateway once.

The relevant commands are:

| Command | Result |
| --- | --- |
| `/hexstrike TASK` | Queue a scoped task on the server worker. |
| `/hexstrike health` | Read the loopback API health and detected-tool counts. |
| `/hexstrike status` | Show server availability and recent HexStrike jobs. |
| `/hexstrike job latest` | Show the latest job's progress or result. |
| `/hexstrike job JOB_ID` | Show one HexStrike job. |
| `/hexstrike cancel JOB_ID` | Cancel a queued or running HexStrike job. |

Ordinary messages continue through the assistant's original model and tool
policy. The command handler returns with `continueAgent: false`, so a command is
never reinterpreted as an ordinary assistant prompt.

## Verification

Run offline checks from this `compute` directory:

```bash
python3 -m unittest test_hexstrike_control.py test_telegram_control.py
git diff --check
```

On the server, verify container isolation, package state, services and Codex
authentication without printing credentials:

```bash
lxc info kali-hexstrike
lxc config show kali-hexstrike --expanded
lxc exec kali-hexstrike -- systemctl is-active hexstrike-api.service
lxc exec kali-hexstrike -- dpkg-query -W -f='${db:Status-Abbrev}\n' kali-linux-everything
lxc exec kali-hexstrike -- apt-get check
test -z "$(lxc exec kali-hexstrike -- dpkg --audit)"
curl -fsS http://127.0.0.1:8888/health
ss -ltn | grep '127.0.0.1:8888'
codex login status
openclaw config validate
openclaw plugins inspect compute-cluster-control --runtime --json
```

Use harmless end-to-end tasks from the verified owner's private Telegram chat:

```text
/hexstrike health
/hexstrike Use server_health and report the API version and detected-tool count.
/hexstrike job latest
```

A passing health check alone does not prove every external executable works.
Verify representative commands inside Kali, and use `dpkg-query` to confirm the
metapackage. HexStrike's detector recognizes executable names, not every Kali
package, so its detected count can remain below its catalog size even when the
complete Kali metapackage is installed.

## Operations and recovery

Inspect the container service and existing queue worker before retrying a task:

```bash
lxc exec kali-hexstrike -- systemctl --no-pager --full status hexstrike-api.service
lxc exec kali-hexstrike -- journalctl -u hexstrike-api.service -n 100 --no-pager
systemctl --user status compute-cluster-worker.service
journalctl --user -u compute-cluster-worker.service -n 100 --no-pager
```

If a Telegram request times out, use `/hexstrike status` or `/hexstrike job
latest` before resending it. The gateway helper times out before long queue jobs
finish, and a submission may already have succeeded. Cancellation terminates the
queue job and deletes an unconsumed private task file.

After a reboot, LXD should start the container, systemd inside Kali should start
the API, and the existing user worker and gateway should start under their
normal user-service policy. Verify each layer separately. Restore from a reviewed
LXD snapshot only when package or service repair is insufficient; a snapshot
also rolls back logs and any state stored inside the container.

Subscription expiry or a usage limit affects only agent turns. Restore Codex
device authentication on the server or wait for the subscription window to
reset. Do not add an API key as a recovery shortcut.

Wireless, Bluetooth, GPU, smart-card and SDR packages can be installed while
their functions remain unavailable. They require compatible hardware, drivers,
container device passthrough and sometimes extra Linux capabilities. Add only
the minimum device access for a reviewed task. Network tools also remain subject
to the container's network route and the authorized target's reachability.

## Rollback and uninstall

Disable only the command integration first so ordinary Telegram chat remains
available:

```bash
openclaw plugins disable compute-cluster-control
systemctl --user restart openclaw-gateway.service
```

If `/cluster` must remain enabled, deploy the previously backed-up plugin version
that registers only `/cluster`, validate it, and restart the same gateway. Do
not remove the whole plugin merely to disable HexStrike unless cluster commands
are also being retired.

Remove the HexStrike runtime after confirming no HexStrike job is queued or
running:

```bash
lxc stop kali-hexstrike
lxc delete kali-hexstrike
lxc image list
rm -rf "$HOME/.local/share/hexstrike-agent"
rm -rf "$HOME/.local/state/hexstrike-agent"
rm -f "$HOME/.local/share/compute-cluster/app/hexstrike_control.py"
```

Remove the LXD image only if no other container needs it. Keep private backups
until the rollback is verified. Uninstalling LXD itself is outside this
component's rollback scope because other workloads may use the daemon, bridge or
storage pool.
