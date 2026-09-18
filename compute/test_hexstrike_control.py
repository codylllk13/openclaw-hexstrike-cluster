#!/usr/bin/env python3
"""Tests for private HexStrike queueing and task-file handoff."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import hexstrike_agent
from hexstrike_control import Controller, HELP


class HexStrikeControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = {}
        self.counter = 0
        self.runner = "/opt/private/hexstrike_agent.py"
        self.controller = Controller(call=self.rpc, state=self.root, runner=self.runner)

    def tearDown(self):
        self.temp.cleanup()

    def rpc(self, action, **kwargs):
        if action == "submit":
            self.counter += 1
            job = {"id": f"{self.counter:08x}" + "0" * 24, "status": "queued",
                   "spec": dict(kwargs["spec"]), "result": None, "progress": None}
            self.jobs[job["id"]] = job
            return job
        if action == "status":
            return {"nodes": [{"node": "server", "online": True, "active_job": None,
                                "info": {"cpu_percent": 12.4}}],
                    "jobs": list(self.jobs.values())[::-1]}
        if action == "get":
            return self.jobs[kwargs["id"]]
        if action == "cancel":
            self.jobs[kwargs["id"]]["status"] = "cancelled"
            return self.jobs[kwargs["id"]]
        raise AssertionError(action)

    def test_help_does_not_touch_queue(self):
        self.assertEqual(self.controller.handle(""), HELP)
        self.assertEqual(self.jobs, {})

    def test_plain_text_queues_private_task_without_shell(self):
        task = "Assess authorized host; literal $(touch /tmp/nope) and 'quotes'"
        output = self.controller.handle(task)
        job = next(iter(self.jobs.values()))
        self.assertIn("queued", output)
        self.assertEqual(job["spec"]["target"], "server")
        self.assertEqual(job["spec"]["argv"][:2], [self.runner, "--task-file"])
        self.assertNotIn(task, job["spec"]["argv"])
        path = Path(job["spec"]["argv"][2])
        self.assertEqual(path.read_text(), task)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "telegram-latest.json").stat().st_mode), 0o600)

    def test_only_hexstrike_jobs_can_be_read_or_cancelled(self):
        self.controller.handle("scan authorized.example")
        job = next(iter(self.jobs.values()))
        unrelated = {"id": "f" * 32, "status": "queued", "spec": {
            "kind": "command", "target": "server", "argv": ["/bin/true"], "title": "other"}}
        self.jobs[unrelated["id"]] = unrelated
        with self.assertRaisesRegex(ValueError, "not a HexStrike job"):
            self.controller.handle("job " + unrelated["id"])
        path = Path(job["spec"]["argv"][2])
        self.controller.handle("cancel " + job["id"][:8])
        self.assertFalse(path.exists())
        self.assertEqual(job["status"], "cancelled")

    def test_status_filters_other_queue_work(self):
        self.controller.handle("authorized web check")
        self.jobs["f" * 32] = {"id": "f" * 32, "status": "queued", "spec": {
            "kind": "command", "target": "server", "argv": ["/bin/true"], "title": "unrelated"}}
        output = self.controller.handle("status")
        self.assertIn("00000001", output)
        self.assertNotIn("unrelated", output)


class HexStrikeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tasks = Path(self.temp.name)
        self.original_tasks = hexstrike_agent.TASKS
        hexstrike_agent.TASKS = self.tasks

    def tearDown(self):
        hexstrike_agent.TASKS = self.original_tasks
        self.temp.cleanup()

    def test_consume_unlinks_private_regular_file(self):
        path = self.tasks / ("a" * 32 + ".txt")
        path.write_text("authorized target check")
        path.chmod(0o600)
        self.assertEqual(hexstrike_agent.consume(str(path)), "authorized target check")
        self.assertFalse(path.exists())

    def test_consume_rejects_symlink(self):
        outside = self.tasks.parent / "outside-task"
        outside.write_text("secret")
        path = self.tasks / ("b" * 32 + ".txt")
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            hexstrike_agent.consume(str(path))

    def test_subscription_environment_removes_api_provider_overrides(self):
        source = {
            "PATH": "/usr/bin",
            "HOME": "/home/example",
            "OPENAI_API_KEY": "must-not-reach-codex",
            "CODEX_API_KEY": "must-not-reach-codex",
            "OPENAI_BASE_URL": "https://provider.invalid",
            "UNRELATED_SETTING": "preserved",
        }
        result = hexstrike_agent.subscription_environment(source)
        self.assertEqual(result["PATH"], "/usr/bin")
        self.assertEqual(result["HOME"], "/home/example")
        self.assertEqual(result["UNRELATED_SETTING"], "preserved")
        self.assertTrue(hexstrike_agent.API_ENVIRONMENT.isdisjoint(result))

    def test_json_stdin_help_needs_no_queue(self):
        env = dict(os.environ, HEXSTRIKE_STATE=str(self.root if hasattr(self, "root") else self.tasks / "state"),
                   CLUSTER_CONFIG=str(self.tasks / "missing.json"))
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("hexstrike_control.py"))],
            input=json.dumps({"args": "help"}), text=True, capture_output=True, env=env, check=True,
        )
        self.assertIn("/hexstrike TASK", json.loads(result.stdout)["text"])


if __name__ == "__main__":
    unittest.main()
