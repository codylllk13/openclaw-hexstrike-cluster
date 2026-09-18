# Control the cluster from your phone

Use the existing paired OpenClaw bot's private chat. Send `/cluster help` to see
the commands. Ordinary chat continues to use the existing assistant; `/cluster`
commands go directly to the compute queue without asking a model to interpret
them.

| Send | Result |
| --- | --- |
| `/cluster status` | Worker availability and recent jobs. |
| `/cluster projects` | Registered project snapshots available to the server. |
| `/cluster run server python3 --version` | Run an explicit command on the server. |
| `/cluster run workstation python3 --version` | Queue work for the workstation. |
| `/cluster ask myproject Fix the failing pagination tests` | Queue research, implementation, then review and testing. |
| `/cluster agent server myproject Explain and fix this bug` | Queue one coding agent. |
| `/cluster job latest` | Inspect the last job or specialist team submitted from this bot. |
| `/cluster job JOB_ID` | Inspect progress, the result, and patch availability. |
| `/cluster cancel JOB_ID` | Cancel queued work or stop a running job. |
| `/cluster retry JOB_ID` | Explicitly create a new attempt after a terminal result. |

The same private chat also exposes a dedicated HexStrike agent:

| Send | Result |
| --- | --- |
| `/hexstrike TASK` | Queue an owner-requested security task on the isolated Kali agent. |
| `/hexstrike health` | Check the loopback-only HexStrike API. |
| `/hexstrike status` | Show the server worker and recent HexStrike jobs. |
| `/hexstrike job latest` | Read the latest HexStrike job and result. |
| `/hexstrike job JOB_ID` | Read one HexStrike job. |
| `/hexstrike cancel JOB_ID` | Cancel a queued or running HexStrike job. |

The agent can invoke the Kali command set through HexStrike's MCP tools. The
HexStrike API is not exposed to the LAN: it runs in an unprivileged LXD container
and reaches the host only through `127.0.0.1`. Hardware-dependent wireless, GPU,
Bluetooth and SDR tools still require compatible hardware attached to the server.
See [the dedicated HexStrike runbook](HEXSTRIKE.md) for reproducible installation,
subscription authentication, verification, recovery, and removal.

Replace `myproject` with a name from `/cluster projects`. `NODE` can be `server`,
`workstation`, or `any`. Job IDs accept an unambiguous eight-character prefix;
use the full ID if the prefix is ambiguous or no longer in recent history.

Register a clean Git project through the desktop app before requesting agent
work. The phone uses the newest registered snapshot for that project. Agents
work on isolated copies and return reviewable patches; they do not merge changes
into your original checkout. Download and review patches in the desktop app.

`run` treats the text after the node as an explicitly requested shell command.
It uses the selected worker account's existing permissions and resource limits.
Short commands may return their result immediately; longer work returns a queued
ID. Use `job latest` or `job JOB_ID` to check again. The command does not send
unsolicited completion messages.

The server needs internet access to receive and reply through Telegram and to
run subscription-backed agents. Already queued local compute commands can run
without internet if their own inputs and tools are local. The persistent queue
stays on the server when the workstation is offline: server-targeted work can
continue, and workstation-targeted work waits for that worker. The default team
starts with workstation research, so that team waits if the workstation is away.
RAM remains local to each machine.

## Installation and activation

This adapter targets the installed OpenClaw **2026.9.4** native command API.
It uses the existing gateway's authorized command and reply path. It neither
reads a bot token nor opens another Telegram polling connection.

1. Back up the server's private OpenClaw configuration and plugin inventory.
2. Install `telegram_control.py` beside `client.py` and `coordinator.py` under
   `~/.local/share/compute-cluster/app/`. The gateway's user must already have the
   cluster's private configuration and access to its queue.
3. Stage `telegram-plugin/` at a persistent location such as
   `~/.local/share/compute-cluster/telegram-plugin/`. Keep the staged source free
   of credentials and owner identifiers.
4. Use the installed CLI to link the reviewed source:

   ```bash
   openclaw plugins install --link ~/.local/share/compute-cluster/telegram-plugin --force --accept-capabilities
   ```

5. Set `plugins.entries.compute-cluster-control.config.ownerId` in the private
   OpenClaw configuration. Derive it in memory from the sole current
   `telegram:OWNER_TELEGRAM_ID` entry in `commands.ownerAllowFrom`; do not obtain
   it from old project files or copy it into Git, command-line arguments, or
   logs. Preserve the other configuration fields and file mode `0600`.
6. Enable the configured plugin and inspect its runtime:

   ```bash
   openclaw plugins enable compute-cluster-control --accept-capabilities
   openclaw config validate
   openclaw plugins inspect compute-cluster-control --runtime --json
   ```

The CLI may leave a newly installed plugin disabled until its required
configuration exists. An existing restrictive `plugins.allow` list must include
`compute-cluster-control`; an explicit deny remains authoritative. Managed
configuration changes can restart the gateway automatically. Installing code
otherwise requires a gateway restart; use the existing user service rather than
starting a second gateway. No assistant model, authentication, tool permissions,
Telegram pairing, group policy, or router settings need to change.

The manifest declares `/cluster` and `/hexstrike` command aliases with kind
`runtime-slash` and startup activation. The installed manifest contract does not require a
`contracts.commands` entry. The registered command requests `operator.admin`
scope so OpenClaw requires owner authority on chat surfaces and exposes the
owner decision to the handler.

The handler independently requires the configured owner to match the current
owner allowlist, the sender to match that owner, authorization and owner status
to be true, and both Telegram routing endpoints to be that owner's private
chat. Other channels, group routes, channel direct-message routes, and missing
authorization facts are denied. Replies return only through the invoking chat.

## Recovery and verification

If a command times out or reports interrupted submission, inspect `status` and
`job latest` before sending it again. Some jobs may already have been accepted.
Partial team submissions retain acknowledged job IDs and are not automatically
recreated. `retry` is an explicit new attempt and does not undo earlier effects.

If `/cluster` is unavailable, check the plugin runtime inspection, private owner
configuration, and existing gateway service. If owner authorization changed,
the plugin fails closed until its private setting matches the current verified
owner. Do not relax pairing or group policy to troubleshoot it.

To disable this adapter while retaining ordinary bot chat:

```bash
openclaw plugins disable compute-cluster-control
```

The local test suite covers helper parsing, exact node selection, source
selection, specialist dependencies, partial submission, response bounds,
explicit retry, private HexStrike task-file handoff, job ownership filters, and a
mocked native handler's owner/private-chat gate. These
tests do not prove live Telegram delivery or real account authorization. Finish
deployment verification by sending `/cluster status` and an innocuous command
such as `/cluster run server python3 --version`, then `/hexstrike health`, from the
verified owner's phone. Check the cluster job in the desktop app.
