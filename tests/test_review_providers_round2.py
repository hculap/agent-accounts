"""Independent full-source provider review regressions using synthetic data only."""

from __future__ import annotations

import json
import unittest

from agent_accounts import providers
from agent_accounts.store import AccountError


class ProviderRoundTwoTests(unittest.TestCase):
    def test_supported_global_options_cannot_hide_authentication_commands(self):
        cases = (
            ["--effort", "low", "auth", "logout"],
            ["-d", "hooks", "auth", "logout"],
            ["--debug", "hooks", "auth", "logout"],
            ["--setting-sources", "project", "auth", "logout"],
            ["--autocompact", "auto", "auth", "logout"],
        )
        for args in cases:
            with self.subTest(args=args), self.assertRaises(AccountError):
                providers.validate_args("claude", args, rotate=False)

    def test_combined_short_continue_flag_cannot_enable_cross_account_resume(self):
        for cluster in ("-cp", "-pc"):
            args = ["-p", cluster, "--output-format", "stream-json", "--verbose", "synthetic task"]
            with self.subTest(cluster=cluster), self.assertRaises(AccountError):
                providers.validate_args("claude", args, rotate=True)

    def test_huge_reset_timestamp_does_not_crash_native_stream_processing(self):
        detector = providers.LimitDetector("claude")
        detector.feed(
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {"status": "rejected", "resetsAt": 10**500},
                }
            ).encode()
        )
        self.assertIsNone(detector.reset_at)

    def test_literal_codex_prompt_after_separator_is_not_an_auth_override(self):
        providers.validate_args(
            "codex", ["exec", "--", "-cchatgpt_base_url=explain-this"], rotate=False
        )

    def test_codex_token_budget_setting_does_not_change_authentication(self):
        providers.validate_args(
            "codex",
            ["exec", "-c", "model_auto_compact_token_limit=200000", "--json", "synthetic task"],
            rotate=True,
        )

    def test_codex_short_config_equals_cannot_override_account_routing(self):
        for key in ("chatgpt_base_url", "model_provider", "cli_auth_credentials_store"):
            with self.subTest(key=key), self.assertRaises(AccountError):
                providers.validate_args(
                    "codex",
                    ["exec", f'-c={key}="synthetic-override"', "--json", "synthetic task"],
                    rotate=True,
                )

    def test_claude_cloud_environment_cannot_bypass_local_session_boundary(self):
        for option in (["--environment", "ccpool_synthetic"], ["--environment=ccpool_synthetic"]):
            for rotate in (False, True):
                with self.subTest(option=option, rotate=rotate), self.assertRaises(AccountError):
                    providers.validate_args(
                        "claude",
                        ["-p", "--output-format", "stream-json", "--verbose", *option, "task"],
                        rotate=rotate,
                    )

    def test_claude_option_values_do_not_change_batch_detection(self):
        self.assertFalse(providers.is_batch("claude", ["--system-prompt", "-p", "synthetic task"]))
        self.assertTrue(
            providers.is_batch("claude", ["-p", "--system-prompt", "-c", "synthetic task"])
        )

    def test_claude_option_values_are_not_resume_flags(self):
        providers.validate_args(
            "claude",
            [
                "-p",
                "--system-prompt",
                "-c",
                "--output-format",
                "stream-json",
                "--verbose",
                "synthetic task",
            ],
            rotate=True,
        )

    def test_claude_worktree_and_resume_names_are_not_auth_commands(self):
        for option in ("-w", "--worktree", "-r", "--resume"):
            with self.subTest(option=option):
                providers.validate_args("claude", [option, "auth", "synthetic task"], rotate=False)

    def test_claude_literal_admin_word_is_allowed_as_prompt_or_option_data(self):
        providers.validate_args("claude", ["-p", "--", "auth"], rotate=False)
        providers.validate_args(
            "claude", ["--system-prompt", "auth", "-p", "synthetic task"], rotate=False
        )


if __name__ == "__main__":
    unittest.main()
