#!/usr/bin/env python3
"""Acceptance tests for durable queue state, leases, and trust boundaries."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from coordinator import Coordinator, QueueError


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.now = 1000.0
        self.config = {"nodes": ["server", "workstation"], "lease_seconds": 90}
        self.queue = Coordinator(self.config, self.state, clock=lambda: self.now)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def rpc(self, action, **fields):
        return self.queue.handle({"action": action, **fields})

    def submit(self, **overrides):
        spec = {"kind": "command", "title": "Test workload", "argv": ["python3", "-V"]}
        spec.update(overrides)
        return self.rpc("submit", spec=spec)

    def claim(self, node="server", capabilities=None):
        return self.rpc("claim", node=node, capabilities=capabilities or ["command", "agent"])

    def heartbeat(self, claim=None, **overrides):
        fields = {"node": "server", "capabilities": ["command", "agent"], "active_job": None, "lease_token": None, "info": {"cpu_count": 4, "memory_total_mb": 16384}}
        if claim:
            fields.update(active_job=claim["id"], lease_token=claim["lease_token"], node=claim["node"])
        fields.update(overrides)
        return self.rpc("heartbeat", **fields)

    @staticmethod
    def result(exit_code=0, **overrides):
        result = {"exit_code": exit_code, "summary": "Finished", "duration_seconds": 1.5, "log": "Private task output", "patch_b64": ""}
        result.update(overrides)
        return result

    def complete(self, claim, result=None, **overrides):
        fields = {"node": claim["node"], "id": claim["id"], "lease_token": claim["lease_token"], "result": result or self.result()}
        fields.update(overrides)
        return self.rpc("complete", **fields)

    def project(self, name="example", content=b"# v2 git bundle\nFAKE TEST PAYLOAD\n", revision="a" * 40):
        return self.rpc("project_put", name=name, bundle_b64=base64.b64encode(content).decode(), revision=revision)

    def test_success_is_durable_and_status_omits_raw_outputs(self):
        submitted = self.submit()
        claim = self.claim()
        self.assertEqual(claim["id"], submitted["id"])
        self.assertEqual(claim["status"], "running")
        self.assertNotIn("lease_token", self.rpc("get", id=claim["id"]))
        self.assertEqual(self.complete(claim)["status"], "succeeded")
        self.queue.close()
        self.queue = Coordinator(self.config, self.state, clock=lambda: self.now)
        self.assertEqual(self.rpc("get", id=claim["id"])["result"]["log"], "Private task output")
        public_result = self.rpc("status")["jobs"][0]["result"]
        self.assertEqual(set(public_result), {"exit_code", "summary", "duration_seconds"})

    def test_old_queue_migration_preserves_existing_jobs(self):
        job = self.submit()
        self.queue.db.execute("ALTER TABLE jobs DROP COLUMN progress_json")
        self.queue.close()
        self.queue = Coordinator(self.config, self.state, clock=lambda: self.now)
        restored = self.rpc("get", id=job["id"])
        self.assertEqual(restored["status"], "queued")
        self.assertIsNone(restored["progress"])

    def test_progress_requires_matching_active_lease_and_status_omits_log(self):
        self.submit()
        claim = self.claim()
        progress = {"log": "Private live output", "updated_at": self.now}
        self.assertFalse(self.heartbeat(claim, node="workstation", progress=progress)["lease_valid"])
        self.assertFalse(self.heartbeat(claim, lease_token="0" * 64, progress=progress)["lease_valid"])
        self.assertIsNone(self.rpc("get", id=claim["id"])["progress"])
        self.assertTrue(self.heartbeat(claim, progress=progress)["lease_valid"])
        self.assertEqual(self.rpc("get", id=claim["id"])["progress"], progress)
        self.assertEqual(self.rpc("status")["jobs"][0]["progress"], {"updated_at": self.now})
        self.complete(claim)
        self.assertFalse(self.heartbeat(claim, progress={"log": "cannot overwrite", "updated_at": self.now})["lease_valid"])
        self.assertEqual(self.rpc("get", id=claim["id"])["progress"], progress)

    def test_progress_size_unicode_and_timestamp_validation(self):
        self.submit()
        claim = self.claim()
        valid = {"log": "😀" * 8192, "updated_at": self.now}
        self.assertTrue(self.heartbeat(claim, progress=valid)["lease_valid"])
        invalid = [{"log": "😀" * 8193, "updated_at": self.now},
                   {"log": "x", "updated_at": float("nan")},
                   {"log": "x", "updated_at": True},
                   {"log": "x", "updated_at": -1},
                   {"log": "nul\x00", "updated_at": self.now},
                   {"log": "x"}, {"log": "x", "updated_at": self.now, "secret": "x"}]
        for progress in invalid:
            with self.subTest(progress=repr(progress)[:80]), self.assertRaises(QueueError):
                self.heartbeat(claim, progress=progress)
        with self.assertRaises(QueueError):
            self.heartbeat(progress=valid)
        self.assertEqual(self.rpc("get", id=claim["id"])["progress"], valid)

    def test_finite_bounded_metrics_and_consistency(self):
        metrics = {"cpu_count": 8, "memory_total_mb": 16384, "cpu_percent": 42.5,
                   "memory_used_mb": 5678.25, "disk_total_gb": 128, "disk_used_gb": 55.5,
                   "load_1": 2.31, "worker_memory_mb": 123.5, "worker_cpu_percent": 150.25}
        self.heartbeat(info=metrics)
        self.assertEqual(self.rpc("status")["nodes"][0]["info"], metrics)
        invalid = [{"cpu_percent": 101}, {"cpu_percent": True}, {"load_1": float("inf")},
                   {"worker_cpu_percent": float("nan")}, {"worker_memory_mb": -1},
                   {"disk_used_gb": 5, "disk_total_gb": 4},
                   {"memory_used_mb": 8193, "memory_total_mb": 8192},
                   {"disk_total_gb": 1000000001}, {"cpu_percent": 10 ** 1000}]
        for info in invalid:
            with self.subTest(info=info), self.assertRaises(QueueError):
                self.heartbeat(info=info)
        self.assertEqual(self.rpc("status")["nodes"][0]["info"], metrics)

    def test_projects_lists_newest_snapshot_without_paths_or_bundle(self):
        self.assertEqual(self.rpc("projects"), [])
        first = self.project(name="example")
        self.now += 1
        second = self.project(name="example", content=b"# v2 git bundle\nNEW\n", revision="b" * 40)
        other = self.project(name="another", revision="c" * 40)
        projects = self.rpc("projects")
        self.assertEqual([row["project"] for row in projects], ["another", "example"])
        self.assertEqual(projects[1], dict(second, created_at=self.now))
        self.assertNotEqual(projects[1]["source_id"], first["source_id"])
        self.assertEqual(set(projects[0]), {"project", "source_id", "revision", "created_at"})

    def test_one_active_job_per_node_and_two_nodes_can_run(self):
        first, second = self.submit(), self.submit()
        self.assertEqual(self.claim("server")["id"], first["id"])
        self.assertIsNone(self.claim("server"))
        self.assertEqual(self.claim("workstation")["id"], second["id"])
        self.assertEqual([n["active_job"] for n in self.rpc("status")["nodes"]], [first["id"], second["id"]])

    def test_concurrent_claims_do_not_duplicate_execution(self):
        job = self.submit()
        barrier = threading.Barrier(2)

        def attempt(node):
            queue = Coordinator(self.config, self.state, clock=lambda: self.now)
            try:
                barrier.wait(timeout=10)
                return queue.handle({"action": "claim", "node": node, "capabilities": ["command"]})
            finally:
                queue.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(attempt, ["server", "workstation"]))
        self.assertEqual([claim["id"] for claim in claimed if claim], [job["id"]])

    def test_concurrent_claims_same_node_obey_one_slot(self):
        self.submit()
        self.submit()
        barrier = threading.Barrier(2)

        def attempt(_):
            queue = Coordinator(self.config, self.state, clock=lambda: self.now)
            try:
                barrier.wait(timeout=10)
                return queue.handle({"action": "claim", "node": "server", "capabilities": ["command"]})
            finally:
                queue.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(attempt, [0, 1]))
        self.assertEqual(sum(claim is not None for claim in claimed), 1)

    def test_target_and_capabilities_are_enforced(self):
        remote = self.submit(target="workstation")
        project = self.project()
        agent = self.rpc("submit", spec={"kind": "agent", "title": "Code", "prompt": "Implement the objective", **project})
        self.assertIsNone(self.claim("server", ["command"]))
        self.assertEqual(self.claim("server", ["agent"])["id"], agent["id"])
        self.assertEqual(self.claim("workstation", ["command"])["id"], remote["id"])

    def test_dependencies_wait_and_return_completed_outputs(self):
        first = self.submit()
        second = self.submit(depends_on=[first["id"]])
        claim = self.claim()
        self.assertIsNone(self.claim("workstation"))
        self.complete(claim)
        dependent = self.claim("workstation")
        self.assertEqual(dependent["id"], second["id"])
        self.assertEqual(dependent["dependencies"], [{"id": first["id"], "result": self.result()}])

    def test_failed_dependencies_block_transitively(self):
        first = self.submit()
        second = self.submit(depends_on=[first["id"]])
        third = self.submit(depends_on=[second["id"]])
        self.assertEqual(self.complete(self.claim(), self.result(1))["status"], "failed")
        for job in (second, third):
            self.assertEqual(self.rpc("get", id=job["id"])["status"], "blocked")
        self.assertIsNone(self.claim())

    def test_submit_after_failed_dependency_is_immediately_blocked(self):
        first = self.submit()
        self.complete(self.claim(), self.result(1))
        self.assertEqual(self.submit(depends_on=[first["id"]])["status"], "blocked")

    def test_lease_expiry_interrupts_without_auto_retry(self):
        first = self.submit()
        second = self.submit(depends_on=[first["id"]])
        claim = self.claim()
        self.now += 90
        self.assertEqual(self.rpc("get", id=first["id"])["status"], "interrupted")
        self.assertEqual(self.rpc("get", id=second["id"])["status"], "blocked")
        self.assertEqual(self.heartbeat(claim), {"cancel": False, "lease_valid": False})
        with self.assertRaises(QueueError):
            self.complete(claim)
        self.assertIsNone(self.claim("workstation"))

    def test_late_completion_cannot_undo_expiration_even_if_request_fails(self):
        job = self.submit()
        claim = self.claim()
        self.now += 100
        with self.assertRaises(QueueError):
            self.complete(claim)
        row = self.queue.db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row["status"], "interrupted")

    def test_valid_heartbeat_extends_lease(self):
        self.submit()
        claim = self.claim()
        self.now += 80
        self.assertEqual(self.heartbeat(claim), {"cancel": False, "lease_valid": True})
        self.now += 20
        self.assertEqual(self.rpc("get", id=claim["id"])["status"], "running")
        self.assertEqual(self.rpc("get", id=claim["id"])["lease_expires_at"], 1170)

    def test_wrong_token_or_node_cannot_refresh_lease(self):
        self.submit()
        claim = self.claim()
        self.now += 80
        self.assertFalse(self.heartbeat(claim, lease_token="0" * 64)["lease_valid"])
        self.assertFalse(self.heartbeat(claim, node="workstation")["lease_valid"])
        self.now += 11
        self.assertEqual(self.rpc("get", id=claim["id"])["status"], "interrupted")

    def test_wrong_node_or_token_cannot_complete(self):
        self.submit()
        claim = self.claim()
        for override in ({"node": "workstation"}, {"lease_token": "0" * 64}):
            with self.assertRaises(QueueError):
                self.complete(claim, **override)
        self.assertEqual(self.rpc("get", id=claim["id"])["status"], "running")

    def test_completion_delivery_is_idempotent(self):
        self.submit()
        claim = self.claim()
        finished = self.complete(claim)
        self.now += 1000
        self.assertEqual(self.complete(claim), finished)
        with self.assertRaises(QueueError):
            self.complete(claim, self.result(1))

    def test_cancel_queued_job_blocks_dependent(self):
        first = self.submit()
        second = self.submit(depends_on=[first["id"]])
        self.assertEqual(self.rpc("cancel", id=first["id"])["status"], "cancelled")
        self.assertEqual(self.rpc("get", id=second["id"])["status"], "blocked")
        self.assertIsNone(self.claim())

    def test_cancel_running_job_notifies_worker_and_overrides_exit_zero(self):
        self.submit()
        claim = self.claim()
        self.assertEqual(self.rpc("cancel", id=claim["id"])["status"], "cancel_requested")
        self.assertEqual(self.heartbeat(claim), {"cancel": True, "lease_valid": True})
        self.assertEqual(self.complete(claim)["status"], "cancelled")
        self.assertEqual(self.rpc("cancel", id=claim["id"])["status"], "cancelled")

    def test_cancellation_still_expires_when_worker_disappears(self):
        self.submit()
        claim = self.claim()
        self.rpc("cancel", id=claim["id"])
        self.now += 91
        self.assertEqual(self.rpc("get", id=claim["id"])["status"], "interrupted")

    def test_retry_creates_new_id_and_preserves_original(self):
        first = self.submit()
        self.complete(self.claim(), self.result(1))
        retry = self.rpc("retry", id=first["id"])
        self.assertNotEqual(first["id"], retry["id"])
        self.assertEqual(retry["retry_of"], first["id"])
        self.assertEqual(first["spec"], retry["spec"])
        self.assertEqual(self.rpc("get", id=first["id"])["status"], "failed")
        self.assertEqual(self.claim()["id"], retry["id"])

    def test_retry_of_unfinished_job_is_rejected(self):
        job = self.submit()
        with self.assertRaises(QueueError):
            self.rpc("retry", id=job["id"])
        self.claim()
        with self.assertRaises(QueueError):
            self.rpc("retry", id=job["id"])

    def test_nodes_report_resources_online_and_offline(self):
        status = self.rpc("status")
        self.assertFalse(any(node["online"] for node in status["nodes"]))
        self.heartbeat()
        self.assertTrue(self.rpc("status")["nodes"][0]["online"])
        self.claim()  # A no-work poll must not erase the hardware metadata.
        self.assertEqual(self.rpc("status")["nodes"][0]["info"]["cpu_count"], 4)
        self.now += 90
        self.assertFalse(self.rpc("status")["nodes"][0]["online"])

    def test_project_bundle_is_content_addressed_and_private(self):
        content = b"# v2 git bundle\ntest content\n"
        project = self.project(content=content)
        self.assertEqual(project["source_id"], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.project(content=content), project)
        self.assertEqual(base64.b64decode(self.rpc("project_get", source_id=project["source_id"])["bundle_b64"]), content)
        self.assertEqual(stat.S_IMODE((self.state / "blobs" / project["source_id"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "queue.sqlite3").stat().st_mode), 0o600)

    def test_corrupt_project_bundle_is_detected(self):
        project = self.project()
        (self.state / "blobs" / project["source_id"]).write_bytes(b"altered")
        with self.assertRaises(QueueError):
            self.rpc("project_get", source_id=project["source_id"])
        with self.assertRaises(QueueError):
            self.project()

    def test_project_validation(self):
        valid = {"name": "source", "bundle_b64": base64.b64encode(b"# v2 git bundle\ndata").decode(), "revision": "a" * 40}
        for override in ({"name": "../escape"}, {"name": "-option"}, {"revision": "main"}, {"bundle_b64": "???"}, {"bundle_b64": base64.b64encode(b"not a bundle").decode()}):
            with self.subTest(override=override), self.assertRaises(QueueError):
                self.rpc("project_put", **{**valid, **override})
        for invalid in ("../config", "abc", "a" * 64):
            with self.subTest(source=invalid), self.assertRaises(QueueError):
                self.rpc("project_get", source_id=invalid)

    def test_blob_size_limits_apply_to_source_and_combined_results(self):
        with patch("coordinator.MAX_BLOB", 32):
            with self.assertRaises(QueueError):
                self.project(content=b"# v2 git bundle\n" + b"x" * 33)
            self.submit()
            claim = self.claim()
            oversized = base64.b64encode(b"x" * 33).decode()
            with self.assertRaises(QueueError):
                self.complete(claim, self.result(patch_b64=oversized))
            almost_full = base64.b64encode(b"x" * 30).decode()
            artifact = base64.b64encode(b"y" * 3).decode()
            with self.assertRaises(QueueError):
                self.complete(claim, self.result(patch_b64=almost_full, artifacts_b64=artifact))
            self.assertEqual(self.complete(claim, self.result(patch_b64=almost_full))["status"], "succeeded")

    def test_project_pairing_and_registered_revision_required(self):
        project = self.project()
        for fields in ({"project": "source"}, {"source_id": "a" * 64}, {**project, "revision": "b" * 40}, {**project, "project": "different"}):
            with self.subTest(fields=fields), self.assertRaises(QueueError):
                self.submit(**fields)
        with self.assertRaises(QueueError):
            self.rpc("submit", spec={"kind": "agent", "title": "No source", "prompt": "work"})

    def test_inheritance_waits_for_success_and_requires_matching_project(self):
        project = self.project()
        parent = self.submit(**project)
        child = self.submit(**project, depends_on=[parent["id"]], inherit_from=parent["id"])
        claim = self.claim()
        self.assertIsNone(self.claim("workstation"))
        patch = base64.b64encode(b"example diff").decode()
        self.complete(claim, self.result(patch_b64=patch))
        inherited = self.claim()
        self.assertEqual(inherited["id"], child["id"])
        self.assertEqual(inherited["dependencies"][0]["result"]["patch_b64"], patch)
        other_project = self.project(name="other")
        with self.assertRaises(QueueError):
            self.submit(**other_project, depends_on=[parent["id"]], inherit_from=parent["id"])
        with self.assertRaises(QueueError):
            self.submit(**project, inherit_from=parent["id"])

    def test_job_validation_rejects_invalid_inputs_without_enqueuing(self):
        invalid = [
            {"kind": "shell"}, {"kind": []}, {"title": ""}, {"title": "x" * 513},
            {"target": "-oProxyCommand=bad"}, {"target": "unknown"},
            {"role": "admin"}, {"timeout_seconds": 29}, {"timeout_seconds": 14401},
            {"timeout_seconds": True}, {"argv": []}, {"argv": "echo hi"},
            {"argv": ["echo", "bad\x00argument"]}, {"argv": [""]},
            {"argv": ["-option"]}, {"argv": ["echo", "\ud800"]},
            {"argv": ["x"] * 257}, {"depends_on": ["a" * 32]},
            {"depends_on": "not a list"}, {"prompt": "wrong job kind"},
            {"unexpected": "field"},
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(QueueError):
                self.submit(**fields)
        self.assertEqual(self.rpc("status")["jobs"], [])

    def test_duplicate_dependency_is_rejected(self):
        parent = self.submit()
        with self.assertRaises(QueueError):
            self.submit(depends_on=[parent["id"], parent["id"]])

    def test_agent_prompt_is_bounded(self):
        project = self.project()
        with self.assertRaises(QueueError):
            self.rpc("submit", spec={"kind": "agent", "title": "work", "prompt": "x" * (64 * 1024 + 1), **project})

    def test_heartbeat_validation(self):
        for override in ({"node": "-unsafe"}, {"node": "unknown"}, {"capabilities": ["root"]}, {"capabilities": ["agent", "agent"]}, {"info": {"cpu_count": True}}, {"info": {"credentials": "forbidden"}}, {"active_job": "a" * 32}):
            with self.subTest(override=override), self.assertRaises(QueueError):
                self.heartbeat(**override)

    def test_result_validation_preserves_active_job(self):
        self.submit()
        claim = self.claim()
        for override in ({"exit_code": True}, {"duration_seconds": float("nan")}, {"duration_seconds": -1}, {"summary": "bad\x00text"}, {"patch_b64": "invalid*"}, {"artifacts_b64": "invalid*"}, {"log": "x" * (1024 * 1024 + 1)}, {"unknown": "field"}):
            with self.subTest(keys=list(override)), self.assertRaises(QueueError):
                self.complete(claim, self.result(**override))
        self.assertEqual(self.rpc("get", id=claim["id"])["status"], "running")

    def test_status_limits_recent_jobs(self):
        oldest = self.submit()
        for _ in range(101):
            self.submit()
        recent = self.rpc("status")["jobs"]
        self.assertEqual(len(recent), 100)
        self.assertNotIn(oldest["id"], {job["id"] for job in recent})

    def test_rpc_envelope_and_environment_paths(self):
        config_path = Path(self.temp.name) / "config.json"
        config_path.write_text(json.dumps(self.config))
        env = {**os.environ, "CLUSTER_CONFIG": str(config_path), "CLUSTER_STATE": str(self.state)}
        script = str(Path(__file__).with_name("coordinator.py"))
        result = subprocess.run([sys.executable, script, "rpc"], input=json.dumps({"action": "status"}), text=True, capture_output=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        failed = subprocess.run([sys.executable, script, "rpc"], input="malformed", text=True, capture_output=True, env=env, timeout=15)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout), {"ok": False, "error": "RPC request must be valid JSON"})
        self.assertEqual(failed.stderr, "")


if __name__ == "__main__":
    unittest.main()
