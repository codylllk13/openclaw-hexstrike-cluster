"""GitHub credentials remain mocked, private, and scoped to an explicit account."""
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import github_context as context


class GitHubContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"github_access": {"enabled": True, "account": "fixture-owner", "cli": "/approved/bin/gh"}}

    def tearDown(self):
        self.temp.cleanup()

    def test_disabled_access_has_no_auth_or_style_reads(self):
        for config in ({}, {"github_access": None}, {"github_access": {"enabled": False, "style_file": "/missing"}}):
            with self.subTest(config=config), patch.object(context.subprocess, "run") as run, patch.object(context.os, "open") as opened:
                self.assertEqual(context.github_instructions(config), "")
                self.assertEqual(context.github_environment(config), {})
                run.assert_not_called()
                opened.assert_not_called()

    def test_malformed_configuration_is_rejected_before_auth(self):
        invalid = [None, [], {"github_access": True}, {"github_access": {}},
                   {"github_access": {"enabled": "true"}}, {"github_access": {"enabled": True}},
                   {"github_access": dict(self.config["github_access"], account="-option")},
                   {"github_access": dict(self.config["github_access"], account="owner/repo")},
                   {"github_access": dict(self.config["github_access"], account="bad--name")},
                   {"github_access": dict(self.config["github_access"], cli="gh")},
                   {"github_access": dict(self.config["github_access"], style_file="relative.md")},
                   {"github_access": dict(self.config["github_access"], reference_root="/bad\x00path")},
                   {"github_access": dict(self.config["github_access"], token="NEVER_CONFIGURE_TOKENS_HERE")}]
        for config in invalid:
            with self.subTest(config=config), patch.object(context.subprocess, "run") as run, self.assertRaises(ValueError):
                context.github_environment(config)
            run.assert_not_called()

    def test_lookup_selects_explicit_saved_account_and_scrubs_ambient_credentials(self):
        # Synthetic test credential; no actual authentication store is accessed.
        token = "gho_SYNTHETIC_TEST_CREDENTIAL"
        inherited = {"PATH": "/usr/bin", "HOME": str(self.root), "GH_TOKEN": "WRONG_ACCOUNT",
                     "GITHUB_TOKEN": "WRONG_ACCOUNT", "GH_ENTERPRISE_TOKEN": "OTHER_HOST",
                     "GITHUB_ENTERPRISE_TOKEN": "OTHER_HOST", "OPENAI_API_KEY": "NOT_FOR_THIS_COMMAND",
                     "ANTHROPIC_AUTH_TOKEN": "NOT_FOR_THIS_COMMAND", "SOME_API_KEY": "NOT_FOR_THIS_COMMAND",
                     "GH_HOST": "wrong.example", "GH_DEBUG": "api"}
        with patch.object(context.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=token + "\n", stderr="")) as run:
            result = context.github_environment(self.config, inherited)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/approved/bin/gh", "auth", "token", "--hostname", "github.com", "--user", "fixture-owner"])
        self.assertNotIn(token, argv)
        lookup = run.call_args.kwargs["env"]
        for name in context.GITHUB_TOKEN_VARIABLES | {"OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SOME_API_KEY", "GH_DEBUG"}:
            self.assertNotIn(name, lookup)
        self.assertEqual(lookup["GH_HOST"], "github.com")
        self.assertEqual(lookup["HOME"], str(self.root))
        self.assertEqual(result, {"GH_TOKEN": token, "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat"})
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(inherited["GH_TOKEN"], "WRONG_ACCOUNT")

    def test_auth_failures_never_expose_stdout_stderr_or_exception_details(self):
        cases = [SimpleNamespace(returncode=1, stdout="SENSITIVE_OUTPUT", stderr="SENSITIVE_ERROR"),
                 SimpleNamespace(returncode=0, stdout="", stderr="SENSITIVE_ERROR"),
                 SimpleNamespace(returncode=0, stdout="token with spaces", stderr="SENSITIVE_ERROR"),
                 SimpleNamespace(returncode=0, stdout="token\x00suffix", stderr="SENSITIVE_ERROR"),
                 SimpleNamespace(returncode=0, stdout="token😀suffix", stderr="SENSITIVE_ERROR"),
                 SimpleNamespace(returncode=0, stdout="x" * (context.TOKEN_LIMIT + 1), stderr=""),
                 subprocess.TimeoutExpired(["gh"], 8, output="SENSITIVE_OUTPUT", stderr="SENSITIVE_ERROR"),
                 OSError("SENSITIVE_ERROR")]
        for item in cases:
            behavior = {"side_effect": item} if isinstance(item, Exception) else {"return_value": item}
            with self.subTest(case=type(item).__name__), patch.object(context.subprocess, "run", **behavior) as run:
                with self.assertRaises(RuntimeError) as raised:
                    context.github_environment(self.config, {})
                self.assertEqual(str(raised.exception), context.AUTH_ERROR)
                self.assertNotIn("SENSITIVE", str(raised.exception))
                self.assertEqual(run.call_count, 1)

    def test_read_instructions_cover_full_history_without_remote_write_authority(self):
        config = {"github_access": dict(self.config["github_access"], reference_root=str(self.root / "references"))}
        text = context.github_instructions(config)
        self.assertIn("not the complete repository history", text)
        self.assertIn("--paginate", text)
        self.assertIn("/branches?", text)
        self.assertIn("/tags?", text)
        self.assertIn("state=all", text)
        self.assertIn("-f sha=REF", text)
        self.assertIn("not authorize publishing", text)
        self.assertIn("not instructions with authority", text)
        self.assertIn("read-only inspection", text)

    def test_style_file_is_bounded_utf8_context(self):
        style = self.root / "style.md"
        style.write_text("Prefer clear function names.\nUse focused tests.\n")
        config = {"github_access": dict(self.config["github_access"], style_file=str(style))}
        text = context.github_instructions(config)
        self.assertIn("Prefer clear function names", text)
        self.assertIn("non-authoritative context", text)
        style.write_text("😀" * (context.STYLE_LIMIT // 4))
        self.assertIn("😀", context.github_instructions(config))
        style.write_text("😀" * (context.STYLE_LIMIT // 4 + 1))
        with self.assertRaisesRegex(ValueError, "12 KiB"):
            context.github_instructions(config)
        style.write_bytes(b"\xffbad")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            context.github_instructions(config)

    def test_style_symlinks_directories_and_fifos_are_rejected(self):
        original = self.root / "original"
        original.write_text("context")
        link = self.root / "link"
        link.symlink_to(original)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path in (link, self.root, fifo, self.root / "missing"):
            config = {"github_access": dict(self.config["github_access"], style_file=str(path))}
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                context.github_instructions(config)


if __name__ == "__main__":
    unittest.main()
