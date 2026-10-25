"""Codex quota preflight contract, exercised only against an offline peer."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = dict(
            os.environ, CODEX_HOME=str(self.root), USAGE_TEST_LOG=str(self.root / "messages.jsonl")
        )
        self.fixture = Path(__file__).with_name("fixture_codex_usage.py")

    def read(self, mode):
        from agent_accounts import codex_usage

        with patch(
            "agent_accounts.providers.command",
            return_value=[sys.executable, str(self.fixture), mode],
        ):
            return codex_usage.read_usage(self.env)

    def test_initialize_ack_precedes_notification_and_quota_request(self):
        result = self.read("success")
        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 95)
        log = [json.loads(line) for line in (self.root / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(
            [event["method"] for event in log],
            ["initialize", "initialized", "account/rateLimits/read"],
        )
        self.assertNotIn("thread/start", [event["method"] for event in log])

    def test_notifications_and_unrelated_response_ids_do_not_replace_usage(self):
        self.assertEqual(self.read("notifications")["accountId"], "synthetic-account")

    def test_unsupported_or_malformed_native_protocol_is_unknown_usage(self):
        for mode in ("unsupported", "missing_result", "malformed"):
            with self.subTest(mode=mode):
                self.assertIsNone(self.read(mode))

    def test_absent_managed_home_never_probes_global_authentication(self):
        self.env.pop("CODEX_HOME")
        self.assertIsNone(self.read("success"))
        self.assertFalse((self.root / "messages.jsonl").exists())

    def test_native_permission_and_canonical_map_reach_shared_semantic_parser(self):
        self.assertIs(self.read("denied")["ordinaryUsageAllowed"], False)
        self.assertIn("codex", self.read("canonical_only")["rateLimitsByLimitId"])
