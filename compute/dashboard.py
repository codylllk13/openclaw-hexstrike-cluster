#!/usr/bin/env python3
"""Read-only, loopback dashboard for the private compute queue."""

from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
import re
import time
from urllib.parse import quote, unquote, urlsplit

import client


HOST = "127.0.0.1"
PORT = 18890
REFRESH_SECONDS = 8
KNOWN_STATES = {
    "queued", "running", "cancel_requested", "succeeded", "failed",
    "cancelled", "interrupted", "blocked",
}
CSS = """
:root { color-scheme: dark; font: 16px/1.5 system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; background: #101820; color: #e7edf3; }
main { max-width: 1200px; margin: auto; padding: 32px 24px 60px; }
h1 { margin: 0; font-size: 2rem; letter-spacing: -.04em; }
h2 { font-size: 1.15rem; margin: 0 0 16px; }
p { margin: 8px 0; }
a { color: #98d7ff; text-decoration-thickness: 1px; text-underline-offset: 3px; }
header { margin-bottom: 28px; }
.muted { color: #a8b7c6; }
.cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap: 16px; }
.card, section { border: 1px solid #30404f; border-radius: 12px; background: #17232e; padding: 20px; }
section { margin-top: 24px; }
.card h2 { margin-bottom: 8px; overflow-wrap: anywhere; }
.badge { display: inline-block; padding: 3px 9px; font-size: .82rem; border-radius: 20px; background: #314150; }
.online, .succeeded { background: #174b40; color: #a5f0d9; }
.running { background: #17476a; color: #b1e2ff; }
.stale, .interrupted, .blocked, .cancel_requested { background: #61481f; color: #ffdda1; }
.failed { background: #602e36; color: #ffc4cd; }
.stats { display: flex; gap: 24px; flex-wrap: wrap; margin-top: 20px; }
.stats strong { font-size: 1.5rem; padding-right: 5px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th { color: #a8b7c6; font-weight: 500; font-size: .84rem; }
td, th { padding: 12px 10px; border-bottom: 1px solid #30404f; vertical-align: top; }
td:first-child, th:first-child { padding-left: 0; }
tr:last-child td { border-bottom: 0; }
.job-title { min-width: 180px; overflow-wrap: anywhere; }
.small { font-size: .83rem; }
.nowrap { white-space: nowrap; }
dl { display: grid; grid-template-columns: minmax(110px, 180px) 1fr; gap: 10px 16px; }
dt { color: #a8b7c6; }
dd { margin: 0; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; margin: 0; }
.notice { border-left: 3px solid #7abce6; padding-left: 14px; }
footer { margin-top: 28px; font-size: .85rem; color: #a8b7c6; }
@media(max-width:600px) { main { padding: 22px 14px; } section,.card { padding: 16px; } dl { grid-template-columns: 1fr; gap: 2px; } dd { margin-bottom: 10px; } }
"""


def e(value):
    """Escape all coordinator-provided text before rendering it."""
    return escape(str(value), quote=True)


def timestamp(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def when(value):
    stamp = timestamp(value)
    if stamp is None:
        return "—"
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%b %d, %H:%M:%S UTC")
    except (ValueError, OverflowError, OSError):
        return "—"


def elapsed(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
        return "—"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def duration(job, now):
    result = job.get("result") or {}
    seconds = result.get("duration_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds):
        return elapsed(seconds)
    start = timestamp(job.get("started_at"))
    finish = timestamp(job.get("finished_at"))
    if start is None:
        return "—"
    if finish is not None:
        return elapsed(finish - start)
    if job.get("status") in {"running", "cancel_requested"}:
        return elapsed(now - start)
    return "—"


def badge(state):
    label = str(state or "unknown")
    css_class = label if label in KNOWN_STATES | {"online", "stale"} else "unknown"
    return f'<span class="badge {css_class}">{e(label.replace("_", " "))}</span>'


def job_link(job_id, label=None):
    return f'<a href="/jobs/{quote(str(job_id), safe="")}">{e(label if label is not None else job_id)}</a>'


def page(title, content):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>{e(title)} · Compute cluster</title><style>{CSS}</style></head>
<body><main>{content}
<footer>Read-only dashboard · Refreshes every {REFRESH_SECONDS} seconds · Times shown in UTC</footer>
</main></body></html>"""


def overview(data):
    now = time.time()
    nodes = data.get("nodes", [])
    jobs = data.get("jobs", [])
    cards = []
    for node in nodes:
        name = node.get("node", "Unknown worker")
        online = node.get("online") is True
        info = node.get("info") or {}
        cpu = info.get("cpu_count")
        ram = info.get("memory_total_mb")
        resources = []
        if isinstance(cpu, int) and cpu > 0:
            resources.append(f"{cpu} logical CPUs")
        if isinstance(ram, (int, float)) and math.isfinite(ram) and ram > 0:
            resources.append(f"{ram / 1024:.1f} GiB RAM")
        active = node.get("active_job")
        work = "Current job: " + job_link(active) if active else "Ready for jobs" if online else "Waiting for worker"
        seen = "Never connected" if node.get("last_seen") is None else "Last seen " + when(node.get("last_seen"))
        cards.append(f"""<article class="card"><h2>{e(name)}</h2>
{badge('online' if online else 'stale')}
<p>{work}</p><p class="muted small">{e(' · '.join(resources) or 'Resource details unavailable')}</p>
<p class="muted small">{e(seen)}</p></article>""")
    rows = []
    for job in jobs:
        spec = job.get("spec") or {}
        job_id = job.get("id", "")
        rows.append(f"""<tr><td class="job-title">{job_link(job_id, spec.get('title') or job_id)}
<div class="small muted">{e(job_id)}</div></td>
<td>{badge(job.get('status'))}</td><td>{e(spec.get('target', 'any'))}</td>
<td>{e(job.get('node') or '—')}</td><td class="nowrap">{e(duration(job, now))}</td>
<td class="small nowrap">{e(when(job.get('created_at')))}</td></tr>""")
    active_count = sum(job.get("status") in {"running", "cancel_requested"} for job in jobs)
    queued_count = sum(job.get("status") == "queued" for job in jobs)
    success_count = sum(job.get("status") == "succeeded" for job in jobs)
    table = f"""<div class="table-wrap"><table><thead><tr>
<th>Job</th><th>State</th><th>Target</th><th>Worker</th><th>Run time</th><th>Submitted</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>""" if rows else '<p class="muted">No jobs yet. Submit a task with clusterctl to get started.</p>'
    return page("Overview", f"""<header><h1>Your compute cluster</h1>
<p class="muted">Two machines, one place to follow the work.</p>
<div class="stats"><span><strong>{active_count}</strong> active</span>
<span><strong>{queued_count}</strong> queued</span><span><strong>{success_count}</strong> succeeded</span></div></header>
<div class="cards">{''.join(cards) or '<p class="muted">No workers have been configured.</p>'}</div>
<section><h2>Recent jobs</h2>{table}
<p class="muted small">Shows up to 100 recent jobs. Each worker uses its own CPU and memory.</p></section>""")


def job_detail(job):
    spec = job.get("spec") or {}
    result = job.get("result") or {}
    title = spec.get("title") or job.get("id", "Job")
    fields = [
        ("Job ID", job.get("id", "—")),
        ("Kind / role", f"{spec.get('kind', '—')} / {spec.get('role', '—')}"),
        ("Target", spec.get("target", "any")),
        ("Worker", job.get("node") or "Not assigned"),
        ("Project", spec.get("project") or "No source project"),
        ("Submitted", when(job.get("created_at"))),
        ("Started", when(job.get("started_at"))),
        ("Finished", when(job.get("finished_at"))),
        ("Run time", duration(job, time.time())),
    ]
    if result.get("exit_code") is not None:
        fields.append(("Exit code", result["exit_code"]))
    details = ''.join(f"<dt>{e(label)}</dt><dd>{e(value)}</dd>" for label, value in fields)
    dependencies = spec.get("depends_on") or []
    if dependencies:
        details += '<dt>Depends on</dt><dd>' + ', '.join(job_link(value) for value in dependencies) + '</dd>'
    summary = result.get("summary") or "No result yet."
    # Full logs, patches, prompts, commands, and artifacts are intentionally not rendered.
    summary = str(summary)
    if len(summary) > 16000:
        summary = summary[:16000] + "\n\n[Summary shortened for this view.]"
    error = f'<p class="notice">{e(str(job["error"])[:4000])}</p>' if job.get("error") else ""
    return page(title, f"""<header><p><a href="/">← All jobs</a></p>
<h1>{e(title)}</h1><p>{badge(job.get('status'))}</p></header>
<section><h2>Job details</h2><dl>{details}</dl>{error}</section>
<section><h2>Result summary</h2><pre>{e(summary)}</pre></section>
<p class="muted small">Use clusterctl for full logs, patches, cancellation, or retry.</p>""")


class Handler(BaseHTTPRequestHandler):
    server_version = "ComputeCluster"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Avoid persisting job identifiers or request paths in service logs.
        return

    def respond(self, status, body, *, head=False):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def handle_read(self, head=False):
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
        except ValueError:
            self.respond(400, page("Invalid request", '<h1>Invalid request</h1><p><a href="/">Return to overview</a></p>'), head=head)
            return
        if path == "/":
            action, kwargs = "status", {}
        elif re.fullmatch(r"/jobs/[A-Za-z0-9][A-Za-z0-9_-]{0,127}", path):
            action, kwargs = "get", {"id": path.removeprefix("/jobs/")}
        else:
            self.respond(404, page("Not found", '<h1>Page not found</h1><p><a href="/">Return to overview</a></p>'), head=head)
            return
        try:
            data = client.rpc(action, **kwargs)
            if not isinstance(data, dict):
                raise RuntimeError("Invalid coordinator response")
            content = overview(data) if action == "status" else job_detail(data)
        except (RuntimeError, OSError, ValueError, TypeError, KeyError):
            # RPC failures may contain private connection details; keep these off the page.
            content = page("Temporarily unavailable", '<header><h1>Unable to load this view</h1><p class="muted">The coordinator may be reconnecting, or this job may no longer exist. This page will retry automatically.</p><p><a href="/">Return to overview</a></p></header>')
            self.respond(503, content, head=head)
            return
        self.respond(200, content, head=head)

    def do_GET(self):
        self.handle_read()

    def do_HEAD(self):
        self.handle_read(head=True)

    def reject_write(self):
        self.respond(405, page("Read-only dashboard", '<h1>This dashboard is read-only</h1><p>Use clusterctl to manage jobs.</p>'))

    do_POST = reject_write
    do_PUT = reject_write
    do_PATCH = reject_write
    do_DELETE = reject_write
    do_OPTIONS = reject_write


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"Read-only compute dashboard listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
