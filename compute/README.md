# Two-machine compute queue

This queue lets a workstation and an always-on server work on independent coding
and compute jobs. A coordinator keeps the job history and assigns work to available
workers. Each worker runs one job at a time. Dependencies let a later specialist
use the result of an earlier job.

CPU and RAM remain local to each machine. A job must fit within its worker's
resources; the queue does not make a single shared-memory computer. Large datasets
stay on the machine that processes them. Project bundles and returned results are
limited in size and are not a general file-sharing system.

## Installed arrangement

Both workers use the installed Codex CLI with their own ChatGPT subscription
sign-in. Cluster Desk on the workstation and the existing Telegram bot submit
work to the same queue. The server hosts the durable queue and read-only
dashboard; the workstation submits jobs over SSH and maintains an automatic
tunnel to that dashboard.

| Machine | Running services | Worker limits |
| --- | --- | --- |
| Workstation | Worker, desktop control service and dashboard tunnel | One job, CPU quota equivalent to 2 logical CPUs, 4 GiB RAM |
| Headless server | Worker, dashboard and existing Telegram assistant | One job, CPU quota equivalent to 3 logical CPUs, 6 GiB RAM |

These are worker limits, not a reservation of particular CPU cores or a combined
RAM pool. Each worker also has a 256-task process/thread limit and a 1 GiB swap
limit. User lingering is enabled on both machines so the services can start at
boot and remain available after logout. No desktop session is required on the
server.

## Cluster Desk

Open **Cluster Desk** from the workstation's application menu or desktop shortcut.
It runs in its own desktop window with task history on the left, a conversation
and prompt box in the center, and machine status alongside.

- **Team** sends an objective through research, implementation, review and testing.
- **Agent** assigns an individual specialist to either machine.
- **Command** runs the entered shell command on the selected worker. Shell syntax,
  including pipes and redirection, is supported. Commands have that user's normal
  permissions; choose the target machine before submitting.
- **Projects** registers a clean local Git folder. Agent tasks require a project;
  commands can run without one.
- Select a job to read its summary, live output or changes, cancel it, retry it,
  or save its patch. **Continue from changes** uses a successful job's project
  snapshot and patch for the next task; it does not edit the original project.

Machine cards show recent CPU, memory and disk usage. Activity includes jobs
submitted from the desktop, command line and Telegram. Each worker has one job
slot, so later jobs wait. Task conversations are stored privately on the
workstation; queue history and results are stored on the server. Closing the
window does not cancel work.

The desktop service listens only at `http://127.0.0.1:18891`. It checks the host,
request origin and a local anti-forgery token before accepting control requests.
It has no public network listener. A disconnected server is shown as unavailable;
an uncertain submission is retained for inspection and is never automatically
sent again.

Install the desktop app after installing the workstation queue client:

```bash
python3 desktop_install.py
```

The desktop window needs Python GI, GTK 3 and WebKit2 4.1. It uses the existing
desktop libraries and installs a `compute-cluster-console` user service. The
server remains headless.

## Telegram control

The HexStrike agent can also run directly on the headless server without
Telegram or the queue. From an SSH or VS Code Remote SSH terminal, use
`hexstrike-agent --health` and then `hexstrike-agent "TASK"`; running it with no
task prompts interactively. Installation and configuration are documented in
[the HexStrike runbook](HEXSTRIKE.md).

Use the existing bot's private chat from the verified owner account:

```text
/cluster status
/cluster run server python3 --version
/cluster ask myproject Fix the failing tests
/cluster job latest
/hexstrike Use server_health and report the API status
/hexstrike job latest
```

Phone submissions appear in **All activity** in Cluster Desk. Short commands can
return output immediately; check longer work with `job latest`. The server needs
an internet connection while you are away. Consult the private owner notes for
the installed network arrangement rather than assuming the tether is provided
by the phone used for Telegram.

See [Telegram commands and maintenance](TELEGRAM.md) for all commands and recovery
instructions, and [the HexStrike deployment runbook](HEXSTRIKE.md) for the Kali
container, subscription-backed agent, verification, and rollback procedure. The
plugin uses the existing bot and verified-owner policy; it does not change
ordinary assistant chat or open a public cluster endpoint.

`/hexstrike TASK` sends an owner-requested security task to the server worker.
The dedicated agent reaches the HexStrike MCP API only through host loopback;
the unauthenticated API and security tools run inside an unprivileged Kali LXD
container. Use `/hexstrike status`, `/hexstrike health`, `/hexstrike job latest`,
and `/hexstrike cancel JOB_ID` to manage those jobs. The command uses the existing
bot and queue and does not change ordinary chat.

## Using the queue

Agents can now consult the owner's available GitHub repositories, branches,
commit history, pull requests and issues through authenticated GitHub CLI on
either machine. Private notes on observed repository conventions are included
in agent context. See [GitHub history access](GITHUB.md) for setup and examples.
History is fetched as needed; project snapshots and reviewable patches keep
their existing isolation.

The `clusterctl` command is the control interface. Its built-in `--help` describes
the available options and exact argument order.

| Command | Purpose |
| --- | --- |
| `clusterctl status` | Show worker availability and recent jobs. |
| `clusterctl project add NAME PATH` | Register a clean Git checkout as an immutable source bundle. |
| `clusterctl run [OPTIONS] -- COMMAND [ARGUMENTS...]` | Queue a command with an explicit argument list. |
| `clusterctl agent --project NAME [OPTIONS] 'TASK'` | Give a coding agent a task and specialist role. |
| `clusterctl team --project NAME [--wait] 'OBJECTIVE'` | Submit the predefined specialist workflow with job dependencies. |
| `clusterctl show JOB_ID` | Inspect one job's state and result summary. |
| `clusterctl logs JOB_ID` | Read a job's captured output. |
| `clusterctl patch JOB_ID --output FILE` | Save the changes for review to a new file. |
| `clusterctl cancel JOB_ID` | Cancel a queued job or ask its worker to terminate a running job. |
| `clusterctl retry JOB_ID` | Explicitly create a new job from an earlier job's specification. |

Register a project before submitting agent work. Project registration requires a
clean Git checkout: commit intended source changes first. Registration bundles
only the current committed tree as a new snapshot commit, without its previous
Git history. Credential-like filenames and submodules are rejected. Keep secrets
and large generated files out of the source tree; filename checks cannot identify
every secret. Private bundles remain in the cluster's state storage; the queue
does not publish them to a Git hosting service.

For example, using a clean project that already has Python unit tests:

```bash
clusterctl project add myproject /path/to/myproject
clusterctl run --project myproject --node server --wait -- python3 -m unittest
clusterctl agent --project myproject --node server --role build 'Fix the failing tests and explain the change.'
clusterctl team --project myproject --wait 'Add pagination with regression tests.'
```

The fixed team workflow sends research to the workstation, implementation to the
server, then review to the workstation and testing to the server in parallel.
Review and testing inherit the implementation patch. Each job defaults to a
30-minute deadline; `--timeout` sets a deadline in seconds. The `--wait` option
keeps the command open to show state changes. Closing that waiting command does
not cancel a submitted job.

Each job checks out the registered revision into its own working directory, with
Git hooks disabled. Workers do not edit the original project checkout. Dependent
jobs can apply an earlier job's patch to a fresh checkout. Review returned patches
before applying them to your project. The queue does not automatically commit,
merge, push, deploy, or send external messages.

The available specialist roles are research, build, review, test, and compute.
Research and review agent jobs are read-only. A role supplies task instructions;
it does not give the worker new machine permissions or install project tools.
Install each project's needed compilers, runtimes, and dependencies on the worker
that will use them.

Codex workers must return a structured completion report. A blocked or failed
report makes the job fail even when the CLI exits normally. Research can succeed
by diagnosing expected baseline failures and proposing a fix. Review fails for
actionable correctness findings; required failing tests prevent completion.

A local `compute-cluster` skill is also installed for the lead coding agent.
Ask it to use the compute cluster for a named project and objective; the skill
handles submission, following results, and reviewing patches within the task's
authorized scope. The reusable skill source is in `skill/compute-cluster/`.

### VSCodium tasks

Open the cluster roadmap project in VSCodium, then choose **Terminal → Run Task**:

- **Cluster: status** shows the machines and recent jobs.
- **Cluster: open Cluster Desk** opens the desktop control app.
- **Cluster: open dashboard** opens the local dashboard page.
- **Cluster: specialist team** prompts for the registered project and objective.

These shortcuts come from the roadmap project's `.vscode/tasks.json`; they do not
automatically appear in every coding project. The same `clusterctl` commands work
from a workstation terminal regardless of which project is open.

## Read-only dashboard

The dashboard listens only on `127.0.0.1:18890` and refreshes every eight seconds.
It shows online or stale workers, job targets, assigned workers, run times, and
escaped result summaries. It does not expose raw logs, arbitrary files, command
execution, or job-management actions. Responses are marked `no-store`.

On the deployed workstation, use the VSCodium dashboard task or visit
`http://127.0.0.1:18890`. The server
dashboard and workstation tunnel are automatic user services, so normal use does
not require starting a tunnel by hand.

As a fallback, when no automatic tunnel is using that port, start one from the
**workstation** using its configured, verified server alias:

```bash
ssh -N -o StrictHostKeyChecking=yes -L 127.0.0.1:18890:127.0.0.1:18890 SERVER_SSH_ALIAS
```

Then open `http://127.0.0.1:18890` in the workstation browser. Keep the tunnel open
while using the page. Use `clusterctl` for actions, detailed logs, and patches.

## Installation and updates

`install.py` installs the current machine's app files, private configuration, CLI
launcher, and systemd user services. It needs Python 3, Git, systemd user services,
and an installed, signed-in agent executable. The workstation also needs verified
noninteractive SSH access to the server. Provision each worker's sign-in locally;
do not copy an OAuth cache between machines.

Run these examples from the `compute` source directory on the indicated machine.
Replace the uppercase placeholders and executable path with the local values.

**Server over SSH:**

Install a complete Codex CLI runtime, including its command-host sidecar and
sandbox resources, and sign in on that machine with `codex login --device-auth`.
Do not copy another machine's authentication files. On Ubuntu 24.04, the packaged
bubblewrap helper and AppArmor profile may need an administrator setup step:

```bash
sudo sh setup-sandbox.sh
```

The script follows [OpenAI's Ubuntu sandbox guidance](https://learn.chatgpt.com/docs/sandboxing).
It installs the distribution helper and loads the packaged profile without
changing the global user-namespace restriction. The cluster installer below runs
as the normal user, not root.

```bash
python3 install.py --node-id server --backend codex \
  --agent-command /absolute/path/to/codex --dashboard --start
```

**Workstation:**

```bash
python3 install.py --node-id workstation --backend codex \
  --agent-command /absolute/path/to/codex \
  --coordinator-ssh SERVER_SSH_ALIAS \
  --coordinator-script /home/LOGIN_USER/.local/share/compute-cluster/app/coordinator.py \
  --tunnel --start
```

`--dashboard` installs the loopback dashboard on the coordinator machine.
`--tunnel` installs the SSH tunnel and **Compute Cluster** application shortcut on
the workstation. `--start` starts or restarts the selected services immediately;
without it the installer only installs and enables them. Do not restart a worker
in the middle of a job unless interruption is intended.

`--coordinator-script` is the coordinator's absolute path on the server. Omit
`--coordinator-ssh` on the server so requests use its local coordinator. The
installer also supports an OpenClaw backend and `--agent-config` for a separately
prepared configuration; the deployed compute workers use `--backend codex`.

The installer backs up overwritten files in
`~/.local/state/compute-cluster/install-backups/`. Re-running it rewrites the
generated configuration and selected service files, so preserve intentional
private customizations when updating. It does not erase existing queue history
or workspaces. It does not itself enable user lingering; that is already enabled
for this deployment and must be arranged separately on a fresh machine.

## Isolation and account access

This is a trusted-owner queue, not a sandbox for untrusted users or arbitrary
downloads. Command jobs run with the worker account's normal permissions. An
isolated working directory prevents accidental checkout collisions; it is not a
container or a security boundary. Only submit commands and projects you trust.

The coordinator accepts requests through the local command interface or SSH.
There is no exposed network RPC listener. SSH keys and model subscription
credentials stay in private configuration outside this repository. Agent workers
use the configured subscription-backed model connection; there is no API-key or
paid-API fallback. If sign-in expires or subscription usage is exhausted, agent
jobs can fail until access is restored.

Workers run under service resource limits to preserve interactive use on the
workstation. CPU limits, memory limits, and a one-job-per-worker policy constrain
parallelism. Memory exhaustion, missing tools, or a failed test are reported as job
failures; scheduling on another machine does not repair those problems by itself.

## Persistence and recovery

Installed scripts live under `~/.local/share/compute-cluster/app/`. Private
configuration is `~/.config/compute-cluster/config.json`, and queue records,
bundles, workspaces, logs, and results live under
`~/.local/state/compute-cluster/`. Keep these locations private when making backups.

User services can restart workers and the dashboard after a crash or reboot;
user lingering lets them run without an interactive login. The coordinator's
queue persists between service runs. Losing the workstation does not stop work
already running on the server, although workstation jobs need that worker to
remain online.

Workers send heartbeats while running a job. A lost lease interrupts the job and
blocks jobs that depend on it. Workers terminate a running process on cancellation,
deadline, invalid lease, or prolonged coordinator connection loss. A completed
result is saved locally before delivery and can be delivered after connectivity
returns. Already-claimed work is never automatically executed a second time.

After a failure, inspect `clusterctl show` and `clusterctl logs`, resolve the cause,
and use `clusterctl retry` only when another execution is appropriate. Retrying
creates a new job: any external side effects of the first attempt are not undone.
For a stale worker, check the machine's connection and the installed user service
status and journal. Restore or restart that service rather than deleting queue
state. Historical interrupted jobs remain interrupted; recovery does not silently
rerun them.

On the **affected machine**, inspect the worker:

```bash
systemctl --user status compute-cluster-worker.service
journalctl --user -u compute-cluster-worker.service -n 50 --no-pager
```

For a dashboard problem, inspect
`compute-cluster-dashboard.service` on the **server over SSH** and
`compute-cluster-tunnel.service` on the **workstation** using the same commands.
Their restart policies reconnect after ordinary process or network failures. If
a manual restart is needed, use `systemctl --user restart SERVICE_NAME` on the
machine hosting that service. Restarting the tunnel or dashboard does not
restart worker jobs; restarting a worker can interrupt its current job.

## Verification

Offline automated checks cover queue validation, dependency handling, worker
execution and recovery behavior, source snapshots without old Git history,
credential-path rejection, CLI argument handling, and the specialist job graph.
Dashboard checks cover escaped text, read-only routing, and private response
handling. These checks do not by themselves prove live subscription access,
execution on both physical machines, or recovery after an actual reboot.

Live parallel command execution, cancellation, queue persistence and worker
process recovery have passed on both machines. The full research → build →
review/test workflow also passed, with review and testing running concurrently.
The returned patch passed independent tests in a fresh checkout while the
original source remained unchanged.
See [VERIFICATION.md](VERIFICATION.md) for measured results and current limits.
