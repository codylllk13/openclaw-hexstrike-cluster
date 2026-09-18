#!/usr/bin/env python3
"""Local HTTP trust boundaries and durable desktop dispatch acceptance tests."""

import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stdout

from console import APIError, ConsoleApp, ConsoleServer, MAX_BODY
from coordinator import Coordinator


class FakeCluster:
    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.offline = False
        self.fail_submit_number = None
        self.submit_count = 0
        self.retry_count = 0
        self.fail_retry = False
        self.projects = {"demo": {"project": "demo", "source_id": "a" * 64,
                                 "revision": "b" * 40, "original_revision": "c" * 40,
                                 "path": "/example/project"}}

    def rpc(self, action, **fields):
        self.calls.append((action, copy.deepcopy(fields)))
        if self.offline:
            raise RuntimeError("Coordinator unavailable")
        if action == "status":
            return {"nodes": [{"node": "workstation", "online": True, "active_job": None},
                              {"node": "server", "online": True, "active_job": None}],
                    "jobs": list(copy.deepcopy(self.jobs).values())}
        if action == "get":
            if fields["id"] not in self.jobs:
                raise RuntimeError("Unknown job ID")
            return copy.deepcopy(self.jobs[fields["id"]])
        if action == "submit":
            self.submit_count += 1
            job = self.make_job(fields["spec"])
            if self.submit_count == self.fail_submit_number:
                raise RuntimeError("Connection ended before acknowledgement")
            return copy.deepcopy(job)
        if action == "cancel":
            job = self.jobs[fields["id"]]
            job["status"] = "cancelled"
            return copy.deepcopy(job)
        if action == "retry":
            self.retry_count += 1
            old = self.jobs[fields["id"]]
            job = self.make_job(old["spec"])
            job["retry_of"] = old["id"]
            if self.fail_retry:
                raise RuntimeError("Connection ended before acknowledgement")
            return copy.deepcopy(job)
        raise AssertionError(action)

    def make_job(self, spec, status="queued"):
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "spec": copy.deepcopy(spec), "status": status,
               "node": None, "created_at": time.time(), "result": None, "progress": None}
        self.jobs[job_id] = job
        return job

    def source(self, name):
        if name is None:
            return {"project": None, "source_id": None, "revision": None}
        if name not in self.projects:
            raise ValueError("Unknown project")
        return {k: self.projects[name][k] for k in ("project", "source_id", "revision")}

    def register(self, name, path):
        print("Registered project snapshot")
        self.projects[name] = {"project": name, "path": path, "source_id": "d" * 64,
                               "revision": "e" * 40, "original_revision": "f" * 40}
        return self.source(name)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "desktop"
        self.assets.mkdir()
        (self.assets / "index.html").write_text('<!doctype html><html><head><meta name="cluster-token" content="__CLUSTER_TOKEN__"><script src="/app.js" defer></script></head><body>Cluster</body></html>')
        (self.assets / "app.js").write_text('"use strict";')
        (self.assets / "app.css").write_text('body { color: white; }')
        self.cluster = FakeCluster()
        self.app = self.make_app()
        self.server = ConsoleServer(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def make_app(self):
        return ConsoleApp(state=self.root / "private", assets=self.assets,
                          rpc_fn=self.cluster.rpc, registry_fn=lambda: self.cluster.projects,
                          register_fn=self.cluster.register, source_fn=self.cluster.source)

    @staticmethod
    def payload(**overrides):
        result = {"request_id": str(uuid.uuid4()), "kind": "command", "text": "printf hello", "target": "any"}
        result.update(overrides)
        return result

    def request(self, method, path, payload=None, *, headers=None, token=True,
                origin=True, raw=None):
        request_headers = {"Host": self.server.expected_host}
        if token:
            request_headers["X-Cluster-Token"] = self.app.token
        if origin:
            request_headers["Origin"] = self.server.origin
        body = json.dumps(payload).encode() if payload is not None else raw
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            metadata = dict(response.getheaders())
            if metadata.get("Content-Type", "").startswith("application/json"):
                content = json.loads(content)
            return response.status, metadata, content
        finally:
            connection.close()

    def test_index_embeds_token_and_security_headers(self):
        status, headers, body = self.request("GET", "/", token=False, origin=False)
        self.assertEqual(status, 200)
        self.assertIn(self.app.token.encode(), body)
        self.assertNotIn(b"__CLUSTER_TOKEN__", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_api_requires_token_including_get_and_patch(self):
        for path in ("/api/state", "/api/jobs/" + "a" * 32, "/api/jobs/" + "a" * 32 + "/patch"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, token=False)[0], 403)
                self.assertEqual(self.request("GET", path, headers={"X-Cluster-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", origin=False)[0], 200)

    def test_host_origin_and_cross_site_requests_are_refused(self):
        for headers in ({"Host": "attacker.example"}, {"Host": "localhost:" + str(self.server.server_port)},
                        {"Origin": "https://attacker.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/api/state", headers=headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/submit", self.payload(), origin=False)[0], 403)
        self.assertEqual(self.request("OPTIONS", "/api/state")[0], 403)
        self.assertEqual(self.cluster.submit_count, 0)

    def test_duplicate_host_header_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.putrequest("GET", "/api/state", skip_host=True)
            connection.putheader("Host", self.server.expected_host)
            connection.putheader("Host", self.server.expected_host)
            connection.putheader("X-Cluster-Token", self.app.token)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()

    def test_body_format_size_and_paths_are_validated(self):
        self.assertEqual(self.request("POST", "/api/submit", raw=b"not json")[0], 400)
        self.assertEqual(self.request("POST", "/api/submit", self.payload(), headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/submit", raw=b"x" * (MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request("POST", "/api/submit", raw=b"[]")[0], 400)
        self.assertEqual(self.request("GET", "/../private/console.sqlite3")[0], 404)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil:18891"})[0], 403)
        self.assertEqual(self.cluster.submit_count, 0)

    def test_explicit_command_dispatch_and_durable_conversation(self):
        payload = self.payload(text="printf 'a b' && python3 -V", target="server")
        status, _, response = self.request("POST", "/api/submit", payload)
        self.assertEqual(status, 200, response)
        self.assertEqual(len(response["turn"]["jobs"]), 1)
        job = self.cluster.jobs[response["turn"]["jobs"][0]["id"]]
        self.assertEqual(job["spec"]["argv"], ["/bin/bash", "-lc", payload["text"]])
        self.assertEqual(job["spec"]["target"], "server")
        self.assertEqual(job["spec"]["role"], "compute")
        self.assertEqual(response["turn"]["submission_status"], "submitted")
        restored = self.make_app()
        self.assertEqual(restored.state()["conversations"], self.app.state()["conversations"])
        self.assertNotEqual(restored.token, self.app.token)

    def test_double_submit_and_concurrent_submit_are_idempotent(self):
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.app.submit(payload), [0, 1]))
        self.assertEqual(self.cluster.submit_count, 1)
        self.assertEqual(results[0]["turn"]["id"], results[1]["turn"]["id"])
        restored = self.make_app()
        self.assertEqual(restored.submit(payload)["turn"]["id"], results[0]["turn"]["id"])
        self.assertEqual(self.cluster.submit_count, 1)
        with self.assertRaises(APIError) as error:
            self.app.submit({**payload, "text": "changed command"})
        self.assertEqual(error.exception.status, 409)

    def test_conversation_following_turn_preserves_first_turn(self):
        initial = self.app.submit(self.payload())
        second = self.app.submit(self.payload(conversation_id=initial["conversation"]["id"], text="printf second"))
        self.assertEqual(len(second["conversation"]["turns"]), 2)
        self.assertEqual(second["conversation"]["turns"][0]["id"], initial["turn"]["id"])
        self.assertEqual(len(self.app.state()["conversations"]), 1)

    def test_team_dispatches_real_specialists_and_dependencies(self):
        response = self.app.submit(self.payload(kind="team", project="demo", text="Fix the implementation"))
        jobs = [self.cluster.jobs[job["id"]] for job in response["turn"]["jobs"]]
        self.assertEqual([j["spec"]["role"] for j in jobs], ["research", "build", "review", "test"])
        self.assertEqual([j["spec"]["target"] for j in jobs], ["workstation", "server", "workstation", "server"])
        self.assertEqual(jobs[0]["spec"]["depends_on"], [])
        self.assertEqual(jobs[1]["spec"]["depends_on"], [jobs[0]["id"]])
        for job in jobs[2:]:
            self.assertEqual(job["spec"]["depends_on"], [jobs[1]["id"]])
            self.assertEqual(job["spec"]["inherit_from"], jobs[1]["id"])
        self.assertEqual({j["spec"]["source_id"] for j in jobs}, {"a" * 64})

    def test_follow_up_uses_parent_source_and_patch_before_new_builder(self):
        parent = self.cluster.make_job({"kind": "agent", "role": "build", **self.cluster.source("demo")}, "succeeded")
        self.cluster.projects["demo"]["source_id"] = "f" * 64
        response = self.app.submit(self.payload(kind="team", project="demo", follow_up_to=parent["id"]))
        jobs = [self.cluster.jobs[job["id"]] for job in response["turn"]["jobs"]]
        self.assertEqual({j["spec"]["source_id"] for j in jobs}, {"a" * 64})
        for job in jobs[:2]:
            self.assertIn(parent["id"], job["spec"]["depends_on"])
            self.assertEqual(job["spec"]["inherit_from"], parent["id"])
        for job in jobs[2:]:
            self.assertEqual(job["spec"]["inherit_from"], jobs[1]["id"])

    def test_single_agent_followup_can_infer_project(self):
        parent = self.cluster.make_job({"kind": "agent", "role": "build", **self.cluster.source("demo")}, "succeeded")
        response = self.app.submit(self.payload(kind="agent", follow_up_to=parent["id"], role="review"))
        self.assertEqual(response["turn"]["project"], "demo")
        job = self.cluster.jobs[response["turn"]["jobs"][0]["id"]]
        self.assertEqual(job["spec"]["inherit_from"], parent["id"])
        self.assertEqual(job["spec"]["role"], "review")

    def test_unsuccessful_or_wrong_project_parent_rejected(self):
        parent = self.cluster.make_job({"kind": "agent", "role": "build", **self.cluster.source("demo")}, "failed")
        with self.assertRaises(APIError):
            self.app.submit(self.payload(kind="agent", follow_up_to=parent["id"]))
        parent["status"] = "succeeded"
        with self.assertRaises(APIError):
            self.app.submit(self.payload(kind="agent", project="other", follow_up_to=parent["id"]))
        self.assertEqual(self.cluster.submit_count, 0)

    def test_partial_network_failure_keeps_ids_and_never_replays(self):
        self.cluster.fail_submit_number = 2
        payload = self.payload(kind="team", project="demo")
        status, _, response = self.request("POST", "/api/submit", payload)
        self.assertEqual(status, 502)
        self.assertEqual(len(response["turn"]["jobs"]), 1)
        self.assertEqual(self.cluster.submit_count, 2)  # The unacknowledged call may have reached the server.
        self.assertEqual(len(self.cluster.jobs), 2)
        self.assertIn("not be automatically resubmitted", response["turn"]["error"])
        restored = self.make_app()
        with self.assertRaises(APIError) as error:
            restored.submit(payload)
        self.assertEqual(error.exception.data["turn"]["jobs"], response["turn"]["jobs"])
        self.assertEqual(self.cluster.submit_count, 2)

    def test_restart_marks_unfinished_submission_uncertain_without_replay(self):
        payload = self.app.validate_submission(self.payload())
        turn_id = self.app.store.reserve_submission(payload["request_id"], self.app._fingerprint(payload), payload)
        known = self.cluster.make_job({"role": "compute"})
        self.app.store.add_job(turn_id, known)
        restored = self.make_app()
        with self.assertRaises(APIError) as error:
            restored.submit(payload)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(error.exception.data["turn"]["jobs"][0]["id"], known["id"])
        self.assertIn("restarted", error.exception.data["error"])
        self.assertEqual(self.cluster.submit_count, 0)

    def test_state_reports_offline_and_retains_projects_and_conversations(self):
        self.app.submit(self.payload())
        before = self.app.state()
        self.cluster.offline = True
        status, _, after = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(after["connected"])
        self.assertIn("unavailable", after["error"])
        self.assertEqual(after["projects"], before["projects"])
        self.assertEqual(after["conversations"], before["conversations"])

    def test_job_detail_has_live_log_and_summary_but_no_binary_fields(self):
        job = self.cluster.make_job({"role": "build"}, "succeeded")
        job["result"] = {"summary": "Done", "log": "Final log", "patch_b64": base64.b64encode(b"diff content").decode(), "artifacts_b64": "c2VjcmV0"}
        job["progress"] = {"log": "Live log", "updated_at": time.time()}
        job["lease_token"] = "not-visible"
        status, _, response = self.request("GET", "/api/jobs/" + job["id"])
        self.assertEqual(status, 200)
        self.assertTrue(response["patch_available"])
        self.assertEqual(response["job"]["result"], {"summary": "Done", "log": "Final log"})
        self.assertEqual(response["job"]["progress"]["log"], "Live log")
        self.assertNotIn("lease_token", response["job"])
        status, headers, content = self.request("GET", "/api/jobs/" + job["id"] + "/patch")
        self.assertEqual(status, 200)
        self.assertEqual(content, b"diff content")
        self.assertIn(job["id"] + ".patch", headers["Content-Disposition"])

    def test_cancel_and_retry_link_attempt_to_original_turn(self):
        response = self.app.submit(self.payload())
        job_id = response["turn"]["jobs"][0]["id"]
        self.assertEqual(self.app.cancel(job_id, {})["job"]["status"], "cancelled")
        nonce = {"request_id": str(uuid.uuid4())}
        retried = self.app.retry(job_id, nonce)
        self.assertEqual(retried, self.app.retry(job_id, nonce))
        self.assertEqual(self.cluster.retry_count, 1)
        stored = self.app.state()["conversations"][0]["turns"][0]
        self.assertEqual([j["id"] for j in stored["jobs"]], [job_id, retried["job"]["id"]])

    def test_uncertain_retry_is_not_replayed(self):
        response = self.app.submit(self.payload())
        job_id = response["turn"]["jobs"][0]["id"]
        self.cluster.fail_retry = True
        nonce = {"request_id": str(uuid.uuid4())}
        for _ in range(2):
            with self.assertRaises(APIError):
                self.app.retry(job_id, nonce)
        self.assertEqual(self.cluster.retry_count, 1)

    def test_project_registration_uses_existing_helper_without_stdout_leak(self):
        capture = io.StringIO()
        with redirect_stdout(capture):
            result = self.app.add_project({"name": "new-project", "path": "/owner/code/project"})
        self.assertEqual(capture.getvalue(), "")
        self.assertEqual(result, {"name": "new-project", "path": "/owner/code/project", "revision": "f" * 40})
        status, _, body = self.request("POST", "/api/projects", {"name": "extra", "path": "/owner/code/extra"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["name"], "extra")

    def test_submission_validation_and_unknown_conversation_do_not_enqueue(self):
        invalid = [
            {"request_id": "not-a-uuid"}, {"kind": "unknown"}, {"text": ""},
            {"text": "has\x00nul"}, {"text": "x" * (16 * 1024 + 1)},
            {"target": "-option"}, {"role": "admin"}, {"timeout_seconds": True},
            {"timeout_seconds": 29}, {"timeout_seconds": 14401},
            {"project": "../bad"}, {"kind": "agent"}, {"follow_up_to": "unknown"},
            {"conversation_id": "a" * 32}, {"unexpected": True},
        ]
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(APIError):
                self.app.submit(self.payload(**override))
        self.assertEqual(self.cluster.submit_count, 0)

    def test_team_followup_interoperates_with_real_coordinator(self):
        queue = Coordinator({"nodes": ["workstation", "server"]}, self.root / "queue")
        def call(action, **fields):
            return queue.handle({"action": action, **fields})
        def complete(job, patch=""):
            return call("complete", node=job["node"], id=job["id"], lease_token=job["lease_token"],
                        result={"exit_code": 0, "summary": "Finished", "duration_seconds": 1,
                                "log": "Checked", "patch_b64": patch})
        try:
            source = call("project_put", name="demo", revision="a" * 40,
                          bundle_b64=base64.b64encode(b"# v2 git bundle\ntest\n").decode())
            parent = call("submit", spec={"kind": "command", "title": "Parent patch", "argv": ["true"], **source})
            claimed = call("claim", node="server", capabilities=["command"])
            complete(claimed, base64.b64encode(b"parent patch").decode())
            app = ConsoleApp(state=self.root / "actual-console", assets=self.assets,
                             rpc_fn=call, registry_fn=lambda: {"demo": {**source, "path": "/source"}},
                             source_fn=lambda name: source)
            response = app.submit(self.payload(kind="team", project="demo", follow_up_to=parent["id"]))
            job_ids = [job["id"] for job in response["turn"]["jobs"]]
            research = call("claim", node="workstation", capabilities=["agent"])
            self.assertEqual(research["id"], job_ids[0])
            self.assertEqual(research["dependencies"][0]["id"], parent["id"])
            self.assertIsNone(call("claim", node="server", capabilities=["agent"]))
            complete(research)
            build = call("claim", node="server", capabilities=["agent"])
            self.assertEqual(build["id"], job_ids[1])
            self.assertEqual(build["spec"]["inherit_from"], parent["id"])
            complete(build, base64.b64encode(b"new patch").decode())
            review = call("claim", node="workstation", capabilities=["agent"])
            test = call("claim", node="server", capabilities=["agent"])
            self.assertEqual([review["id"], test["id"]], job_ids[2:])
            for job in (review, test):
                self.assertEqual(job["dependencies"][0]["result"]["patch_b64"], base64.b64encode(b"new patch").decode())
                complete(job)
        finally:
            queue.close()


if __name__ == "__main__":
    unittest.main()
