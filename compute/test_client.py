"""Offline checks for the private coordinator transport."""

import json
import shlex
import subprocess
import unittest
from unittest.mock import patch

import client


class ClientTests(unittest.TestCase):
    def test_remote_transport_quotes_script_and_preserves_request(self):
        script = "/private path/$(not-a-command)/coordinator.py"
        config = {"coordinator_ssh": "server-alias", "coordinator_script": script}
        process = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"jobs":[]}}', "")
        with patch.object(client, "load_config", return_value=config), patch.object(client.subprocess, "run", return_value=process) as run:
            self.assertEqual(client.rpc("status"), {"jobs": []})
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(shlex.split(command[-1]), ["python3", script, "rpc"])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"action": "status"})
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_invalid_alias_is_rejected_before_starting_process(self):
        for alias in ("-oProxyCommand=bad", "server name", ["server"]):
            with self.subTest(alias=alias), patch.object(client, "load_config", return_value={"coordinator_ssh": alias}), patch.object(client.subprocess, "run") as run:
                with self.assertRaises(RuntimeError):
                    client.rpc("status")
                run.assert_not_called()

    def test_invalid_response_shapes_become_runtime_errors(self):
        for output in ("not-json", "[]", "null", "1", '"text"'):
            process = subprocess.CompletedProcess([], 0, output, "")
            with self.subTest(output=output), patch.object(client, "load_config", return_value={}), patch.object(client.subprocess, "run", return_value=process):
                with self.assertRaises(RuntimeError):
                    client.rpc("status")

    def test_connection_timeout_is_sanitized(self):
        error = subprocess.TimeoutExpired(["private-connection-detail"], 15)
        with patch.object(client, "load_config", return_value={}), patch.object(client.subprocess, "run", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                client.rpc("status")
        self.assertNotIn("private-connection-detail", str(raised.exception))

    def test_failed_envelope_and_nonzero_process_are_errors(self):
        processes = [subprocess.CompletedProcess([], 0, '{"ok":false,"error":"queue rejected"}', ""),
                     subprocess.CompletedProcess([], 1, '{"ok":true,"data":{}}', "")]
        for process in processes:
            with self.subTest(returncode=process.returncode), patch.object(client, "load_config", return_value={}), patch.object(client.subprocess, "run", return_value=process):
                with self.assertRaises(RuntimeError):
                    client.rpc("status")


if __name__ == "__main__":
    unittest.main()
