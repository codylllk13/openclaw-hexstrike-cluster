# Verification record — 2026-09-17

## Current status

The two-machine compute and coding cluster passed live acceptance. Research on
the workstation handed a plan to the server's builder; its patch was inherited
by workstation review and server testing, which ran concurrently. All four
specialists completed successfully through ChatGPT subscription authentication.
The returned patch also passed independent verification in a fresh checkout.

Cluster Desk is now installed as a native workstation window with a local
control service. The existing Telegram bot accepts owner-only cluster commands.
A command sent by the owner from Telegram completed on the server and appeared
in the desktop's All activity view.

## GitHub history access

- GitHub CLI is authenticated independently on each node. The server uses the
  verified official CLI release 2.101.0; the workstation retains 2.100.0.
- The enabled worker configuration supplies account-specific credentials only
  through agent process environments. Private repository convention notes are
  included as context; no credentials or account inventory are tracked here.
- Regression discovery reported 126 tests, with one native Telegram test class
  skipped because Node was unavailable on this invocation's PATH. The changed
  GitHub module and worker tests all passed. The earlier Node-enabled Telegram
  authorization checks remain recorded below; Telegram code was not changed.
- Installed worker permission settings were exercised in real Codex sandboxes
  from user-service environments on both machines. Each research and build
  profile authenticated, listed repositories through the GraphQL-backed CLI,
  paginated older private-repository commits, and read private Git references
  through the credential helper. An unrelated network domain was refused.
- Research denied inside and outside writes. Build permitted a workspace write
  and denied an outside write. No Unix-session-bus allowance was added.
- Separate live probes verified that the configured shell environment preserves
  the intended GitHub credential and drops an unrelated secret variable.
- These are installed CLI/sandbox checks and isolated worker execution tests;
  no additional model-backed jobs were submitted for this update. Existing
  desktop/Telegram submissions use these updated workers for subsequent jobs.

## Desktop and phone acceptance

- Regression suite: 101 tests passed, including 21 new console tests and nine
  telemetry/progress tests. The Telegram adapter added 15 passing tests, including
  its JavaScript owner/private-chat authorization gate run with Node enabled.
- The console tests include strict Host/Origin and token checks, durable request
  deduplication, concurrent submissions, partial failures without replay,
  follow-up patch lineage against a real coordinator, and patch downloads.
- A real server command was submitted through the desktop UI. Its output appeared
  while it was running, then its completed output and status were displayed.
  Explicit retry created a separate execution; cancellation was acknowledged.
- A research agent submitted through the UI completed on the workstation using
  its existing subscription sign-in. It inspected the demo source and tests,
  diagnosed the expected failure, and returned a report without changing files.
- Browser interaction verified project registration, persisted conversations,
  both machines' CPU/memory/disk cards, external job activity, patch preview,
  and the control for continuing work from an earlier patch.
- Restarting the local console changed its anti-forgery token; the open UI
  recovered its session and could register a project without reloading. Mutating
  requests are not automatically replayed during recovery.
- The GTK/WebKit desktop window launched successfully and remained active. Its
  application-menu and desktop launchers are installed. Native folder and save
  chooser interaction was not automated; browser path entry and patch fetching
  were verified separately.
- Installed OpenClaw configuration validation passed. Runtime inspection reports
  the cluster plugin loaded with its native command registered. Telegram channel
  probing reports running, healthy, and no channel error.
- The owner sent a command through the paired bot and reported its successful
  reply. The central job record confirms it ran on the server; the same completed
  phone command was visible in Cluster Desk. This proves the actual bot-to-queue
  path, beyond the mocked authorization tests.
- Existing assistant model, authentication, tool restrictions, channel pairing
  and owner policy match the private pre-install backup. No second bot poller,
  public control listener or paid API fallback was added.

## Passed checks

- Original queue suite: 71 tests covering SQLite transactions, concurrent claims,
  leases, cancellation, dependencies, retries, durable completion, isolated Git
  snapshots, excluded history, patch inheritance, structured agent outcomes,
  read-only roles, bounds, and transport/CLI validation.
- Both physical machines executed overlapping 12-second CPU workloads. Results
  returned centrally; each completed its assigned workload.
- A server-targeted job stayed queued while its worker was stopped and the
  dashboard restarted. It executed exactly once after the worker rejoined.
- A running workstation command cancelled successfully and its process stopped.
- A deliberately killed workstation worker restarted automatically. Its
  interrupted job returned exit 125 and was not executed again. Marker checks
  confirmed one execution and that the old child process was gone.
- Both workers are enabled user services with lingering enabled. Workstation
  limits: two CPU equivalents, 4 GiB RAM. Server limits: three CPU equivalents,
  6 GiB RAM. Each has one job slot and a 256-process cap.
- Both nodes authenticated with ChatGPT using their own local sign-in. No API
  key fallback was configured or used.
- The loopback dashboard is reachable through the workstation's SSH tunnel;
  HTTP checks cover read-only routes, escaped content and no-store responses.
- The full specialist workflow repaired a deliberately broken `sum_squares`
  function: research diagnosed two failing baseline tests, the builder returned
  a minimal one-line patch, and review/test both accepted it. All three existing
  tests passed; the test specialist also checked mixed-sign, all-negative and
  empty inputs. Review and testing overlapped on separate physical machines.
- Independent verification applied the returned patch to a fresh checkout and
  ran all three tests successfully. The original source remained clean and its
  file hash was unchanged.
- Workspace-write allowed an inside-workspace write and rejected an outside
  write. Read-only rejected both. Ubuntu's global user-namespace restriction
  remained enabled.
- All five installed application modules matched the repository source on both
  nodes. Dashboard and tunnel were bound only to loopback; direct LAN access to
  the dashboard was refused.

## Issues found and addressed

The first server agent attempt lacked its command-host sidecar. The complete
runtime was installed. The process had returned exit zero while reporting that
it could not work; workers now require a structured final report and treat
blocked, failed, missing or malformed reports as failed jobs.

The next attempt correctly failed because Ubuntu rejected the bundled sandbox's
network namespace setup. Downstream jobs were blocked. The administrator ran
`setup-sandbox.sh` to install Ubuntu's bubblewrap package and stock AppArmor
profile. Live checks confirmed workspace-write permits writes inside the job
directory and rejects writes outside it; read-only rejects both writes. Global kernel restrictions remain unchanged. Legacy
Landlock compatibility was investigated but is not enabled.

## Limits of this verification

Reboot recovery, a prolonged real network outage, deliberate subscription
exhaustion, GPU workloads, multi-node shared memory and large dataset transfer
have not been live-tested. No OS reinstall, partition resizing, router changes,
unsolicited external messaging or GitHub publication were performed. Telegram
replies to the owner's explicit cluster commands were exercised as described above.
