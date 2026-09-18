#!/usr/bin/env python3
"""Private, durable single-owner compute queue. No network listener is opened."""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid


MAX_BLOB = 48 * 1024 * 1024
MAX_RPC = 72 * 1024 * 1024
MAX_SPEC = 256 * 1024
MAX_LOG = 1024 * 1024
MAX_SUMMARY = 64 * 1024
MAX_PROGRESS_LOG = 32 * 1024
ID_RE = re.compile(r"[0-9a-f]{32}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
REVISION_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
CAPABILITIES = {"command", "agent"}
ROLES = {"research", "build", "review", "test", "compute"}
ACTIVE = {"running", "cancel_requested"}
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "blocked"}
JOB_COLUMNS = (
    "id,spec_json,status,node,created_at,started_at,finished_at,"
    "lease_expires_at,error,retry_of,progress_json"
)


class QueueError(ValueError):
    """A safe, user-readable queue or validation error."""


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _string(value, label, limit, *, allow_empty=False):
    if not isinstance(value, str) or "\x00" in value:
        raise QueueError(f"{label} must be text without NUL characters")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise QueueError(f"{label} contains invalid Unicode") from None
    if size > limit or (not allow_empty and not value.strip()):
        raise QueueError(f"{label} is empty or exceeds its size limit")
    return value


def _identifier(value, label, pattern=ID_RE):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise QueueError(f"Invalid {label}")
    return value


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise QueueError(f"{label} must be an integer between {low} and {high}")
    return value


def _number(value, label, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise QueueError(f"{label} must be a finite number between {low} and {high}")
    return value


def _decode_blob(value, label, limit=None):
    limit = MAX_BLOB if limit is None else limit
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4:
        raise QueueError(f"{label} is invalid or too large")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise QueueError(f"{label} must be valid base64") from None
    if len(raw) > limit:
        raise QueueError(f"{label} is too large")
    return raw


def _load_config():
    path = Path(os.environ.get("CLUSTER_CONFIG", "~/.config/compute-cluster/config.json")).expanduser()
    try:
        with path.open("rb") as handle:
            data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise QueueError("Coordinator configuration is too large")
        config = json.loads(data)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raise QueueError("Coordinator configuration is missing or invalid") from None
    if not isinstance(config, dict):
        raise QueueError("Coordinator configuration must be an object")
    return config


class Coordinator:
    def __init__(self, config=None, state=None, clock=time.time):
        self.config = _load_config() if config is None else config
        self.clock = clock
        configured = self.config.get("nodes")
        if configured is None:
            configured = [self.config.get("node_id")]
        if isinstance(configured, dict):
            configured = list(configured)
        if not isinstance(configured, list) or not configured or len(configured) > 64:
            raise QueueError("Configure a nonempty list of node IDs")
        self.nodes = tuple(dict.fromkeys(_identifier(n, "configured node", NODE_RE) for n in configured))
        if "any" in self.nodes:
            raise QueueError("The node name 'any' is reserved")
        self.lease_seconds = _integer(self.config.get("lease_seconds", 90), "lease_seconds", 30, 3600)
        self.state = Path(state or os.environ.get("CLUSTER_STATE", "~/.local/state/compute-cluster")).expanduser()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.blobs = self.state / "blobs"
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        self.blobs.chmod(0o700)
        db_path = self.state / "queue.sqlite3"
        # SQLite's WAL and SHM inherit the database mode, not just the directory mode.
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db_path.chmod(0o600)
        self.db = sqlite3.connect(db_path, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                node TEXT PRIMARY KEY,
                capabilities_json TEXT NOT NULL,
                info_json TEXT NOT NULL,
                last_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT NOT NULL,
                source_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(name, source_id, revision)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                status TEXT NOT NULL,
                node TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                lease_token TEXT,
                lease_expires_at REAL,
                result_json TEXT,
                result_brief_json TEXT,
                progress_json TEXT,
                error TEXT,
                retry_of TEXT REFERENCES jobs(id)
            );
            CREATE TABLE IF NOT EXISTS dependencies (
                job_id TEXT NOT NULL REFERENCES jobs(id),
                dependency_id TEXT NOT NULL REFERENCES jobs(id),
                PRIMARY KEY(job_id, dependency_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_node
                ON jobs(node) WHERE status IN ('running', 'cancel_requested');
            CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
            CREATE INDEX IF NOT EXISTS dependency_parent ON dependencies(dependency_id);
        """)
        # Existing queues are upgraded in place. The write lock also prevents two
        # simultaneous RPC processes racing the first migration.
        with self.transaction():
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
            if "progress_json" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN progress_json TEXT")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _node(self, value):
        value = _identifier(value, "node", NODE_RE)
        if value not in self.nodes:
            raise QueueError("Node is not configured")
        return value

    def _capabilities(self, value):
        if (not isinstance(value, list) or not value or len(value) > 2
                or any(not isinstance(v, str) or v not in CAPABILITIES for v in value)
                or len(set(value)) != len(value)):
            raise QueueError("capabilities must contain command and/or agent")
        return sorted(value)

    def _block_dependents(self, now):
        # Repeated updates propagate failure across any depth without recursive Python.
        while True:
            changed = self.db.execute("""
                UPDATE jobs SET status='blocked', finished_at=?,
                    error='A dependency did not succeed; no work was executed'
                WHERE status='queued' AND EXISTS (
                    SELECT 1 FROM dependencies d JOIN jobs parent ON parent.id=d.dependency_id
                    WHERE d.job_id=jobs.id AND parent.status IN
                        ('failed','cancelled','interrupted','blocked')
                )
            """, (now,)).rowcount
            if not changed:
                break

    def _expire(self, now):
        self.db.execute("""
            UPDATE jobs SET status='interrupted', finished_at=?,
                error='Worker lease expired; job was not automatically retried'
            WHERE status IN ('running','cancel_requested') AND lease_expires_at<=?
        """, (now, now))
        self._block_dependents(now)

    def _row(self, job_id, *, full=True):
        job_id = _identifier(job_id, "job ID")
        result_col = "result_json" if full else "result_brief_json"
        row = self.db.execute(f"SELECT {JOB_COLUMNS},{result_col} AS result_data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise QueueError("Unknown job ID")
        return row

    @staticmethod
    def _record(row, *, progress_log=True):
        progress = json.loads(row["progress_json"]) if row["progress_json"] is not None else None
        if progress is not None and not progress_log:
            progress = {"updated_at": progress["updated_at"]}
        return {
            "id": row["id"], "spec": json.loads(row["spec_json"]),
            "status": row["status"], "node": row["node"],
            "created_at": row["created_at"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "lease_expires_at": row["lease_expires_at"],
            "error": row["error"], "retry_of": row["retry_of"],
            "result": json.loads(row["result_data"]) if row["result_data"] is not None else None,
            "progress": progress,
        }

    def _validate_spec(self, incoming):
        if not isinstance(incoming, dict):
            raise QueueError("spec must be an object")
        allowed = {"kind", "title", "target", "project", "source_id", "revision", "argv", "prompt", "role", "depends_on", "inherit_from", "timeout_seconds"}
        if set(incoming) - allowed:
            raise QueueError("spec contains unknown fields")
        try:
            spec_length = len(_json(incoming).encode())
        except (TypeError, ValueError, OverflowError):
            raise QueueError("spec must contain valid JSON values") from None
        if spec_length > MAX_SPEC:
            raise QueueError("spec exceeds its size limit")
        kind = incoming.get("kind")
        if not isinstance(kind, str) or kind not in CAPABILITIES:
            raise QueueError("kind must be command or agent")
        target = incoming.get("target", "any")
        if target != "any":
            self._node(target)
        role = incoming.get("role", "build" if kind == "agent" else "compute")
        if not isinstance(role, str) or role not in ROLES:
            raise QueueError("Invalid specialist role")
        spec = {
            "kind": kind, "title": _string(incoming.get("title"), "title", 512),
            "target": target, "role": role,
            "timeout_seconds": _integer(incoming.get("timeout_seconds", 3600), "timeout_seconds", 30, 14400),
            "project": incoming.get("project"), "source_id": incoming.get("source_id"),
            "revision": incoming.get("revision"),
        }
        source_fields = [spec[key] is not None for key in ("project", "source_id", "revision")]
        if any(source_fields) and not all(source_fields):
            raise QueueError("project, source_id, and revision must be provided together")
        if all(source_fields):
            _identifier(spec["project"], "project name", NAME_RE)
            _identifier(spec["source_id"], "source hash", HASH_RE)
            spec["revision"] = _identifier(spec["revision"], "Git revision", REVISION_RE).lower()
            if self.db.execute("SELECT 1 FROM projects WHERE name=? AND source_id=? AND revision=?", (spec["project"], spec["source_id"], spec["revision"])).fetchone() is None:
                raise QueueError("Project source/revision has not been registered")
            if not (self.blobs / spec["source_id"]).is_file():
                raise QueueError("Project source bundle is unavailable")
        if kind == "agent":
            if not all(source_fields):
                raise QueueError("Agent jobs require a project")
            if "argv" in incoming:
                raise QueueError("Agent jobs cannot contain argv")
            spec["prompt"] = _string(incoming.get("prompt"), "prompt", 64 * 1024)
        else:
            if "prompt" in incoming:
                raise QueueError("Command jobs cannot contain prompt")
            argv = incoming.get("argv")
            if not isinstance(argv, list) or not argv or len(argv) > 256:
                raise QueueError("argv must be a nonempty list of at most 256 strings")
            spec["argv"] = [_string(arg, "argv element", 16 * 1024, allow_empty=True) for arg in argv]
            if not spec["argv"][0].strip() or spec["argv"][0].startswith("-"):
                raise QueueError("argv must start with an executable name")
        dependencies = incoming.get("depends_on", [])
        if not isinstance(dependencies, list) or len(dependencies) > 100:
            raise QueueError("depends_on must contain at most 100 job IDs")
        for parent in dependencies:
            self._row(parent, full=False)
        if len(set(dependencies)) != len(dependencies):
            raise QueueError("Duplicate dependencies are not allowed")
        spec["depends_on"] = dependencies
        spec["inherit_from"] = incoming.get("inherit_from")
        if spec["inherit_from"] is not None:
            _identifier(spec["inherit_from"], "inherited job ID")
            if spec["inherit_from"] not in dependencies:
                raise QueueError("inherit_from must be one of depends_on")
            if spec["project"] is None:
                raise QueueError("Patch inheritance requires a project")
            parent_spec = json.loads(self._row(spec["inherit_from"], full=False)["spec_json"])
            if any(parent_spec[key] != spec[key] for key in ("project", "source_id", "revision")):
                raise QueueError("Inherited job must use the same project source and revision")
        return spec

    def _submit(self, spec, now, retry_of=None):
        spec = self._validate_spec(spec)
        job_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO jobs(id,spec_json,status,created_at,retry_of) VALUES(?,?,'queued',?,?)", (job_id, _json(spec), now, retry_of))
        self.db.executemany("INSERT INTO dependencies(job_id,dependency_id) VALUES(?,?)", [(job_id, parent) for parent in spec["depends_on"]])
        self._block_dependents(now)
        return self._record(self._row(job_id))

    def _touch_node(self, node, capabilities, now, info=None):
        self.db.execute("""
            INSERT INTO nodes(node,capabilities_json,info_json,last_seen) VALUES(?,?,?,?)
            ON CONFLICT(node) DO UPDATE SET
                capabilities_json=excluded.capabilities_json,
                info_json=CASE WHEN ? THEN excluded.info_json ELSE nodes.info_json END,
                last_seen=excluded.last_seen
        """, (node, _json(capabilities), _json(info or {}), now, info is not None))

    def _status(self, now):
        nodes = []
        for node in self.nodes:
            row = self.db.execute("SELECT * FROM nodes WHERE node=?", (node,)).fetchone()
            active = self.db.execute("SELECT id FROM jobs WHERE node=? AND status IN ('running','cancel_requested')", (node,)).fetchone()
            nodes.append({
                "node": node, "capabilities": json.loads(row["capabilities_json"]) if row else [],
                "info": json.loads(row["info_json"]) if row else {},
                "last_seen": row["last_seen"] if row else None,
                "online": bool(row and now - row["last_seen"] < self.lease_seconds),
                "active_job": active["id"] if active else None,
            })
        rows = self.db.execute(f"SELECT {JOB_COLUMNS},result_brief_json AS result_data FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 100").fetchall()
        return {"nodes": nodes, "jobs": [self._record(row, progress_log=False) for row in rows]}

    def _heartbeat(self, request, now):
        node = self._node(request.get("node"))
        capabilities = self._capabilities(request.get("capabilities"))
        info = request.get("info", {})
        bounds = {"cpu_percent": 100, "memory_used_mb": 1000000000,
                  "disk_total_gb": 1000000000, "disk_used_gb": 1000000000,
                  "load_1": 1000000, "worker_memory_mb": 1000000000,
                  "worker_cpu_percent": 1000000}
        if not isinstance(info, dict) or set(info) - {"cpu_count", "memory_total_mb"} - bounds.keys():
            raise QueueError("info contains an unsupported metric")
        for key in info:
            if key in bounds:
                _number(info[key], key, 0, bounds[key])
            else:
                _integer(info[key], key, 1, 1000000000)
        for used, total in (("memory_used_mb", "memory_total_mb"), ("disk_used_gb", "disk_total_gb")):
            if used in info and total in info and info[used] > info[total]:
                raise QueueError(f"{used} cannot exceed {total}")
        active = request.get("active_job")
        token = request.get("lease_token")
        if (active is None) != (token is None):
            raise QueueError("active_job and lease_token must be supplied together")
        progress = request.get("progress")
        if progress is not None:
            if active is None:
                raise QueueError("progress requires an active job lease")
            if not isinstance(progress, dict) or set(progress) != {"log", "updated_at"}:
                raise QueueError("progress must contain log and updated_at")
            _string(progress["log"], "progress log", MAX_PROGRESS_LOG, allow_empty=True)
            _number(progress["updated_at"], "progress updated_at", 0, 1000000000000)
        self._touch_node(node, capabilities, now, info)
        if active is None:
            return {"cancel": False, "lease_valid": True}
        _identifier(active, "active job ID")
        _identifier(token, "lease token", HASH_RE)
        row = self.db.execute("SELECT status,node,lease_token FROM jobs WHERE id=?", (active,)).fetchone()
        valid = bool(row and row["status"] in ACTIVE and row["node"] == node and row["lease_token"] == token)
        if valid:
            self.db.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (now + self.lease_seconds, active))
            if progress is not None:
                self.db.execute("UPDATE jobs SET progress_json=? WHERE id=?", (_json(progress), active))
        return {"cancel": bool(valid and row["status"] == "cancel_requested"), "lease_valid": valid}

    def _claim(self, request, now):
        node = self._node(request.get("node"))
        capabilities = self._capabilities(request.get("capabilities"))
        self._touch_node(node, capabilities, now)
        if self.db.execute("SELECT 1 FROM jobs WHERE node=? AND status IN ('running','cancel_requested')", (node,)).fetchone():
            return None
        # Specs are bounded, and eligibility is checked inside the write transaction.
        candidates = self.db.execute("""
            SELECT id,spec_json FROM jobs WHERE status='queued' AND NOT EXISTS (
                SELECT 1 FROM dependencies d JOIN jobs parent ON parent.id=d.dependency_id
                WHERE d.job_id=jobs.id AND parent.status!='succeeded'
            ) ORDER BY created_at,rowid
        """)
        selected = None
        for row in candidates:
            spec = json.loads(row["spec_json"])
            if spec["target"] in ("any", node) and spec["kind"] in capabilities:
                selected = (row["id"], spec)
                break
        if selected is None:
            return None
        job_id, spec = selected
        token = uuid.uuid4().hex + uuid.uuid4().hex
        self.db.execute("UPDATE jobs SET status='running',node=?,started_at=?,lease_token=?,lease_expires_at=? WHERE id=?", (node, now, token, now + self.lease_seconds, job_id))
        record = self._record(self._row(job_id))
        record["lease_token"] = token
        record["dependencies"] = [
            {"id": parent, "result": self._record(self._row(parent))["result"]}
            for parent in spec["depends_on"]
        ]
        return record

    def _validate_result(self, result):
        if not isinstance(result, dict):
            raise QueueError("result must be an object")
        required = {"exit_code", "summary", "duration_seconds", "log", "patch_b64"}
        if set(result) - required - {"artifacts_b64"} or not required <= set(result):
            raise QueueError("result has missing or unknown fields")
        _integer(result["exit_code"], "exit_code", -65535, 65535)
        _string(result["summary"], "summary", MAX_SUMMARY, allow_empty=True)
        _string(result["log"], "log", MAX_LOG, allow_empty=True)
        duration = result["duration_seconds"]
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= 7 * 86400:
            raise QueueError("duration_seconds must be a finite nonnegative duration")
        patch = _decode_blob(result["patch_b64"], "patch_b64")
        if "artifacts_b64" in result:
            _decode_blob(result["artifacts_b64"], "artifacts_b64", MAX_BLOB - len(patch))
        return dict(result)

    def _complete(self, request, now):
        node = self._node(request.get("node"))
        job_id = _identifier(request.get("id"), "job ID")
        token = _identifier(request.get("lease_token"), "lease token", HASH_RE)
        row = self.db.execute("SELECT status,node,lease_token,result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["node"] != node or row["lease_token"] != token:
            raise QueueError("Invalid job lease")
        result = self._validate_result(request.get("result"))
        result_json = _json(result)
        if row["status"] in TERMINAL:
            if row["result_json"] is not None and json.loads(row["result_json"]) == result:
                return self._record(self._row(job_id))
            raise QueueError("Job lease is no longer active or completion differs")
        if row["status"] not in ACTIVE:
            raise QueueError("Job lease is not active")
        status = "cancelled" if row["status"] == "cancel_requested" else ("succeeded" if result["exit_code"] == 0 else "failed")
        brief = {key: result[key] for key in ("exit_code", "summary", "duration_seconds")}
        self.db.execute("UPDATE jobs SET status=?,finished_at=?,result_json=?,result_brief_json=?,lease_expires_at=NULL WHERE id=?", (status, now, result_json, _json(brief), job_id))
        self._block_dependents(now)
        return self._record(self._row(job_id))

    def _project_put(self, request, now):
        name = _identifier(request.get("name"), "project name", NAME_RE)
        revision = _identifier(request.get("revision"), "Git revision", REVISION_RE).lower()
        raw = _decode_blob(request.get("bundle_b64"), "bundle_b64")
        if not raw.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n")):
            raise QueueError("Source must be a Git bundle")
        source_id = hashlib.sha256(raw).hexdigest()
        destination = self.blobs / source_id
        if not destination.exists():
            temporary = self.blobs / (".upload-" + uuid.uuid4().hex)
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                dir_fd = os.open(self.blobs, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            with destination.open("rb") as handle:
                existing = handle.read(MAX_BLOB + 1)
            if len(existing) > MAX_BLOB or hashlib.sha256(existing).hexdigest() != source_id:
                raise QueueError("Stored bundle failed integrity verification")
        self.db.execute("INSERT OR IGNORE INTO projects(name,source_id,revision,created_at) VALUES(?,?,?,?)", (name, source_id, revision, now))
        return {"project": name, "source_id": source_id, "revision": revision}

    def _project_get(self, request):
        source_id = _identifier(request.get("source_id"), "source hash", HASH_RE)
        if self.db.execute("SELECT 1 FROM projects WHERE source_id=?", (source_id,)).fetchone() is None:
            raise QueueError("Unknown source hash")
        try:
            with (self.blobs / source_id).open("rb") as handle:
                raw = handle.read(MAX_BLOB + 1)
        except OSError:
            raise QueueError("Project source bundle is unavailable") from None
        if len(raw) > MAX_BLOB or hashlib.sha256(raw).hexdigest() != source_id:
            raise QueueError("Stored bundle failed integrity verification")
        return {"bundle_b64": base64.b64encode(raw).decode("ascii")}

    def handle(self, request):
        if not isinstance(request, dict):
            raise QueueError("RPC request must be an object")
        action = request.get("action")
        if not isinstance(action, str):
            raise QueueError("RPC action is required")
        now = self.clock()
        # Commit expiration independently: invalid requests cannot keep stale jobs alive.
        with self.transaction():
            self._expire(now)
        with self.transaction():
            if action == "status":
                return self._status(now)
            if action == "submit":
                return self._submit(request.get("spec"), now)
            if action == "get":
                return self._record(self._row(request.get("id")))
            if action == "cancel":
                row = self._row(request.get("id"))
                if row["status"] == "running":
                    self.db.execute("UPDATE jobs SET status='cancel_requested' WHERE id=?", (row["id"],))
                elif row["status"] in {"queued", "blocked"}:
                    self.db.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?", (now, row["id"]))
                self._block_dependents(now)
                return self._record(self._row(row["id"]))
            if action == "retry":
                row = self._row(request.get("id"), full=False)
                if row["status"] not in TERMINAL:
                    raise QueueError("Only finished jobs can be retried")
                return self._submit(json.loads(row["spec_json"]), now, retry_of=row["id"])
            if action == "heartbeat":
                return self._heartbeat(request, now)
            if action == "claim":
                return self._claim(request, now)
            if action == "complete":
                return self._complete(request, now)
            if action == "project_put":
                return self._project_put(request, now)
            if action == "project_get":
                return self._project_get(request)
            if action == "projects":
                rows = self.db.execute("""
                    SELECT p.name AS project,p.source_id,p.revision,p.created_at
                    FROM projects p WHERE p.rowid=(
                        SELECT recent.rowid FROM projects recent WHERE recent.name=p.name
                        ORDER BY recent.created_at DESC,recent.rowid DESC LIMIT 1
                    ) ORDER BY p.name
                """).fetchall()
                return [dict(row) for row in rows]
            raise QueueError("Unknown RPC action")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["rpc"]:
        print("Usage: coordinator.py rpc", file=sys.stderr)
        return 2
    os.umask(0o077)
    coordinator = None
    try:
        raw = sys.stdin.buffer.read(MAX_RPC + 1)
        if len(raw) > MAX_RPC:
            raise QueueError("RPC request exceeds its size limit")
        try:
            request = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise QueueError("RPC request must be valid JSON") from None
        coordinator = Coordinator()
        response = {"ok": True, "data": coordinator.handle(request)}
    except QueueError as error:
        response = {"ok": False, "error": str(error)}
    except (sqlite3.Error, OSError):
        # Never return filesystem contents, connection config, or raw database errors.
        response = {"ok": False, "error": "Coordinator storage is unavailable; retry after checking its service"}
    except Exception:
        response = {"ok": False, "error": "Coordinator could not process this request"}
    finally:
        if coordinator is not None:
            coordinator.close()
    print(_json(response))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
