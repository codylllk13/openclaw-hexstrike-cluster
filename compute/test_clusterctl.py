"""Offline tests for source snapshots and CLI/coordinator integration."""

import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import clusterctl


HERE = Path(__file__).resolve().parent


class ClusterctlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cluster-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.env = dict(os.environ, CLUSTER_STATE=str(self.root / "state"),
                        CLUSTER_CONFIG=str(self.root / "config.json"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
        self.env_patch = patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.config = {"nodes": ["workstation", "server"],
                       "coordinator_script": str(HERE / "coordinator.py")}
        Path(self.env["CLUSTER_CONFIG"]).write_text(json.dumps(self.config))
        self.git("init", "-q")
        (self.repo / "main.py").write_text("print('hello')\n")
        self.commit("Initial project")

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              text=True, capture_output=True, check=check, env=self.env).stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "clusterctl.py"), *args],
                              capture_output=True, text=True, env=self.env, timeout=20)

    def add_snapshot(self, source=None):
        captured = {}
        def record(action, **kwargs):
            self.assertEqual(action, "project_put")
            captured.update(kwargs)
            raw = base64.b64decode(kwargs["bundle_b64"], validate=True)
            return {"project": kwargs["name"], "source_id": hashlib.sha256(raw).hexdigest(),
                    "revision": kwargs["revision"]}
        with patch.object(clusterctl, "rpc", side_effect=record), contextlib.redirect_stdout(io.StringIO()):
            clusterctl.add_project("sample", source or self.repo)
        return captured

    def test_snapshot_contains_only_head_tree_and_never_copies_history(self):
        secret = self.repo / ".env"
        secret.write_text("DELETED_HISTORICAL_SECRET=fixture\n")
        self.commit("Historical credential")
        old_blob = self.git("rev-parse", "HEAD:.env")
        old_commit = self.git("rev-parse", "HEAD")
        secret.unlink()
        (self.repo / "main.py").write_text("print('current')\n")
        self.commit("Current clean source")
        refs_before = self.git("show-ref")
        head_before = self.git("rev-parse", "HEAD")
        files_before = {str(p.relative_to(self.repo)): p.read_bytes()
                        for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts}
        objects_before = sorted(str(p.relative_to(self.repo / ".git"))
                                for p in (self.repo / ".git" / "objects").rglob("*") if p.is_file())
        captured = self.add_snapshot()
        bundle = self.root / "source.bundle"
        bundle.write_bytes(base64.b64decode(captured["bundle_b64"]))
        checkout = self.root / "checkout"
        subprocess.run(["git", "clone", "-q", "-b", "snapshot", str(bundle), str(checkout)],
                       capture_output=True, check=True, env=self.env)
        self.assertEqual(self.git("rev-list", "--all", "--count", cwd=checkout), "1")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}", cwd=checkout),
                         self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual((checkout / "main.py").read_text(), "print('current')\n")
        for absent in (old_blob, old_commit):
            check = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", absent],
                                   capture_output=True, env=self.env)
            self.assertNotEqual(check.returncode, 0)
        self.assertEqual(self.git("show-ref"), refs_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.assertEqual(sorted(str(p.relative_to(self.repo / ".git"))
                                for p in (self.repo / ".git" / "objects").rglob("*") if p.is_file()), objects_before)
        self.assertEqual({str(p.relative_to(self.repo)): p.read_bytes()
                          for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts}, files_before)
        registration = clusterctl.registry()["sample"]
        self.assertEqual(registration["original_revision"], head_before)
        self.assertEqual(registration["revision"], captured["revision"])
        self.assertNotEqual(registration["revision"], head_before)

    def test_dirty_tracked_and_untracked_sources_rejected_before_upload(self):
        for filename in ("main.py", "untracked.txt"):
            with self.subTest(filename=filename):
                target = self.repo / filename
                before = target.read_bytes() if target.exists() else None
                target.write_text("uncommitted\n")
                with patch.object(clusterctl, "rpc") as rpc, self.assertRaisesRegex(ValueError, "clean HEAD"):
                    clusterctl.add_project("sample", self.repo)
                rpc.assert_not_called()
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)

    def test_credential_names_rejected_before_upload(self):
        (self.repo / ".env").write_text("fixture-only\n")
        self.commit("Add credential path")
        with patch.object(clusterctl, "rpc") as rpc, self.assertRaisesRegex(ValueError, "credential"):
            clusterctl.add_project("sample", self.repo)
        rpc.assert_not_called()

    def test_subdirectory_registration_checks_entire_bundled_tree(self):
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("pass\n")
        (self.repo / ".env").write_text("fixture-only\n")
        self.commit("Credential outside requested subdirectory")
        with patch.object(clusterctl, "rpc") as rpc, contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "credential|root"):
            clusterctl.add_project("sample", self.repo / "src")
        rpc.assert_not_called()

    def test_git_quoted_credential_filename_is_checked_correctly(self):
        directory = self.repo / "folder\nwith newline"
        directory.mkdir()
        (directory / ".env").write_text("fixture-only\n")
        self.commit("Credential in unusually named folder")
        with patch.object(clusterctl, "rpc") as rpc, contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "credential"):
            clusterctl.add_project("sample", self.repo)
        rpc.assert_not_called()

    def test_submodules_rejected_before_upload(self):
        revision = self.git("rev-parse", "HEAD")
        (self.repo / "vendor").mkdir()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{revision},vendor")
        self.git("commit", "-qm", "Gitlink fixture")
        # The empty directory represents an uninitialized submodule.
        with patch.object(clusterctl, "rpc") as rpc, self.assertRaisesRegex(ValueError, "Submodules"):
            clusterctl.add_project("sample", self.repo)
        rpc.assert_not_called()

    def test_real_team_graph_and_snapshot_registration(self):
        registered = self.cli("project", "add", "sample", str(self.repo))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        submitted = self.cli("team", "--project", "sample", "Add a tiny feature")
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        status = self.cli("status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        jobs = json.loads(status.stdout)["jobs"]
        self.assertEqual(len(jobs), 4)
        roles = {job["spec"]["role"]: job for job in jobs}
        research, build, review, test = (roles[role] for role in ("research", "build", "review", "test"))
        self.assertEqual(research["spec"]["target"], "workstation")
        self.assertEqual(research["spec"]["depends_on"], [])
        self.assertEqual(build["spec"]["target"], "server")
        self.assertEqual(build["spec"]["depends_on"], [research["id"]])
        self.assertIsNone(build["spec"]["inherit_from"])
        for job, target in ((review, "workstation"), (test, "server")):
            self.assertEqual(job["spec"]["target"], target)
            self.assertEqual(job["spec"]["depends_on"], [build["id"]])
            self.assertEqual(job["spec"]["inherit_from"], build["id"])
        self.assertEqual(len({job["spec"]["source_id"] for job in jobs}), 1)
        self.assertTrue(all(job["status"] == "queued" for job in jobs))

    def test_command_arguments_are_preserved_and_never_run_by_submission(self):
        marker = self.root / "should-not-exist"
        literal = f"$(touch {marker}); spaces and quotes ' \""
        submitted = self.cli("run", "--node", "server", "--", "printf", "%s", literal)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        shown = self.cli("show", submitted.stdout.strip())
        job = json.loads(shown.stdout)
        self.assertEqual(job["spec"]["argv"], ["printf", "%s", literal])
        self.assertFalse(marker.exists())

    def test_invalid_commands_fail_without_queueing(self):
        cases = [
            ("run", "--"),
            ("run", "--timeout", "1", "--", "true"),
            ("run", "--node", "unknown", "--", "true"),
            ("run", "--inherit", "0" * 32, "--", "true"),
            ("agent", "--project", "missing", "Task"),
            ("agent", "Task without required project"),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                result = self.cli(*argv)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
        status = self.cli("status", "--json")
        self.assertEqual(json.loads(status.stdout)["jobs"], [])

    def test_patch_output_refuses_to_overwrite_existing_file(self):
        target = self.root / "existing.patch"
        target.write_text("keep this content\n")
        response = {"result": {"patch_b64": base64.b64encode(b"replacement").decode()}}
        with patch.object(clusterctl, "rpc", return_value=response), \
                patch.object(sys, "argv", ["clusterctl", "patch", "a" * 32, "--output", str(target)]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(clusterctl.main(), 1)
        self.assertEqual(target.read_text(), "keep this content\n")


if __name__ == "__main__":
    unittest.main()
