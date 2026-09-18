"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  let token = document.querySelector('meta[name="cluster-token"]')?.content || "";
  let tokenRefreshPromise = null;
  const terminal = new Set(["succeeded", "failed", "cancelled", "interrupted", "blocked"]);
  const active = new Set(["running", "cancel_requested"]);
  const roles = { research: ["◈", "Research"], build: ["⌘", "Implementation"], review: ["◇", "Review"], test: ["✓", "Testing"], compute: ["›_", "Compute"] };
  let data = { connected: false, nodes: [], jobs: [], projects: [], conversations: [] };
  let selectedId = null;
  let view = "chat";
  const drafts = new Map();
  const signatures = new Map();
  let sending = false;
  let refreshTimer;
  let refreshing = false;
  let queuedRefresh = false;
  let lastRefresh = null;
  let detail = { id: null, job: null, tab: "summary", patch: null, patchAvailable: false, fetching: false, mutation: false, timer: null, generation: 0, retryRequest: null };
  let detailOpener = null;
  let toastTimer;
  let projectSaving = false;
  const historicalJobs = new Map();
  const historyErrors = new Map();
  const historyInFlight = new Set();
  const HISTORY_CACHE_LIMIT = 256;
  let historyLoadTimer;
  const shell = document.querySelector(".app-shell");

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }
  function setText(node, value) {
    const text = String(value ?? "");
    if (node.textContent !== text) node.textContent = text;
  }
  function replace(node, children) { node.replaceChildren(...children); }
  function errorMessage(error) { return error?.message || "Something went wrong. Please try again."; }
  function showError(id, message) { setText($(id), message || ""); $(id).hidden = !message; }
  function id() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (v) => v.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  async function renewToken() {
    if (!tokenRefreshPromise) {
      tokenRefreshPromise = (async () => {
        const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
        try {
          const response = await fetch("/", { cache: "no-store", credentials: "same-origin", signal: controller.signal });
          if (!response.ok) throw new Error("The console is restarting. Try again shortly.");
          const documentCopy = new DOMParser().parseFromString(await response.text(), "text/html");
          const fresh = documentCopy.querySelector('meta[name="cluster-token"]')?.content;
          if (!fresh || fresh === "__CLUSTER_TOKEN__") throw new Error("The local console could not refresh its session.");
          token = fresh;
          document.querySelector('meta[name="cluster-token"]').content = fresh;
        } finally { clearTimeout(timeout); }
      })().finally(() => { tokenRefreshPromise = null; });
    }
    return tokenRefreshPromise;
  }
  async function api(path, body, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 25000);
    try {
      const headers = { "X-Cluster-Token": token };
      const request = { method: body === undefined ? "GET" : "POST", headers, cache: "no-store", credentials: "same-origin", signal: controller.signal };
      if (body !== undefined) { headers["Content-Type"] = "application/json"; request.body = JSON.stringify(body); }
      const response = await fetch(path, request);
      if (options.blob && response.ok) return await response.blob();
      let result;
      try { result = await response.json(); } catch (_) { throw new Error("The local console returned an unreadable response."); }
      if (!response.ok) {
        if (response.status === 403 && result.error === "Missing or invalid console token" && !options.tokenRetried) {
          await renewToken();
          if (body === undefined) return api(path, undefined, { ...options, tokenRetried: true });
          throw new Error("The local console restarted and its session has been refreshed. Your draft is safe. Try the action again; no action was automatically resent.");
        }
        const failure = new Error(result.error || `Request failed (${response.status}).`);
        failure.data = result; failure.status = response.status; throw failure;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The connection took too long. Your task may still have been accepted; retrying this submission will check the same request.");
      if (error instanceof TypeError) throw new Error("Cannot reach the local console. Check that its service is running.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function stamp(value) {
    if (typeof value === "number" && Number.isFinite(value)) return value * 1000;
    if (typeof value === "string") { const parsed = Date.parse(value); return Number.isFinite(parsed) ? parsed : null; }
    return null;
  }
  function timeLabel(value) { const ms = stamp(value); return ms ? new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : ""; }
  function dateLabel(value) {
    const ms = stamp(value);
    if (!ms) return "";
    const when = new Date(ms), today = new Date();
    return when.toDateString() === today.toDateString() ? "Today" : when.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function duration(job) {
    let seconds = job?.result?.duration_seconds;
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) {
      const start = stamp(job?.started_at), end = stamp(job?.finished_at);
      if (!start) return "Not started";
      seconds = ((end || Date.now()) - start) / 1000;
    }
    const rounded = Math.max(0, Math.floor(seconds));
    if (rounded >= 3600) return `${Math.floor(rounded / 3600)}h ${Math.floor(rounded % 3600 / 60)}m`;
    return rounded >= 60 ? `${Math.floor(rounded / 60)}m ${rounded % 60}s` : `${rounded}s`;
  }
  function labelState(value) { return ({ succeeded: "Complete", running: "Working", queued: "Queued", failed: "Failed", interrupted: "Interrupted", cancelled: "Cancelled", cancel_requested: "Cancelling", blocked: "Blocked" })[value] || "Pending"; }
  function badge(value) { return element("span", `status-badge ${terminal.has(value) || active.has(value) || value === "queued" ? value : ""}`, labelState(value)); }
  function nodeName(value) { return ({ workstation: "Workstation", server: "Headless server", any: "Any worker" })[value] || value || "Unassigned"; }
  function jobById(jobId) { return data.jobs.find((job) => job.id === jobId) || (detail.job?.id === jobId ? detail.job : null) || historicalJobs.get(jobId)?.job || null; }
  function currentConversation() { return data.conversations.find((conversation) => conversation.id === selectedId); }
  function cacheHistoricalJob(job) {
    const spec = job.spec || {}, result = job.result || {}, summary = String(result.summary || "");
    const brief = {
      id: job.id, status: job.status, node: job.node, retry_of: job.retry_of,
      created_at: job.created_at, started_at: job.started_at, finished_at: job.finished_at,
      error: job.error ? String(job.error).slice(0, 4000) : null,
      spec: { title: spec.title, role: spec.role, target: spec.target, project: spec.project,
        source_id: spec.source_id, revision: spec.revision, kind: spec.kind, depends_on: spec.depends_on },
      result: job.result ? { summary: summary.length > 8000 ? `${summary.slice(0, 8000)}\n\n[Open the full result to read more.]` : summary,
        duration_seconds: result.duration_seconds, exit_code: result.exit_code } : null,
    };
    historicalJobs.delete(job.id);
    historicalJobs.set(job.id, { job: brief, fetchedAt: Date.now() });
    while (historicalJobs.size > HISTORY_CACHE_LIMIT) historicalJobs.delete(historicalJobs.keys().next().value);
    historyErrors.delete(job.id);
  }
  function scheduleHistoryLoads() {
    clearTimeout(historyLoadTimer);
    historyLoadTimer = setTimeout(loadVisibleHistory, 80);
  }
  function loadVisibleHistory() {
    const conversation = currentConversation();
    if (view !== "chat" || !conversation || !data.connected || historyInFlight.size >= 2) return;
    const viewport = $("chat-scroll").getBoundingClientRect();
    // Hydrate visible turns plus a small overscan. This also bounds the cache for
    // arbitrarily long conversations; scrolling back restores evicted results.
    const visibleJobs = Array.from($("conversation").querySelectorAll("[data-history-job]")).filter((node) => {
      const box = node.getBoundingClientRect();
      return box.bottom >= viewport.top - 300 && box.top <= viewport.bottom + 300;
    }).map((node) => node.dataset.historyJob);
    const recentIds = new Set(data.jobs.map((job) => job.id));
    const wanted = [...new Set(visibleJobs)];
    for (const jobId of wanted) {
      if (historyInFlight.size >= 2) break;
      if (recentIds.has(jobId) || historyInFlight.has(jobId)) continue;
      const cached = historicalJobs.get(jobId), failed = historyErrors.get(jobId);
      if (cached && (terminal.has(cached.job.status) || Date.now() - cached.fetchedAt < 15000)) continue;
      if (failed && (failed.permanent || Date.now() - failed.triedAt < 60000)) continue;
      historyInFlight.add(jobId);
      const requestedConversation = selectedId;
      api(`/api/jobs/${encodeURIComponent(jobId)}`).then((response) => {
        if (!response.job?.id) throw new Error("The saved job is unavailable.");
        cacheHistoricalJob(response.job);
      }).catch((error) => {
        historyErrors.set(jobId, { message: errorMessage(error), triedAt: Date.now(), permanent: error.status === 404 });
        while (historyErrors.size > HISTORY_CACHE_LIMIT) historyErrors.delete(historyErrors.keys().next().value);
      }).finally(() => {
        historyInFlight.delete(jobId);
        if (selectedId === requestedConversation && view === "chat") { signatures.delete("chat"); render(); }
        else scheduleHistoryLoads();
      });
    }
  }
  function draft() {
    const key = selectedId || "__new__";
    if (!drafts.has(key)) drafts.set(key, { text: "", kind: "team", project: "", target: "any", role: "build", followUp: null, blocked: false, pending: null });
    return drafts.get(key);
  }
  function toast(message) { clearTimeout(toastTimer); setText($("toast"), message); $("toast").hidden = false; toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500); }
  function resizePrompt() { const input = $("prompt"); input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 190)}px`; }
  function rememberComposer() {
    Object.assign(draft(), { text: $("prompt").value, project: $("project-select").value, role: $("role-select").value });
    if (draft().kind !== "team") draft().target = $("node-select").value;
  }
  function updateComposer(load = false) {
    const current = draft();
    if (load) { $("prompt").value = current.text; $("project-select").value = current.project; $("role-select").value = current.role; resizePrompt(); }
    document.querySelectorAll("[data-mode]").forEach((button) => { const on = button.dataset.mode === current.kind; button.classList.toggle("selected", on); button.setAttribute("aria-pressed", String(on)); });
    const nodeSelect = $("node-select");
    setText(nodeSelect.options[0], current.kind === "team" ? "Both machines" : "Any available");
    nodeSelect.disabled = current.kind === "team" || sending;
    nodeSelect.value = current.kind === "team" ? "any" : current.target;
    $("role-wrap").hidden = current.kind !== "agent";
    $("prompt").placeholder = current.kind === "command" ? "Enter a shell command or script…" : current.kind === "agent" ? "Give a specialist a task…" : "Describe a task for your team…";
    setText($("mode-description"), current.kind === "command" ? "Commands run with the selected worker account’s permissions." : current.kind === "agent" ? "One specialist works in an isolated project copy." : "A team researches, implements, reviews, and tests.");
    $("follow-up-chip").hidden = !current.followUp;
    if (current.followUp) setText($("follow-up-label"), `Continuing from: ${current.followUp.title}`);
    $("project-select").disabled = Boolean(current.followUp) || sending;
    $("send-task").disabled = sending || !current.text.trim() || current.blocked || !data.connected;
    $("send-task").classList.toggle("sending", sending);
    setText($("send-symbol"), sending ? "·" : "↑");
    $("send-task").setAttribute("aria-label", sending ? "Submitting task" : "Send task");
    $("role-select").disabled = sending;
  }
  function selectConversation(conversationId) {
    rememberComposer(); selectedId = conversationId; view = "chat";
    showError("composer-error", ""); signatures.delete("chat"); updateProjectSelect(); updateComposer(true); render();
    $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
    if (window.innerWidth <= 650) shell.classList.add("sidebar-hidden");
  }
  function newTask() { selectConversation(null); $("prompt").focus(); }
  function showActivity() {
    rememberComposer(); selectedId = null; view = "activity"; showError("composer-error", ""); updateComposer(true); render();
    if (window.innerWidth <= 650) shell.classList.add("sidebar-hidden");
  }
  function updateProjectSelect() {
    const signature = JSON.stringify(data.projects.map((p) => [p.name, p.revision]));
    if (signatures.get("projects-select") === signature) return;
    signatures.set("projects-select", signature);
    const selected = draft().project;
    const empty = element("option", "", "Choose a project"); empty.value = "";
    const options = [empty];
    for (const project of data.projects) { const option = element("option", "", project.name); option.value = project.name; options.push(option); }
    const add = element("option", "", "+ Add project…"); add.value = "__add_project__"; options.push(add);
    replace($("project-select"), options); $("project-select").value = selected;
    setText($("project-count"), data.projects.length);
    if ($("project-dialog").open) renderProjects();
  }
  function renderHistory() {
    const search = $("history-search").value.trim().toLowerCase();
    const conversations = [...data.conversations].sort((a, b) => (stamp(b.updated_at) || 0) - (stamp(a.updated_at) || 0));
    const visible = conversations.filter((c) => !search || `${c.title} ${(c.turns || []).map((t) => t.text).join(" ")}`.toLowerCase().includes(search));
    const signature = JSON.stringify([selectedId, view, search, visible.map((c) => [c.id, c.title, c.updated_at, (c.turns || []).some((t) => (t.jobs || []).some((ref) => active.has(jobById(ref.id)?.status)))])]);
    if (signatures.get("history") === signature) return;
    signatures.set("history", signature);
    const buttons = visible.map((conversation) => {
      const button = element("button", `history-item${view === "chat" && selectedId === conversation.id ? " active" : ""}`);
      button.type = "button"; button.title = conversation.title || "Untitled task"; button.dataset.focusKey = `history-${conversation.id}`;
      button.setAttribute("aria-current", selectedId === conversation.id && view === "chat" ? "page" : "false");
      const working = (conversation.turns || []).some((turn) => (turn.jobs || []).some((ref) => active.has(jobById(ref.id)?.status)));
      button.append(element("span", "history-title", conversation.title || "Untitled task"));
      const meta = element("span", "history-meta"); meta.append(element("span", `tiny-dot${working ? " running" : ""}`), element("span", "", working ? "Working now" : dateLabel(conversation.updated_at)));
      button.append(meta); button.addEventListener("click", () => selectConversation(conversation.id)); return button;
    });
    replace($("history"), buttons); $("history-empty").hidden = buttons.length > 0;
    setText($("history-empty"), search ? "No matching tasks." : "Your conversations will appear here.");
    setText($("history-count"), conversations.length || "");
    $("show-activity").classList.toggle("active", view === "activity");
  }
  function jobSummary(job) {
    if (!job) return "Waiting for a coordinator update.";
    if (job.result?.summary) return job.result.summary;
    if (job.error) return job.error;
    if (job.status === "running") return "Working on the task. Open to follow live output.";
    if (job.status === "cancel_requested") return "Stopping the running process.";
    if (job.status === "queued") return job.spec?.depends_on?.length ? "Waiting for earlier specialists to finish." : "Waiting for an available worker.";
    return "Open this job for details.";
  }
  function specialistCard(ref) {
    const job = jobById(ref.id), role = ref.role || job?.spec?.role || "compute";
    const known = roles[role] || roles.compute;
    const button = element("button", `specialist-card ${job?.status || "queued"}`); button.type = "button"; button.dataset.focusKey = `job-${ref.id}`; button.dataset.historyJob = ref.id;
    const top = element("span", "specialist-top"); top.append(element("span", "role-glyph", known[0]), element("span", "role-name", known[1]), badge(job?.status));
    const bottom = element("span", "specialist-footer"), elapsed = element("span", "", job ? duration(job) : ""); elapsed.dataset.jobDuration = ref.id; bottom.append(element("span", "", nodeName(job?.node || job?.spec?.target)), elapsed);
    const summary = !job && historyErrors.has(ref.id) ? "Saved result unavailable right now. Open the job to try again." : jobSummary(job);
    button.append(top, element("span", "specialist-summary", String(summary).slice(0, 220)), bottom);
    button.addEventListener("click", () => openDetail(ref.id)); return button;
  }
  function turnPhase(turn) {
    const attempts = (turn.jobs || []).map((ref) => jobById(ref.id)).filter(Boolean);
    const superseded = new Set(attempts.map((job) => job.retry_of).filter(Boolean));
    const jobs = attempts.filter((job) => !superseded.has(job.id));
    const complete = jobs.filter((job) => job.status === "succeeded").length;
    const failures = jobs.filter((job) => ["failed", "blocked", "interrupted"].includes(job.status));
    if (turn.error) return ["Submission needs attention", "Some work may have been accepted. Check the jobs below before submitting again."];
    if (attempts.length < (turn.jobs || []).length) return [data.connected ? "Loading history" : "History unavailable", data.connected ? "Retrieving the saved specialist results. Open a job if its details do not appear." : "Reconnect to retrieve missing results. Already loaded results remain available."];
    if (!jobs.length) return ["Waiting for an update", "Your request has been saved."];
    if (failures.length) return ["Needs attention", "A job needs attention. Open its details to inspect the result or retry."];
    if (complete === jobs.length) return ["Complete", turn.kind === "team" ? "Your team has finished. Review the implementation and specialist reports below." : "The task has finished. Here’s the result."];
    if (jobs.every((job) => terminal.has(job.status))) return ["Stopped", "This task has stopped. Completed results remain available below."];
    if (turn.kind === "team") return [`${complete} of ${jobs.length} complete`, complete ? "Your specialists are building on the completed work." : "Your team will research, implement, and verify this task."];
    return [jobs.some((job) => active.has(job.status)) ? "Working" : "Queued", jobs.some((job) => active.has(job.status)) ? "Your worker is on it. Open the job to see live output." : "Your task will start when its worker is available."];
  }
  function renderTurn(turn) {
    const section = element("article", "turn"); section.dataset.turnId = turn.id;
    const phase = turnPhase(turn);
    section.append(element("div", "user-message", turn.text || ""));
    const meta = element("div", "user-meta"); meta.append(element("span", "", turn.project || "No project"), element("span", "", "·"), element("span", "", timeLabel(turn.created_at))); section.append(meta);
    const heading = element("div", "assistant-heading"); heading.append(element("span", "assistant-icon", "✳"), element("strong", "", turn.kind === "team" ? "Specialist team" : turn.kind === "command" ? "Compute worker" : "Your specialist"), element("span", "assistant-status", phase[0]));
    section.append(heading, element("p", "turn-intro", phase[1]));
    const grid = element("div", "specialist-grid"); for (const ref of turn.jobs || []) grid.append(specialistCard(ref)); section.append(grid);
    if (turn.error) section.append(element("p", "turn-error", turn.error));
    const candidates = (turn.jobs || []).map((ref) => jobById(ref.id)).filter(Boolean);
    const resultJob = [...candidates].reverse().find((job) => job.spec?.role === "build" && job.status === "succeeded") || (turn.kind !== "team" ? [...candidates].reverse().find((job) => terminal.has(job.status)) : null);
    if (resultJob?.result?.summary) {
      const summary = String(resultJob.result.summary);
      section.append(element("div", `turn-result${summary.length > 1300 ? " collapsed" : ""}`, summary.slice(0, 7000)));
      const actions = element("div", "turn-actions");
      const details = element("button", "subtle-button", "View full result"); details.dataset.focusKey = `result-${resultJob.id}`; details.addEventListener("click", () => openDetail(resultJob.id)); actions.append(details);
      if (resultJob.status === "succeeded" && resultJob.spec?.source_id) { const follow = element("button", "subtle-button", "Continue from this work"); follow.addEventListener("click", () => continueFrom(resultJob)); actions.append(follow); }
      section.append(actions);
    }
    return section;
  }
  function renderChat() {
    const conversation = currentConversation();
    $("welcome").hidden = Boolean(conversation) || view !== "chat";
    $("conversation").hidden = !conversation || view !== "chat";
    $("activity-view").hidden = view !== "activity";
    setText($("conversation-title"), view === "activity" ? "All activity" : conversation?.title || "New task");
    setText($("conversation-subtitle"), view === "activity" ? "Every job, across both machines" : conversation ? `${conversation.turns?.length || 0} request${conversation.turns?.length === 1 ? "" : "s"} · Isolated workspaces` : "Work across your machines");
    if (view === "activity") { renderActivity(); return; }
    if (!conversation) return;
    const signature = JSON.stringify([conversation, (conversation.turns || []).flatMap((turn) => (turn.jobs || []).map((ref) => { const job = jobById(ref.id); return [ref.id, job?.status, job?.node, job?.result?.summary, job?.error, job?.finished_at]; }))]);
    if (signatures.get("chat") === signature) return;
    signatures.set("chat", signature);
    const area = $("chat-scroll"), nearBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 100;
    const scroll = area.scrollTop, focusKey = document.activeElement?.dataset?.focusKey;
    replace($("conversation"), (conversation.turns || []).map(renderTurn));
    if (focusKey) { const restore = Array.from($("conversation").querySelectorAll("[data-focus-key]")).find((node) => node.dataset.focusKey === focusKey); restore?.focus({ preventScroll: true }); }
    area.scrollTop = nearBottom ? area.scrollHeight : scroll;
  }
  function activityCard(job) {
    const button = element("button", "activity-card"); button.type = "button"; button.dataset.focusKey = `activity-${job.id}`;
    const info = element("span", "activity-info"); info.append(element("strong", "", job.spec?.title || "Untitled job"), element("span", "", `${nodeName(job.node || job.spec?.target)} · ${job.spec?.project || "No project"} · ${dateLabel(job.created_at)} ${timeLabel(job.created_at)}`));
    button.append(element("span", "activity-glyph", (roles[job.spec?.role] || roles.compute)[0]), info, badge(job.status));
    button.addEventListener("click", () => openDetail(job.id)); return button;
  }
  function renderActivity() {
    const signature = JSON.stringify(data.jobs.map((job) => [job.id, job.status, job.node, job.spec?.title, job.created_at]));
    if (signatures.get("activity") === signature) return;
    signatures.set("activity", signature);
    replace($("activity-list"), data.jobs.length ? data.jobs.map(activityCard) : [element("p", "activity-empty muted", "No jobs yet. Send a task to get started.")]);
  }
  function numeric(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null; }
  function metric(label, percentage, value) {
    const row = element("div", `metric${percentage === null ? " unavailable" : ""}`);
    const heading = element("div", "metric-label"); heading.append(element("span", "", label), element("span", "metric-value", value));
    const track = element("div", "metric-track"); const fill = element("div", `metric-fill${percentage > 90 ? " high" : percentage > 70 ? " medium" : ""}`);
    fill.style.width = `${percentage === null ? 0 : Math.max(0, Math.min(100, percentage))}%`;
    track.append(fill); row.append(heading, track); return row;
  }
  function machineCard(node) {
    const online = data.connected && node.online === true;
    const info = node.info || {};
    const card = element("section", "machine-card");
    const top = element("div", "machine-heading"); top.append(element("span", "machine-icon", node.node === "server" ? "▤" : "▱"), element("h3", "", nodeName(node.node)), element("span", `node-status${online ? "" : " offline"}`, online ? "Online" : "Offline"));
    const cpuCount = numeric(info.cpu_count), total = numeric(info.memory_total_mb), used = online ? numeric(info.memory_used_mb) : null, cpu = online ? numeric(info.cpu_percent) : null;
    const diskTotal = numeric(info.disk_total_gb), diskUsed = online ? numeric(info.disk_used_gb) : null;
    card.append(top, element("p", "machine-caption", [cpuCount ? `${cpuCount} logical CPUs` : null, total ? `${(total / 1024).toFixed(1)} GiB RAM` : null].filter(Boolean).join(" · ") || "Waiting for machine details"));
    card.append(metric("CPU", cpu, cpu === null ? "Unavailable" : `${Math.round(cpu)}%`));
    card.append(metric("Memory", used !== null && total ? used / total * 100 : null, used !== null && total ? `${(used / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} GiB` : "Unavailable"));
    card.append(metric("Disk", diskUsed !== null && diskTotal ? diskUsed / diskTotal * 100 : null, diskUsed !== null && diskTotal ? `${Math.round(diskUsed)} / ${Math.round(diskTotal)} GiB` : "Unavailable"));
    const footer = element("div", "metric-footer"); footer.append(element("span", "", node.active_job ? "● Worker busy" : online ? "○ Worker available" : "○ Awaiting connection"), element("span", "", node.node === "server" ? "3 CPU · 6 GiB cap" : "2 CPU · 4 GiB cap")); card.append(footer);
    return card;
  }
  function renderCluster() {
    const online = data.nodes.filter((node) => node.online === true).length;
    ["header-connection", "sidebar-connection"].forEach((target) => { $(target).className = `connection-dot ${data.connected ? "online" : "offline"}`; });
    setText($("cluster-connection-label"), data.connected ? `${online} machine${online === 1 ? "" : "s"} online` : "Offline");
    const health = $("cluster-health"); replace(health, [element("span", `pulse-dot ${data.connected ? "online" : "offline"}`), element("span", "", data.connected ? "Connected to your cluster" : "Coordinator unavailable")]);
    showError("connection-banner", data.connected ? "" : data.error || "Connecting to your cluster. Your drafts and task history stay here.");
    replace($("machine-cards"), data.nodes.length ? data.nodes.map(machineCard) : [element("p", "panel-empty", "Waiting for machine status…")]);
    const jobs = data.jobs.filter((job) => active.has(job.status) || job.status === "queued");
    setText($("active-count"), jobs.length); $("active-empty").hidden = jobs.length > 0;
    setText($("active-empty"), data.connected ? "Nothing running right now.\nYour workers are ready for a task." : "Reconnect to see current jobs.");
    replace($("active-jobs"), jobs.slice(0, 8).map((job) => {
      const button = element("button", "active-job"); button.dataset.focusKey = `active-${job.id}`; button.append(element("span", "active-job-title", job.spec?.title || "Untitled job"));
      const meta = element("span", "active-job-meta"); meta.append(element("span", "", nodeName(job.node || job.spec?.target)), element("span", "", labelState(job.status))); button.append(meta); button.addEventListener("click", () => openDetail(job.id)); return button;
    }));
    setText($("all-job-count"), data.jobs.length);
    setText($("last-updated"), lastRefresh ? `Updated ${lastRefresh.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}` : "Waiting for an update");
  }
  function render() {
    const focused = document.activeElement, focusKey = focused?.dataset?.focusKey;
    updateProjectSelect(); renderHistory(); renderChat(); renderCluster(); updateComposer();
    document.querySelectorAll("[data-job-duration]").forEach((node) => { const job = jobById(node.dataset.jobDuration); if (job) setText(node, duration(job)); });
    if (focusKey && !focused.isConnected) Array.from(document.querySelectorAll("[data-focus-key]")).find((node) => node.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
    scheduleHistoryLoads();
  }
  async function refresh() {
    clearTimeout(refreshTimer);
    if (refreshing) { queuedRefresh = true; return; }
    refreshing = true;
    try {
      const result = await api("/api/state");
      data = { ...data, ...result, nodes: result.nodes || [], jobs: result.jobs || [], projects: result.projects || [], conversations: result.conversations || [] };
      if (data.connected) lastRefresh = new Date();
    } catch (error) { data.connected = false; data.error = errorMessage(error); }
    finally { refreshing = false; render(); const delay = queuedRefresh ? 50 : 3000; queuedRefresh = false; refreshTimer = setTimeout(refresh, delay); }
  }
  function mergeConversation(conversation) {
    if (!conversation?.id) return;
    const index = data.conversations.findIndex((item) => item.id === conversation.id);
    if (index >= 0) data.conversations[index] = conversation; else data.conversations.unshift(conversation);
  }
  async function submitTask() {
    rememberComposer();
    const current = draft(), sourceKey = selectedId || "__new__";
    if (sending || !current.text.trim() || current.blocked) return;
    if (!data.connected) { showError("composer-error", "Reconnect to the cluster before submitting."); return; }
    if (current.kind !== "command" && !current.project && !current.followUp) { showError("composer-error", "Choose a project for this task, or add one from Projects."); $("project-select").focus(); return; }
    const body = { kind: current.kind, text: current.text.trim(), target: current.kind === "team" ? "any" : current.target, role: current.kind === "command" ? "compute" : current.role, timeout_seconds: 1800 };
    if (selectedId) body.conversation_id = selectedId;
    if (current.project) body.project = current.project;
    if (current.followUp) body.follow_up_to = current.followUp.id;
    const fingerprint = JSON.stringify(body);
    if (!current.pending || current.pending.fingerprint !== fingerprint) current.pending = { fingerprint, requestId: id() };
    body.request_id = current.pending.requestId;
    sending = true; showError("composer-error", ""); updateComposer();
    try {
      const result = await api("/api/submit", body, { timeout: 140000 });
      mergeConversation(result.conversation);
      if (result.turn?.error) { const partial = new Error(result.turn.error); partial.data = result; throw partial; }
      if (current.text.trim() === body.text) current.text = "";
      current.pending = null; current.blocked = false;
      if (result.conversation?.id) {
        const nextId = result.conversation.id;
        if (!drafts.has(nextId)) drafts.set(nextId, { ...current });
        if ((selectedId || "__new__") === sourceKey) { selectedId = nextId; view = "chat"; draft().pending = null; }
      }
      signatures.delete("chat"); updateComposer(true); render(); $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
      await refresh();
    } catch (error) {
      const partial = error.data;
      if (partial?.conversation) {
        mergeConversation(partial.conversation); current.blocked = true;
        if ((selectedId || "__new__") === sourceKey) { selectedId = partial.conversation.id; view = "chat"; drafts.set(selectedId, current); updateComposer(true); }
        showError("composer-error", `${errorMessage(error)} Some jobs may already be queued. Inspect them below; edit the draft before starting another submission.`);
        render(); refresh();
      } else showError("composer-error", errorMessage(error));
    } finally { sending = false; updateComposer(); }
  }
  function openDetail(jobId) {
    detailOpener = document.activeElement;
    clearTimeout(detail.timer);
    detail = { ...detail, id: jobId, job: jobById(jobId), tab: "summary", patch: null, patchAvailable: false, fetching: false, mutation: false, generation: detail.generation + 1, retryRequest: null };
    $("detail-drawer").hidden = false; $("drawer-backdrop").hidden = false;
    showError("detail-error", ""); $("detail-content").dataset.tab = "";
    renderDetail(); $("close-detail").focus(); fetchDetail();
  }
  function closeDetail() { clearTimeout(detail.timer); detail.id = null; detail.generation++; $("detail-drawer").hidden = true; $("drawer-backdrop").hidden = true; if (detailOpener?.isConnected) detailOpener.focus({ preventScroll: true }); }
  async function fetchDetail() {
    if (!detail.id || detail.fetching) return;
    const jobId = detail.id, generation = detail.generation; detail.fetching = true;
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (detail.id !== jobId || detail.generation !== generation) return;
      detail.job = result.job; detail.patchAvailable = result.patch_available === true;
      if (result.job?.id) cacheHistoricalJob(result.job);
      if (!detail.mutation) showError("detail-error", ""); renderDetail();
      if (detail.tab === "patch" && detail.patchAvailable && detail.patch === null) loadPatch();
    } catch (error) { if (detail.id === jobId && detail.generation === generation) showError("detail-error", errorMessage(error)); }
    finally { if (detail.id === jobId && detail.generation === generation) { detail.fetching = false; detail.timer = setTimeout(fetchDetail, terminal.has(detail.job?.status) ? 6000 : 2000); } }
  }
  function renderDetail() {
    const job = detail.job;
    setText($("detail-title"), job?.spec?.title || "Loading job…");
    replace($("detail-meta"), job ? [badge(job.status), element("span", "", nodeName(job.node || job.spec?.target)), element("span", "", "·"), element("span", "", duration(job)), element("span", "", "·"), element("span", "", job.spec?.project || "No project")] : []);
    document.querySelectorAll("[data-detail-tab]").forEach((button) => { const selected = button.dataset.detailTab === detail.tab; button.classList.toggle("selected", selected); button.setAttribute("aria-selected", String(selected)); });
    const content = $("detail-content"), nearBottom = content.scrollHeight - content.scrollTop - content.clientHeight < 60;
    if (content.dataset.tab !== detail.tab) {
      content.dataset.tab = detail.tab;
      replace(content, [element("p", "detail-note"), element(detail.tab === "summary" ? "div" : "pre", detail.tab === "summary" ? "detail-text" : detail.tab === "logs" ? "log-output" : "patch-output")]); content.scrollTop = 0;
    }
    let note = "", text = "Loading job…";
    if (job && detail.tab === "summary") {
      note = job.result?.exit_code !== undefined ? `Finished with exit code ${job.result.exit_code}.` : active.has(job.status) ? "This worker is running. Live logs update while the task is active." : "Job results will appear here.";
      text = job.result?.summary || job.error || jobSummary(job);
      if (job.error && job.result?.summary) text += `\n\n${job.error}`;
    } else if (job && detail.tab === "logs") {
      const live = job.progress?.log;
      note = active.has(job.status) ? `Live output${job.progress?.updated_at ? ` · Updated ${timeLabel(job.progress.updated_at)}` : ""}. Output can arrive in batches.` : "Captured job output. Long output may be truncated by the worker.";
      text = terminal.has(job.status) ? job.result?.log || live || "No output was captured." : live || job.result?.log || "Waiting for the worker’s first output…";
    } else if (job && detail.tab === "patch") {
      note = detail.patchAvailable ? "Review these changes before applying them. Your original project has not been changed." : "Patches become available when a job returns source changes.";
      text = detail.patch ?? (detail.patchAvailable ? "Loading patch…" : "No patch is available for this job.");
    }
    setText(content.firstChild, note); setText(content.lastChild, text);
    if (detail.tab === "logs" && nearBottom) content.scrollTop = content.scrollHeight;
    $("detail-cancel").hidden = !job || !(active.has(job.status) || job.status === "queued");
    $("detail-cancel").disabled = detail.mutation || job?.status === "cancel_requested" || !data.connected;
    $("detail-retry").hidden = !job || !terminal.has(job.status); $("detail-retry").disabled = detail.mutation || !data.connected;
    $("detail-download").hidden = !detail.patchAvailable; $("detail-download").disabled = detail.mutation;
    $("detail-continue").hidden = !job || job.status !== "succeeded" || !job.spec?.source_id;
    setText($("detail-continue"), detail.patchAvailable ? "Continue from changes" : "Continue task");
  }
  async function loadPatch() {
    const jobId = detail.id, generation = detail.generation;
    if (!jobId || !detail.patchAvailable || detail.patch !== null) return;
    try {
      const blob = await api(`/api/jobs/${encodeURIComponent(jobId)}/patch`, undefined, { blob: true });
      const text = await blob.text();
      if (detail.id === jobId && detail.generation === generation) { detail.patch = text.length > 250000 ? `${text.slice(0, 250000)}\n\n[Preview shortened. Download the patch for all changes.]` : text; renderDetail(); }
    } catch (error) { if (detail.id === jobId) showError("detail-error", errorMessage(error)); }
  }
  async function downloadPatch() {
    const jobId = detail.id; if (!jobId) return;
    $("detail-download").disabled = true;
    try {
      const blob = await api(`/api/jobs/${encodeURIComponent(jobId)}/patch`, undefined, { blob: true });
      const url = URL.createObjectURL(blob), anchor = element("a"); anchor.href = url; anchor.download = `${jobId}.patch`; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000); toast("Patch download started.");
    } catch (error) { showError("detail-error", errorMessage(error)); }
    finally { $("detail-download").disabled = false; }
  }
  async function mutateJob(action) {
    const jobId = detail.id; if (!jobId || detail.mutation) return;
    detail.mutation = true; showError("detail-error", ""); renderDetail();
    if (action === "retry" && !detail.retryRequest) detail.retryRequest = id();
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/${action}`, { request_id: action === "retry" ? detail.retryRequest : id() }, { timeout: 40000 });
      if (detail.id === jobId) {
        if (action === "retry" && result.job?.id) { openDetail(result.job.id); toast("A new attempt has been queued."); }
        else { detail.job = result.job || detail.job; renderDetail(); toast("Cancellation requested."); }
      }
      refresh();
    } catch (error) { if (detail.id === jobId) showError("detail-error", errorMessage(error)); }
    finally { detail.mutation = false; if (detail.id) renderDetail(); }
  }
  function continueFrom(job) {
    const conversation = data.conversations.find((item) => (item.turns || []).some((turn) => (turn.jobs || []).some((ref) => ref.id === job.id)));
    if (detail.id) closeDetail();
    selectConversation(conversation?.id || null);
    draft().followUp = { id: job.id, title: job.spec?.title || "Completed job" }; draft().project = job.spec?.project || ""; draft().kind = "team"; draft().blocked = false; draft().pending = null;
    updateComposer(true); $("prompt").focus(); toast("The next task will start from this job’s source and changes.");
  }
  function renderProjects() {
    replace($("project-list"), data.projects.length ? data.projects.map((project) => {
      const row = element("div", "project-row"), info = element("div"); info.append(element("strong", "", project.name), element("p", "project-path", project.path || "Registered source"));
      const use = element("button", "", "Use project"); use.type = "button"; use.addEventListener("click", () => { draft().project = project.name; draft().followUp = null; updateComposer(true); $("project-dialog").close(); $("prompt").focus(); }); row.append(info, use); return row;
    }) : [element("p", "muted small", "No projects yet. Add one to give your agents a workspace.")]);
  }
  function openProjects() {
    renderProjects(); showError("project-error", ""); $("project-dialog").showModal();
    const native = Boolean(window.webkit?.messageHandlers?.clusterNative);
    setText($("browse-help"), native ? "Paste an absolute folder path or choose a folder." : "Paste the absolute path to a folder on the workstation. Native Browse is available in the desktop app.");
    $("browse-project").disabled = !native; $("project-name").focus();
  }
  async function saveProject(event) {
    event.preventDefault(); if (projectSaving) return;
    projectSaving = true; $("save-project").disabled = true; setText($("save-project"), "Registering…"); showError("project-error", "");
    const name = $("project-name").value.trim(), path = $("project-path").value.trim();
    try {
      await api("/api/projects", { name, path }, { timeout: 140000 });
      draft().project = name; draft().followUp = null; await refresh(); updateComposer(true); $("project-name").value = ""; $("project-path").value = ""; renderProjects(); toast(`Project “${name}” is ready.`);
    } catch (error) { showError("project-error", errorMessage(error)); }
    finally { projectSaving = false; $("save-project").disabled = false; setText($("save-project"), "Add project"); }
  }
  window.clusterFolderSelected = (path) => {
    if (typeof path !== "string" || !path) return;
    $("project-path").value = path;
    if (!$("project-name").value.trim()) $("project-name").value = path.split("/").filter(Boolean).pop()?.replace(/[^A-Za-z0-9_.-]/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 64) || "project";
    $("project-name").focus();
  };
  $("new-task").addEventListener("click", newTask);
  $("show-activity").addEventListener("click", showActivity);
  $("history-search").addEventListener("input", renderHistory);
  $("chat-scroll").addEventListener("scroll", scheduleHistoryLoads, { passive: true });
  $("toggle-sidebar").addEventListener("click", () => shell.classList.toggle("sidebar-hidden"));
  $("close-sidebar").addEventListener("click", () => shell.classList.add("sidebar-hidden"));
  function toggleCluster(force) { const hide = force === undefined ? !shell.classList.contains("cluster-hidden") : force; shell.classList.toggle("cluster-hidden", hide); $("toggle-cluster").setAttribute("aria-expanded", String(!hide)); }
  $("toggle-cluster").addEventListener("click", () => toggleCluster()); $("close-cluster").addEventListener("click", () => toggleCluster(true));
  document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => { rememberComposer(); draft().kind = button.dataset.mode; draft().blocked = false; updateComposer(); $("prompt").focus(); }));
  $("prompt").addEventListener("input", () => { draft().text = $("prompt").value; draft().blocked = false; resizePrompt(); updateComposer(); });
  $("prompt").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitTask(); } });
  $("send-task").addEventListener("click", submitTask);
  $("project-select").addEventListener("change", () => { if ($("project-select").value === "__add_project__") { $("project-select").value = draft().project; openProjects(); } else { draft().project = $("project-select").value; draft().followUp = null; draft().blocked = false; updateComposer(); } });
  $("node-select").addEventListener("change", () => { draft().target = $("node-select").value; draft().blocked = false; });
  $("role-select").addEventListener("change", () => { draft().role = $("role-select").value; draft().blocked = false; });
  $("clear-follow-up").addEventListener("click", () => { draft().followUp = null; updateComposer(); });
  document.querySelectorAll(".suggestion").forEach((button) => button.addEventListener("click", () => { draft().kind = button.dataset.kind; draft().text = button.dataset.prompt; if (button.dataset.role) draft().role = button.dataset.role; updateComposer(true); $("prompt").focus(); }));
  $("close-detail").addEventListener("click", closeDetail); $("drawer-backdrop").addEventListener("click", closeDetail);
  document.querySelectorAll("[data-detail-tab]").forEach((button) => button.addEventListener("click", () => { detail.tab = button.dataset.detailTab; renderDetail(); if (detail.tab === "patch") loadPatch(); }));
  $("detail-cancel").addEventListener("click", () => mutateJob("cancel")); $("detail-retry").addEventListener("click", () => mutateJob("retry"));
  $("detail-download").addEventListener("click", downloadPatch); $("detail-continue").addEventListener("click", () => { if (detail.job) continueFrom(detail.job); });
  $("manage-projects").addEventListener("click", openProjects); $("close-projects").addEventListener("click", () => $("project-dialog").close()); $("cancel-projects").addEventListener("click", () => $("project-dialog").close());
  $("project-form").addEventListener("submit", saveProject); $("browse-project").addEventListener("click", () => { window.webkit?.messageHandlers?.clusterNative?.postMessage(JSON.stringify({ action: "choose_folder" })); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && detail.id) { event.preventDefault(); closeDetail(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n" && !$("project-dialog").open) { event.preventDefault(); newTask(); }
    if (event.key === "Tab" && detail.id) {
      const nodes = Array.from($("detail-drawer").querySelectorAll("button:not([disabled]),a[href],input,select,textarea")).filter((node) => !node.hidden && node.getClientRects().length);
      if (nodes.length && event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes[nodes.length - 1].focus(); }
      else if (nodes.length && !event.shiftKey && document.activeElement === nodes[nodes.length - 1]) { event.preventDefault(); nodes[0].focus(); }
    }
  });
  if (window.innerWidth <= 980) toggleCluster(true);
  if (window.innerWidth <= 650) shell.classList.add("sidebar-hidden");
  render(); refresh();
})();
