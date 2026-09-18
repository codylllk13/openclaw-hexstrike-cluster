# Two-node coding/compute queue: implementation contract

Current user request authorizes a new two-machine cluster. Historical Alienware,
old private credentials, and root/sudo grants must not be restored. All runtime
state, private host configuration, job source bundles, logs and outputs live
outside this repository. No automatic GitHub publishing.

Python standard library only. Installed scripts live at
`~/.local/share/compute-cluster/app/`. Private config at
`~/.config/compute-cluster/config.json` (0600). Private state at
`~/.local/state/compute-cluster` (0700). Coordinator is always-on server;
workstation pulls jobs via SSH. No network RPC listener; `coordinator.py rpc`
reads one JSON object from stdin and emits `{ "ok": true, "data": ... }` or
`{ "ok": false, "error": "..." }`. Config/state locations can be overridden
with `CLUSTER_CONFIG` and `CLUSTER_STATE` for tests.

Modules owned independently: coordinator.py + test_coordinator.py (queue agent),
worker.py + test_worker.py (worker agent), client.py + clusterctl.py + dashboard
and deployment docs/integration (root). No edits to another owner's files.

`client.py` exports `load_config() -> dict` and `rpc(action, **kwargs) -> data`,
raising RuntimeError on failure. If config `coordinator_ssh` is set, invokes
`ssh -o BatchMode=yes -o ConnectTimeout=8 <alias> python3 <coordinator_script> rpc`;
otherwise invokes local coordinator Python. Transfer via RPC base64 is bounded
at 48 MiB per payload, not for huge datasets. Large data stays node-local.

RPC request schema `{action: name, ...fields}`:

- `status`: returns `{nodes: [...], jobs: [...]}` recent 100 jobs, no raw logs.
- `submit`: `{spec: job_spec}` returns full job record with `id`.
- `get`: `{id}` returns full job record plus `result` if any.
- `cancel`: `{id}` marks queued cancelled or running cancel_requested.
- `retry`: `{id}` explicitly submits a NEW job with same spec; never auto-retry.
- `heartbeat`: `{node, capabilities: ["command","agent"], active_job: id|null,
  lease_token: str|null, info: {cpu_count, memory_total_mb}}` updates node;
  returns `{cancel: bool, lease_valid: bool}`; active job lease refreshed only
  for matching node+token. Node ids are configured, simple identifiers.
- `claim`: `{node, capabilities}` atomically returns null or job with
  `{id, spec, lease_token, dependencies: [{id,result}]}`; one active job per node.
- `complete`: `{node,id,lease_token,result}` verifies lease and records finish.
  Result `{exit_code:int, summary:str, duration_seconds:float, log:str,
  patch_b64:str, artifacts_b64:str(optional)}`; bounded fields; exit0 succeeded,
  other failed unless cancellation requested. Lease expiry -> interrupted,
  never automatically execute a job twice; pending dependent jobs blocked.
- `project_put`: `{name, bundle_b64, revision}` immutable git bundle keyed by
  sha256; returns `{project:name, source_id:sha256, revision}`. Reject invalid
  name/hash/base64/oversize; never extract untrusted archives on coordinator.
- `project_get`: `{source_id}` returns `{bundle_b64}`.

Job spec: `{kind:"command"|"agent", title:str, target:"any"|node,
project:str|null, source_id:sha256|null, revision:git_sha|null,
argv:[str] (command), prompt:str (agent), role:"research"|"build"|"review"|"test"|"compute",
depends_on:[job_ids], inherit_from:job_id|null, timeout_seconds:30..14400}`.
Reject missing source/revision pairing, unknown dependencies, oversized input,
invalid role/kind/target, empty argv, NUL, option-like node ids. Dependencies
must succeed before claiming; inherit_from must be a successful dependency.
Source-free command jobs run in empty isolated job directory. Agent jobs require
a project. No shell=True or interpolated shell command from job content.

Worker config: `{node_id, coordinator_ssh?, coordinator_script,
backend:"codex"|"openclaw", agent_command:absolute_path,
agent_config:absolute_path(optional OpenClaw exec config),
model:"gpt-5.6-sol", poll_seconds:5, lease_seconds:90}`.
Worker root `state/work/<id>/repo`; clone provided bundle and checkout exact
revision detached, with repo hooks disabled. Reject symlinks/path traversal
when staging/extracting artifacts. Apply inherited binary patch to clone.
No mutation of source repository. Capture stdout/stderr privately, bounded
log returned. Write durable completion record locally before RPC complete;
retry completion delivery after temporary connection loss. Heartbeat during
process execution every10s, terminate process group on cancellation/deadline/
lease invalidation or prolonged communication failure. On worker restart do
not run an already-claimed job again; report interrupted if own stale claim.

Deployed agent backend on both nodes: Codex uses --ignore-user-config, --ignore-rules,
--sandbox workspace-write (research/review read-only), approval_policy never,
--ephemeral, model selected, low effort, prompt stdin, --output-last-message.
Remove API-key env vars and inherited provider endpoints; subscription only.
The optional OpenClaw adapter is not deployed or validated for this cluster;
its isolated exec path cannot reuse the existing Telegram OAuth store. Both
nodes use independent Codex device sign-ins. Final Codex output must satisfy
a structured completed/blocked/failed report; a blocked/failed report fails the
job even with a zero CLI exit code. Role instructions
restrict work to job objective, prohibit external messages/push/deployment and
require reporting tests/limits. This is a trusted-owner job queue, not a
multi-tenant sandbox. Arbitrary authorized command jobs have the worker user's
permissions; service resource controls limit CPU/RAM/processes.

After job, collect binary git diff including intended untracked project files
(exclude .git, .env*, credential/key files, logs, caches, node_modules); a patch
is the reviewable deliverable. No automatic upstream commits/push/merges.
For read-only roles, fail if source edits are detected. Node resources governed
by systemd per-worker: local CPUQuota200%, MemoryMax4G, TasksMax256; server
CPUQuota300%, MemoryMax6G. Workers enabled at boot with user lingering.

CLI root implements status, submit JSON, run argv, agent prompt, project add
(clean Git repo -> immutable bundle), team fixed specialist workflow,
show/logs/patch/cancel/retry. Dashboard is read-only loopback and accessed via
SSH tunnel. Persistent job records, resource limits, dependency handling,
cancellation, worker loss/rejoin and real execution on both nodes require tests.
