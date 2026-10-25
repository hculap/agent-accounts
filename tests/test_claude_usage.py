"""Native Claude usage transport tests with a synthetic protocol subprocess."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_accounts import claude_usage
from agent_accounts.store import AccountError


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-claude-usage-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.capture = self.home / "requests.json"
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.home),
            "SYNTHETIC_USAGE_CAPTURE": str(self.capture),
        }
        if "SYSTEMROOT" in os.environ:
            self.env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        fixture = Path(__file__).with_name("fixture_claude_usage.py").resolve()
        self.command = patch.object(
            claude_usage.providers,
            "command",
            side_effect=lambda provider, args: [sys.executable, str(fixture), *args],
        )
        self.command.start()
        self.addCleanup(self.command.stop)

    def test_reads_matching_native_usage_response_without_a_model_prompt(self):
        data = claude_usage.read_usage(self.env)
        self.assertIsNotNone(data)
        self.assertTrue(data["rate_limits_available"])
        self.assertEqual(data["rate_limits"]["five_hour"]["utilization"], 95.25)
        self.assertEqual(data["rate_limits"]["seven_day"]["utilization"], 21)
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(captured["cwd"], str(self.home))
        self.assertEqual(
            [request["request"]["subtype"] for request in captured["requests"]],
            ["initialize", "get_usage"],
        )
        args = captured["argv"]
        self.assertEqual(args[args.index("--input-format") + 1], "stream-json")
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertTrue(json.loads(args[args.index("--settings") + 1])["disableAllHooks"])
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--no-session-persistence", args)
        self.assertNotIn("--bare", args)

    def test_preserves_native_unavailable_marker(self):
        self.env["SYNTHETIC_USAGE_MODE"] = "unavailable"
        data = claude_usage.read_usage(self.env)
        self.assertIsNotNone(data)
        self.assertIs(data["rate_limits_available"], False)
        self.assertIsNone(data["rate_limits"])

    def test_unsupported_or_malformed_protocol_returns_unknown(self):
        for mode in (
            "init_error",
            "unsupported",
            "malformed_payload",
            "malformed_json",
            "wrong_id_only",
        ):
            self.env["SYNTHETIC_USAGE_MODE"] = mode
            with self.subTest(mode=mode):
                self.assertIsNone(claude_usage.read_usage(self.env))

    def test_absent_managed_home_does_not_probe_global_account(self):
        self.env.pop("CLAUDE_CONFIG_DIR")
        self.assertIsNone(claude_usage.read_usage(self.env))
        self.assertFalse(self.capture.exists())

    def test_missing_native_cli_returns_unknown_usage(self):
        with patch.object(claude_usage.providers, "command", side_effect=AccountError("synthetic")):
            self.assertIsNone(claude_usage.read_usage(self.env))

    def test_keyboard_interrupt_is_not_converted_to_unknown_usage(self):
        with patch.object(claude_usage, "JsonProcess", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                claude_usage.read_usage(self.env)

    def test_sigterm_exit_status_is_preserved(self):
        with patch.object(claude_usage, "JsonProcess", side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit) as stopped:
                claude_usage.read_usage(self.env)
        self.assertEqual(stopped.exception.code, 143)


if __name__ == "__main__":
    unittest.main()
