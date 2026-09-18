#!/usr/bin/env python3
"""Phone-command parsing and owner-gated native command tests; no messages sent."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from telegram_control import Controller, HELP, bounded


class TelegramControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.jobs = {}
        self.counter = 0
        self.fail_submit = None
        self.projects = [{"project": "sample", "source_id": "a" * 64, "revision": "b" * 40, "created_at": 1000}]
        self.controller = Controller(call=self.rpc, state=self.root, wait_seconds=0)

    def tearDown(self):
        self.temp.cleanup()

    def rpc(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "projects":
            return self.projects
        if action == "status":
            return {"nodes": [{"node": "server", "online": True, "active_job": None, "info": {"cpu_percent": None}},
                              {"node": "workstation", "online": False, "info": {"cpu_percent": "unknown"}}],
                    "jobs": list(self.jobs.values())[::-1]}
        if action == "submit":
            self.counter += 1
            if self.counter == self.fail_submit:
                raise RuntimeError("network unavailable")
            job = {"id": f"{self.counter:08x}" + "0" * 24, "status": "queued", "spec": dict(kwargs["spec"]),
                   "node": None, "result": None, "progress": None}
            self.jobs[job["id"]] = job
            return job
        if action == "get":
            return self.jobs[kwargs["id"]]
        if action == "cancel":
            self.jobs[kwargs["id"]]["status"] = "cancelled"
            return self.jobs[kwargs["id"]]
        if action == "retry":
            return self.rpc("submit", spec=self.jobs[kwargs["id"]]["spec"])
        raise AssertionError(action)

    def test_help_uses_no_rpc(self):
        self.assertEqual(self.controller.handle(""), HELP)
        self.assertEqual(self.calls, [])

    def test_status_handles_unavailable_metrics(self):
        result = self.controller.handle("status")
        self.assertIn("server: online", result)
        self.assertIn("workstation: offline", result)
        self.assertNotIn("CPU", result)

    def test_run_preserves_explicit_shell_script_as_argument(self):
        script = "printf '%s\\n' '$HOME'; printf '%s' '$(not-executed-here)'"
        result = self.controller.handle("run server " + script)
        spec = next(iter(self.jobs.values()))["spec"]
        self.assertEqual(spec["argv"], ["/bin/bash", "-lc", script])
        self.assertEqual(spec["target"], "server")
        self.assertEqual(len(self.jobs), 1)
        self.assertIn("Queued", result)
        self.assertEqual((self.root / "telegram-latest.json").stat().st_mode & 0o777, 0o600)

    def test_unknown_node_does_not_choose_fallback_or_submit(self):
        for args in ("run retired-server echo hello", "agent other sample fix it"):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "no alternate node"):
                self.controller.handle(args)
        self.assertEqual(self.calls, [])

    def test_invalid_commands_do_not_queue(self):
        for args in ("unknown", "run", "agent server sample", "ask sample", "cancel $(x)", "status extra", "x\x00y"):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.controller.handle(args)
        self.assertEqual(self.jobs, {})

    def test_agent_uses_registered_snapshot_and_no_provider_override(self):
        self.controller.handle("agent workstation sample Fix pagination")
        spec = next(iter(self.jobs.values()))["spec"]
        self.assertEqual(spec["prompt"], "Fix pagination")
        self.assertEqual(spec["revision"], "b" * 40)
        self.assertEqual(spec["source_id"], "a" * 64)
        self.assertEqual(spec["target"], "workstation")
        self.assertNotIn("model", spec)
        self.assertNotIn("provider", spec)
        self.assertEqual([action for action, _ in self.calls], ["projects", "submit"])

    def test_missing_project_does_not_submit(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.controller.handle("agent server missing Fix it")
        self.assertFalse(self.jobs)

    def test_team_graph_uses_two_nodes_and_inherits_only_build_patch(self):
        result = self.controller.handle("ask sample Fix the bug")
        jobs = list(self.jobs.values())
        self.assertEqual([job["spec"]["role"] for job in jobs], ["research", "build", "review", "test"])
        self.assertEqual([job["spec"]["target"] for job in jobs], ["workstation", "server", "workstation", "server"])
        self.assertEqual(jobs[1]["spec"]["depends_on"], [jobs[0]["id"]])
        for job in jobs[2:]:
            self.assertEqual(job["spec"]["depends_on"], [jobs[1]["id"]])
            self.assertEqual(job["spec"]["inherit_from"], jobs[1]["id"])
        self.assertIn("Team queued", result)
        self.assertIn("Last specialist team", self.controller.handle("job latest"))

    def test_partial_team_keeps_ids_without_automatic_resubmission(self):
        self.fail_submit = 3
        result = self.controller.handle("ask sample Fix the bug")
        self.assertIn("interrupted", result)
        self.assertIn("Do not resend blindly", result)
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(self.counter, 3)
        saved = json.loads((self.root / "telegram-latest.json").read_text())
        self.assertEqual([job["id"] for job in saved["jobs"]], list(self.jobs))

    def test_single_submit_failure_is_not_retried_or_retargeted(self):
        self.fail_submit = 1
        with self.assertRaises(RuntimeError):
            self.controller.handle("run server echo hi")
        self.assertEqual(self.counter, 1)
        self.assertFalse(self.jobs)

    def test_job_lookup_cancel_and_explicit_retry(self):
        self.controller.handle("run server echo hi")
        first = next(iter(self.jobs.values()))
        self.assertIn("queued", self.controller.handle("job " + first["id"][:8]))
        self.assertIn("cancelled", self.controller.handle("cancel " + first["id"]))
        self.assertIn("New attempt:", self.controller.handle("retry " + first["id"]))
        self.assertEqual(len(self.jobs), 2)

    def test_fast_completed_command_returns_result(self):
        original = self.rpc
        def completed(action, **kwargs):
            job = original(action, **kwargs)
            if action == "get":
                job.update(status="succeeded", result={"summary": "Command completed.", "log": "hello from server", "patch_b64": ""})
            return job
        controller = Controller(call=completed, state=self.root, wait_seconds=8)
        result = controller.handle("run server echo hello")
        self.assertIn("succeeded", result)
        self.assertIn("hello from server", result)

    def test_responses_are_bounded_in_utf16_and_redacted(self):
        text = bounded("access_token=FAKE_SECRET\n" + "😀" * 5000)
        self.assertNotIn("FAKE_SECRET", text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3400)

    def test_json_stdin_help_does_not_require_queue_or_credentials(self):
        env = dict(os.environ, CLUSTER_STATE=str(self.root / "private-state"), CLUSTER_CONFIG=str(self.root / "absent.json"))
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("telegram_control.py"))],
                                input=json.dumps({"args": "help"}), text=True, capture_output=True, env=env, check=True)
        self.assertIn("/cluster status", json.loads(result.stdout)["text"])
        self.assertEqual(result.stderr, "")


class NativePluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get("CLUSTER_TEST_NODE") or shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Set CLUSTER_TEST_NODE to run native plugin authorization tests")

    def test_native_owner_gate_and_no_llm_fallback(self):
        plugin = Path(__file__).with_name("telegram-plugin") / "index.mjs"
        script = r'''
import assert from 'node:assert/strict';
const { default: plugin, createHandler, currentOwner } = await import(process.argv[1]);
// Synthetic fixture IDs only; no installed owner configuration is read.
const owner = '123456789';
const config = { commands: { ownerAllowFrom: ['telegram:' + owner] } };
let calls = [];
const handler = createHandler(owner, async args => { calls.push(args); return {text:'queued'}; });
const ctx = {config, channel:'telegram', senderId:owner, senderIsOwner:true,
  isAuthorizedSender:true, from:'telegram:' + owner, to:'telegram:' + owner, args:'status'};
assert.equal((await handler(ctx)).text, 'queued');
assert.deepEqual(calls, ['status']);
for (const override of [
  {channel:'webchat'}, {channelId:'discord'}, {senderId:'987654321'},
  {senderIsOwner:false}, {senderIsOwner:undefined}, {isAuthorizedSender:false},
  {from:'telegram:group:-100123'}, {to:'telegram:-100123'}, {to:'telegram:' + owner + ':direct-topic:1'},
  {threadParentId:'group'}, {config:{commands:{ownerAllowFrom:['telegram:987654321']}}},
  {config:{commands:{ownerAllowFrom:['*']}}}, {config:{}}, {from:undefined}, {to:undefined},
]) {
  const before = calls.length;
  const result = await handler({...ctx, ...override});
  assert.match(result.text, /verified owner/);
  assert.equal(result.continueAgent, false);
  assert.equal(calls.length, before);
}
assert.equal(currentOwner({commands:{ownerAllowFrom:['telegram:' + owner, 'telegram:987654321']}}), null);
const failing = createHandler(owner, async () => { throw new Error('private error detail'); });
const failed = await failing(ctx);
assert.equal(failed.continueAgent, false);
assert.equal(failed.isError, true);
assert(!failed.text.includes('private error detail'));
const invalid = await handler({...ctx, args:'bad\0input'.replace('\\0', '\0')});
assert.equal(invalid.continueAgent, false);
let definition;
plugin.register({config, pluginConfig:{ownerId:owner}, registerCommand: value => {definition = value;}});
assert.equal(definition.name, 'cluster');
assert.equal(definition.requireAuth, true);
assert.deepEqual(definition.requiredScopes, ['operator.admin']);
assert.deepEqual(definition.channels, ['telegram']);
assert.throws(() => plugin.register({config, pluginConfig:{ownerId:'987654321'}, registerCommand(){}}));
console.log('owner authorization, private-DM scope, registration, and no-fallback checks passed');
'''
        result = subprocess.run([self.node, "--input-type=module", "-e", script, plugin.as_uri()],
                                capture_output=True, text=True, check=True)
        self.assertIn("checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
