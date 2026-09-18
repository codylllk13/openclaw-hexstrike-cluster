#!/usr/bin/env python3
"""Private localhost desktop console for the owner's compute cluster."""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

import clusterctl
from client import rpc


PORT = 18891
MAX_BODY = 256 * 1024
MAX_PATCH = 48 * 1024 * 1024
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
ROLES = {"build", "research", "review", "test", "compute"}
TARGETS = {"any", "workstation", "server"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


class APIError(Exception):
    def __init__(self, message, status=400, data=None):
        super().__init__(message)
        self.status = status
        self.data = {"error": message, **(data or {})}


def encode_json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def text(value, name, limit, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise APIError(f"{name} must be nonempty text without NUL characters")
    try:
        if len(value.encode("utf-8")) > limit:
            raise APIError(f"{name} exceeds its size limit")
    except UnicodeEncodeError:
        raise APIError(f"{name} contains invalid Unicode") from None
    return value


def identifier(value, name="job ID"):
    if not isinstance(value, str) or not JOB_ID.fullmatch(value):
        raise APIError(f"Invalid {name}")
    return value


def request_identifier(value):
    if not isinstance(value, str) or len(value) not in (32, 36):
        raise APIError("request_id must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise APIError("request_id must be a UUID") from None


def safe_job(job):
    if not isinstance(job, dict):
        raise APIError("Coordinator returned an invalid job", 502)
    result = dict(job)
    result.pop("lease_token", None)
    if isinstance(job.get("result"), dict):
        result["result"] = {k: v for k, v in job["result"].items()
                            if k not in {"patch_b64", "artifacts_b64"}}
    return result


class Store:
    """Each acknowledged job is committed immediately, before submitting the next."""

    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / "console.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    kind TEXT NOT NULL,text TEXT NOT NULL,project TEXT,target TEXT NOT NULL,
                    role TEXT NOT NULL,created_at REAL NOT NULL,error TEXT,submission_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turn_jobs (
                    turn_id TEXT NOT NULL REFERENCES turns(id),id TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(turn_id,id)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,action TEXT NOT NULL,fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,turn_id TEXT REFERENCES turns(id),
                    response_json TEXT,http_status INTEGER,error TEXT,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL
                );
            """)
            # A process crash leaves uncertain operations visible, never automatically replayed.
            error = "Console restarted during submission. Listed jobs remain valid; the last operation may also have reached the server. This request will not be resubmitted."
            db.execute("UPDATE turns SET error=?,submission_status='incomplete' WHERE id IN (SELECT turn_id FROM requests WHERE status='processing')", (error,))
            db.execute("UPDATE requests SET status='error',http_status=503,error=?,updated_at=? WHERE status='processing'", (error, time.time()))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def request(self, request_id, action, fingerprint):
        with self.db() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is not None and (row["action"] != action or row["fingerprint"] != fingerprint):
            raise APIError("request_id was already used for a different action", 409)
        return dict(row) if row else None

    def reserve_submission(self, request_id, fingerprint, payload):
        now = time.time()
        conversation_id = payload.get("conversation_id")
        turn_id = uuid.uuid4().hex
        with self.db() as db:
            if conversation_id:
                if not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
                    raise APIError("Conversation was not found", 404)
            else:
                conversation_id = uuid.uuid4().hex
                title = " ".join(payload["text"].split())[:100]
                db.execute("INSERT INTO conversations VALUES(?,?,?,?)", (conversation_id, title, now, now))
            db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,NULL,'submitting')", (
                turn_id, conversation_id, payload["kind"], payload["text"], payload.get("project"),
                payload["target"], payload["role"], now,
            ))
            db.execute("INSERT INTO requests(id,action,fingerprint,status,turn_id,created_at,updated_at) VALUES(?,?,?,'processing',?,?,?)", (request_id, "submit", fingerprint, turn_id, now, now))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        return turn_id

    def reserve_action(self, request_id, action, fingerprint, turn_id=None):
        now = time.time()
        with self.db() as db:
            db.execute("INSERT INTO requests(id,action,fingerprint,status,turn_id,created_at,updated_at) VALUES(?,?,?,'processing',?,?,?)", (request_id, action, fingerprint, turn_id, now, now))

    def add_job(self, turn_id, job):
        job_id = identifier(job.get("id"))
        role = (job.get("spec") or {}).get("role", "compute")
        if role not in ROLES:
            role = "compute"
        if turn_id is None:
            return
        now = time.time()
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO turn_jobs VALUES(?,?,?,?)", (turn_id, job_id, role, now))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=(SELECT conversation_id FROM turns WHERE id=?)", (now, turn_id))

    def update_project(self, turn_id, project):
        with self.db() as db:
            db.execute("UPDATE turns SET project=? WHERE id=?", (project, turn_id))

    def finish(self, request_id, *, response=None, error=None, http_status=200):
        now = time.time()
        with self.db() as db:
            db.execute("UPDATE requests SET status=?,response_json=?,http_status=?,error=?,updated_at=? WHERE id=?", ("error" if error else "done", encode_json(response) if response is not None else None, http_status, error, now, request_id))
            row = db.execute("SELECT turn_id,action FROM requests WHERE id=?", (request_id,)).fetchone()
            if row and row["turn_id"] and row["action"] == "submit":
                db.execute("UPDATE turns SET error=?,submission_status=? WHERE id=?", (error, "incomplete" if error else "submitted", row["turn_id"]))

    def conversations(self):
        with self.db() as db:
            conversations = [dict(row) for row in db.execute("SELECT * FROM conversations ORDER BY updated_at DESC,rowid DESC")]
            turns = [dict(row) for row in db.execute("SELECT * FROM turns ORDER BY created_at,rowid")]
            jobs = [dict(row) for row in db.execute("SELECT * FROM turn_jobs ORDER BY created_at,rowid")]
        by_turn = {}
        for job in jobs:
            by_turn.setdefault(job["turn_id"], []).append({"id": job["id"], "role": job["role"]})
        by_conversation = {}
        for turn in turns:
            conversation_id = turn.pop("conversation_id")
            turn["jobs"] = by_turn.get(turn["id"], [])
            by_conversation.setdefault(conversation_id, []).append(turn)
        for conversation in conversations:
            conversation["turns"] = by_conversation.get(conversation["id"], [])
        return conversations

    def submission(self, turn_id):
        for conversation in self.conversations():
            for turn in conversation["turns"]:
                if turn["id"] == turn_id:
                    return {"conversation": conversation, "turn": turn}
        raise APIError("Saved submission was not found", 500)

    def turn_for_job(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT turn_id FROM turn_jobs WHERE id=?", (job_id,)).fetchone()
        return row["turn_id"] if row else None


class ConsoleApp:
    def __init__(self, state=None, assets=None, rpc_fn=None, registry_fn=None,
                 register_fn=None, source_fn=None):
        state = state or os.environ.get("CLUSTER_STATE", "~/.local/state/compute-cluster")
        self.store = Store(state)
        self.assets = Path(assets or Path(__file__).with_name("desktop"))
        self.token = secrets.token_urlsafe(32)
        self.rpc = rpc_fn or rpc
        self.registry = registry_fn or clusterctl.registry
        self.register = register_fn or clusterctl.add_project
        self.source = source_fn or clusterctl.project_fields
        self.mutation_lock = threading.RLock()

    @staticmethod
    def _fingerprint(payload):
        return hashlib.sha256(encode_json(payload).encode()).hexdigest()

    def projects(self):
        return [{"name": name, "path": entry.get("path", ""),
                 "revision": entry.get("original_revision", entry.get("revision", ""))}
                for name, entry in sorted(self.registry().items())]

    def state(self):
        connected, error, nodes, jobs = True, None, [], []
        try:
            status = self.rpc("status")
            nodes = status["nodes"]
            jobs = [safe_job(job) for job in status["jobs"]]
        except (RuntimeError, OSError, KeyError, TypeError, APIError) as exc:
            connected = False
            error = str(exc) or "Coordinator is unavailable"
        return {"connected": connected, "error": error, "nodes": nodes, "jobs": jobs,
                "projects": self.projects(), "conversations": self.store.conversations()}

    def get_job(self, job_id):
        identifier(job_id)
        try:
            job = self.rpc("get", id=job_id)
        except RuntimeError as exc:
            raise APIError(str(exc), 503) from None
        return {"job": safe_job(job), "patch_available": bool((job.get("result") or {}).get("patch_b64"))}

    def patch(self, job_id):
        identifier(job_id)
        try:
            job = self.rpc("get", id=job_id)
        except RuntimeError as exc:
            raise APIError(str(exc), 503) from None
        encoded = (job.get("result") or {}).get("patch_b64")
        if not encoded:
            raise APIError("This job has no patch", 404)
        if not isinstance(encoded, str) or len(encoded) > ((MAX_PATCH + 2) // 3) * 4:
            raise APIError("Patch exceeds the download limit", 502)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise APIError("Coordinator returned an invalid patch", 502) from None
        if len(data) > MAX_PATCH:
            raise APIError("Patch exceeds the download limit", 502)
        return data

    @staticmethod
    def validate_submission(incoming):
        if not isinstance(incoming, dict):
            raise APIError("Submission must be a JSON object")
        allowed = {"conversation_id", "request_id", "kind", "text", "project", "target", "role", "timeout_seconds", "follow_up_to"}
        if set(incoming) - allowed:
            raise APIError("Submission contains unknown fields")
        request_id = request_identifier(incoming.get("request_id"))
        kind = incoming.get("kind")
        if not isinstance(kind, str) or kind not in {"team", "agent", "command"}:
            raise APIError("kind must be team, agent, or command")
        content = text(incoming.get("text"), "text", 48 * 1024 if kind != "command" else 16 * 1024)
        target = incoming.get("target", "any")
        role = incoming.get("role", "compute" if kind == "command" else "build")
        if not isinstance(target, str) or target not in TARGETS:
            raise APIError("Unknown target machine")
        if not isinstance(role, str) or role not in ROLES:
            raise APIError("Unknown specialist role")
        timeout = incoming.get("timeout_seconds", 1800)
        if type(timeout) is not int or not 30 <= timeout <= 14400:
            raise APIError("timeout_seconds must be between 30 and 14400")
        project = incoming.get("project") or None
        if project is not None and (not isinstance(project, str) or not PROJECT_NAME.fullmatch(project)):
            raise APIError("Invalid project name")
        conversation = incoming.get("conversation_id") or None
        if conversation:
            identifier(conversation, "conversation ID")
        follow_up = incoming.get("follow_up_to") or None
        if follow_up:
            identifier(follow_up)
        if kind != "command" and not project and not follow_up:
            raise APIError("Select a project before assigning an agent")
        return {"request_id": request_id, "kind": kind, "text": content,
                "target": target, "role": role, "timeout_seconds": timeout,
                "project": project, "conversation_id": conversation, "follow_up_to": follow_up}

    def _replay(self, record):
        if record["turn_id"] and record["action"] == "submit":
            data = self.store.submission(record["turn_id"])
        else:
            data = json.loads(record["response_json"]) if record["response_json"] else {}
        if record["status"] != "done":
            error = record["error"] or "This request is already being processed; it will not be resubmitted"
            raise APIError(error, record["http_status"] or 409, data)
        return data

    def _source_for(self, payload):
        parent_id = payload["follow_up_to"]
        if parent_id:
            parent = self.rpc("get", id=parent_id)
            if parent.get("status") != "succeeded":
                raise APIError("Follow-up work requires a successful parent job", 409)
            spec = parent.get("spec", {})
            fields = {key: spec.get(key) for key in ("project", "source_id", "revision")}
            if not all(fields.values()):
                raise APIError("The selected parent job has no project snapshot")
            if payload["project"] and payload["project"] != fields["project"]:
                raise APIError("Follow-up work must use its parent's project")
            return fields
        return self.source(payload["project"])

    def submit(self, incoming):
        payload = self.validate_submission(incoming)
        request_id = payload["request_id"]
        fingerprint = self._fingerprint(payload)
        with self.mutation_lock:
            existing = self.store.request(request_id, "submit", fingerprint)
            if existing:
                return self._replay(existing)
            turn_id = self.store.reserve_submission(request_id, fingerprint, payload)
            try:
                source = self._source_for(payload)
                self.store.update_project(turn_id, source.get("project"))
                common = {"timeout_seconds": payload["timeout_seconds"], **source}
                parent = payload["follow_up_to"]
                parent_dependencies = [parent] if parent else []

                def enqueue(role, prompt, target, dependencies, inherit):
                    job = self.rpc("submit", spec={**common, "kind": "agent", "title": role.capitalize() + ": " + payload["text"][:80],
                        "role": role, "target": target, "prompt": prompt + "\n" + payload["text"],
                        "depends_on": dependencies, "inherit_from": inherit})
                    self.store.add_job(turn_id, job)
                    return identifier(job.get("id"))

                if payload["kind"] == "team":
                    research = enqueue("research", "Inspect the project and propose a focused implementation and tests. Do not edit files.", "workstation", parent_dependencies, parent)
                    build_dependencies = list(dict.fromkeys([research, *parent_dependencies]))
                    build = enqueue("build", "Implement the objective using the research report in dependencies. Run focused tests.", "server", build_dependencies, parent)
                    enqueue("review", "Review the inherited implementation for correctness and regressions. Do not edit files. Report actionable findings and remaining risks.", "workstation", [build], build)
                    enqueue("test", "Test the inherited implementation with existing tests and focused behavioral checks. Do not modify source files. Report evidence and failures.", "server", [build], build)
                elif payload["kind"] == "agent":
                    enqueue(payload["role"], "Complete the assigned objective and report evidence, tests, and limitations.", payload["target"], parent_dependencies, parent)
                else:
                    job = self.rpc("submit", spec={**common, "kind": "command", "title": payload["text"][:100],
                        "role": payload["role"], "target": payload["target"], "argv": ["/bin/bash", "-lc", payload["text"]],
                        "depends_on": parent_dependencies, "inherit_from": parent})
                    self.store.add_job(turn_id, job)
                self.store.finish(request_id)
            except Exception as exc:
                # Even a timeout can mean the remote accepted a job. Never replay it blindly.
                detail = str(exc) if isinstance(exc, (APIError, RuntimeError, ValueError)) else "Submission could not be completed"
                error = detail + ". Listed jobs remain valid. This request will not be automatically resubmitted."
                code = exc.status if isinstance(exc, APIError) else (400 if isinstance(exc, ValueError) else 502)
                self.store.finish(request_id, error=error, http_status=code)
                raise APIError(error, code, self.store.submission(turn_id)) from None
            return self.store.submission(turn_id)

    def cancel(self, job_id, body):
        identifier(job_id)
        if not isinstance(body, dict) or set(body) - {"request_id"}:
            raise APIError("Invalid cancellation request")
        with self.mutation_lock:
            try:
                return {"job": safe_job(self.rpc("cancel", id=job_id))}
            except RuntimeError as exc:
                raise APIError(str(exc), 503) from None

    def retry(self, job_id, body):
        identifier(job_id)
        if not isinstance(body, dict) or set(body) - {"request_id"}:
            raise APIError("Invalid retry request")
        # Older callers without a nonce get one retry per original job, not duplicates.
        request_id = request_identifier(body["request_id"]) if "request_id" in body else "retry:" + job_id
        fingerprint = self._fingerprint({"job": job_id})
        with self.mutation_lock:
            existing = self.store.request(request_id, "retry", fingerprint)
            if existing:
                return self._replay(existing)
            turn_id = self.store.turn_for_job(job_id)
            self.store.reserve_action(request_id, "retry", fingerprint, turn_id)
            try:
                job = self.rpc("retry", id=job_id)
                self.store.add_job(turn_id, job)
                response = {"job": safe_job(job)}
                self.store.finish(request_id, response=response)
                return response
            except Exception as exc:
                detail = str(exc) if isinstance(exc, (RuntimeError, APIError, ValueError)) else "Retry could not be completed"
                error = detail + ". The retry may have reached the server; this request will not be resubmitted."
                self.store.finish(request_id, error=error, http_status=502)
                raise APIError(error, 502) from None

    def add_project(self, body):
        if not isinstance(body, dict) or set(body) != {"name", "path"}:
            raise APIError("Provide a project name and path")
        name = text(body["name"], "name", 64)
        if not PROJECT_NAME.fullmatch(name):
            raise APIError("Invalid project name")
        path = text(body["path"], "path", 4096)
        with self.mutation_lock:
            try:
                with redirect_stdout(io.StringIO()):
                    self.register(name, path)
                return next(project for project in self.projects() if project["name"] == name)
            except ValueError as exc:
                raise APIError(str(exc)) from None
            except RuntimeError as exc:
                raise APIError(str(exc), 503) from None
            except (OSError, subprocess.CalledProcessError, StopIteration):
                raise APIError("Project could not be registered. Check that the path is a clean Git repository.") from None


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, port=PORT):
        self.app = app
        super().__init__(("127.0.0.1", port), ConsoleHandler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)
        self.expected_host = "127.0.0.1:" + str(self.server_port)


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "ClusterConsole"

    def log_message(self, _format, *_args):
        pass  # Request bodies, prompts, job IDs, and tokens do not enter access logs.

    def _send(self, status, data, content_type="application/json; charset=utf-8", disposition=None):
        raw = data if isinstance(data, bytes) else encode_json(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(raw)

    def _validate_request(self, *, api=False, mutation=False):
        if self.client_address[0] != "127.0.0.1":
            raise APIError("Only local connections are accepted", 403)
        hosts = self.headers.get_all("Host", [])
        if hosts != [self.server.expected_host]:
            raise APIError("Unexpected Host header", 403)
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            raise APIError("Unexpected Origin header", 403)
        if mutation and origins != [self.server.origin]:
            raise APIError("A same-origin request is required", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError("Cross-site requests are refused", 403)
        if api:
            tokens = self.headers.get_all("X-Cluster-Token", [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0], self.server.app.token):
                raise APIError("Missing or invalid console token", 403)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError("Transfer-Encoding is not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise APIError("Content-Length is required", 411)
        length = int(lengths[0])
        if length > MAX_BODY:
            raise APIError("Request body exceeds 256 KiB", 413)
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise APIError("Use application/json", 415)
        self.connection.settimeout(10)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise APIError("Incomplete request body")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise APIError("Request body must be valid JSON") from None
        if not isinstance(body, dict):
            raise APIError("Request body must be a JSON object")
        return body

    def _path(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            raise APIError("Absolute request URLs are not accepted")
        return parsed.path

    def _dispatch(self, mutation=False):
        path = self._path()
        api = path.startswith("/api/")
        self._validate_request(api=api, mutation=mutation)
        if mutation:
            body = self._body()
            if path == "/api/submit":
                self._send(200, self.server.app.submit(body))
            elif path == "/api/projects":
                self._send(200, self.server.app.add_project(body))
            elif match := re.fullmatch(r"/api/jobs/([0-9a-f]{32})/(cancel|retry)", path):
                action = getattr(self.server.app, match[2])
                self._send(200, action(match[1], body))
            else:
                raise APIError("Endpoint not found", 404)
            return
        if path == "/api/state":
            self._send(200, self.server.app.state())
        elif match := re.fullmatch(r"/api/jobs/([0-9a-f]{32})/patch", path):
            self._send(200, self.server.app.patch(match[1]), "application/octet-stream", 'attachment; filename="' + match[1] + '.patch"')
        elif match := re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path):
            self._send(200, self.server.app.get_job(match[1]))
        elif path in {"/", "/index.html", "/app.css", "/app.js"}:
            name = "index.html" if path in {"/", "/index.html"} else path[1:]
            try:
                raw = (self.server.app.assets / name).read_bytes()
            except OSError:
                raise APIError("Desktop assets are not installed", 503) from None
            content_type = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}[name]
            if name == "index.html":
                raw = raw.replace(b"__CLUSTER_TOKEN__", self.server.app.token.encode())
                if b'name="cluster-token"' not in raw:
                    raw = raw.replace(b"<head>", b'<head><meta name="cluster-token" content="' + self.server.app.token.encode() + b'">', 1)
            self._send(200, raw, content_type)
        else:
            raise APIError("Endpoint not found", 404)

    def _handle(self, mutation=False):
        try:
            self._dispatch(mutation)
        except APIError as exc:
            self._send(exc.status, exc.data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._send(500, {"error": "The local console could not complete this request"})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(mutation=True)

    def do_OPTIONS(self):
        self._send(403, {"error": "Cross-origin access is not available"})


def main():
    os.umask(0o077)
    server = ConsoleServer(ConsoleApp())
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
