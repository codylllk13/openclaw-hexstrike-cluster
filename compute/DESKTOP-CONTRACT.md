# Desktop control app interface

Current request authorizes a ChatGPT-style desktop control interface, replacing
read-only scope for this new local console. Existing read-only server dashboard
and SSH transport stay available. The Telegram adapter in TELEGRAM.md submits
to the same queue; its jobs appear in the desktop's recent activity. The router
remains unchanged.

Workstation console listens only 127.0.0.1:18891 using stdlib HTTP and client.rpc.
Serves desktop assets and JSON API; rejects unexpected Host/Origin, no CORS,
no-store, X-Frame-Options DENY, CSP self-only scripts/styles (no inline handlers),
frame-ancestors none. Per-process CSRF token embedded as meta name cluster-token;
API calls send X-Cluster-Token, POST application/json. No credential values exposed.
This is a single-owner local control UI; commands have existing worker-user rights.

GET /api/state -> {connected:bool,error:str|null,nodes:[...],jobs:[...],
 projects:[{name,path,revision}],conversations:[{id,title,created_at,updated_at,
 turns:[{id,kind,text,project,target,role,created_at,jobs:[{id,role}],error?}]}]}.
Calls coordinator status; preserve local conversations/projects and report offline
instead of empty success. Poll ~3s frontend, do not overlap polls. Status jobs may
carry progress object (below). Read job detail on selection ~2s while running.

GET /api/jobs/ID -> {job:full_job_record,patch_available:bool}; job.result.log
available, binary fields removed. job.progress may supply live log/timestamp.
GET /api/jobs/ID/patch -> application/octet-stream attachment ID.patch; bounded.
POST /api/submit -> {conversation_id?, request_id:uniqueUUID, kind:team|agent|command,
 text:nonempty, project?:name, target:any|workstation|server, role?:build|research|review|test|compute,
 timeout_seconds:30..14400(default1800), follow_up_to?:successful jobID}
 -> {conversation,turn}. Team dispatches research(workstation) -> build(server) ->
 review(workstation) + test(server). Reuse existing cli source registry/snapshot.
Use source from successful parent when follow_up_to provided; inherit parent's
patch for research/build (same source), then inherit new builder patch for reviews.
Commands interpreted as explicitly requested shell script using argv ['/bin/bash','-lc',text]
(no shell=True in backend). Agent requires project; command project optional.
Conversation/turn metadata persisted privately outside repo (console SQLite or
atomic JSON); request_id prevents duplicate jobs on double-submit/retry. If network
failure during multi-job submission, preserve submitted IDs and report incomplete
submission, never silently resubmit. Console serializes mutations.
POST /api/jobs/ID/cancel -> {job: safe_record}
POST /api/jobs/ID/retry -> {job:safe_record}; link new attempt into original turn.
POST /api/projects -> {name,path}; returns registered project, snapshots clean
Git HEAD via existing clusterctl.add_project. Frontend supports pasted path and
native Browse callback. Don't copy full history, don't change source or auto-commit.
Error envelope {error:str}, meaningful HTTP codes. Max POST body256KiB.

Telemetry: coordinator backward-compatible migration adds job progress JSON;
heartbeat optional progress {log:str capped32KiB,updated_at:epoch} accepted only
for matching active node/token; get returns progress. status omits log but can give
updated_at. Heartbeat info adds cpu_percent, memory_used_mb, disk_total_gb,
disk_used_gb, load_1, worker_memory_mb, worker_cpu_percent (optional finite bounded
numbers). Worker samples /proc, disk and cgroup where available; no blocking sleep
for CPU sampling. Send redacted rolling process output every heartbeat (5s during
job) and machine stats while idle. Keep completion/lease/recovery behavior intact.

Native root wrapper: Python GI Gtk3 + WebKit2 4.1 (already installed), standalone
window, app/menu/Desktop launcher. Start local console service if needed.
Allow only own console navigation; links external go system browser on deliberate
user click. JS bridge only choose_folder; frontend calls
window.webkit.messageHandlers.clusterNative.postMessage(JSON.stringify({action:'choose_folder'}));
root invokes window.clusterFolderSelected(path) via safe JSON serialization.
Frontend falls back path entry in browser. File download support for patch via
WebKit download handler. No general shell bridge. Console actions are API only.

Design: ChatGPT-inspired restrained dark UI, sidebar new-task/search/history and
project management; center selected conversation messages, user prompt and
specialist cards/progress/results, composer bottom with Team/Agent/Command modes,
project and node selectors. Right collapsible live-cluster panel CPU/RAM/disk,
active jobs, state. Detail drawer logs/summary/patch with cancel/retry/download.
Use textContent for untrusted job/model output, never innerHTML from records.
No fake metrics or nonfunctional controls. Keep current selection/draft during
polls; show pending/error/offline states and do not erase draft on failure.
