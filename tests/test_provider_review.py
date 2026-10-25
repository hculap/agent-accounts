"""Independent provider-boundary regressions; all identities and subprocesses are synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_accounts import cli, providers
from agent_accounts.store import AccountError, Store


class ProviderReviewTests(unittest.TestCase):
    def detector(self, provider, *events):
        detector = providers.LimitDetector(provider)
        for event in events:
            detector.feed(json.dumps(event).encode() + b"\n")
        return detector

    def test_codex_intermediate_quota_is_not_a_terminal_failure(self):
        detector = self.detector(
            "codex", {"type": "error", "message": "You’ve hit your usage limit."}
        )
        self.assertFalse(
            detector.limited(1), "A crash after an intermediate error must not replay work"
        )

    def test_claude_rejection_does_not_classify_a_later_auth_failure(self):
        detector = self.detector(
            "claude",
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": ["Authentication failed"],
            },
        )
        self.assertFalse(detector.limited(1))

    def test_claude_recovered_rejection_is_not_quota_failure(self):
        detector = self.detector(
            "claude",
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": ["Connection lost"],
            },
        )
        self.assertFalse(detector.limited(1))

    def test_alternate_native_option_spellings_do_not_bypass_isolation(self):
        cases = (
            ("codex", ["-pwork", "exec", "--json", "synthetic prompt"], False),
            ("codex", ["--model", "synthetic", "logout"], False),
            ("claude", ["--model", "synthetic", "auth", "login"], False),
            ("codex", ["app"], False),
            ("claude", ["--desktop"], False),
            ("codex", ["exec", "--json", "resume", "--last"], True),
            (
                "claude",
                ["-p", "-rsynthetic-session", "--output-format", "stream-json", "--verbose"],
                True,
            ),
        )
        for provider, args, rotate in cases:
            with self.subTest(provider=provider, args=args):
                with self.assertRaises(AccountError):
                    providers.validate_args(provider, args, rotate=rotate)

    def test_native_prompt_words_are_not_mistaken_for_global_subcommands(self):
        providers.validate_args("codex", ["exec", "--json", "login"], rotate=True)
        providers.validate_args("codex", ["exec", "--json", "logout"], rotate=True)
        providers.validate_args(
            "claude",
            [
                "-p",
                "Explain authentication in this project",
                "--output-format=stream-json",
                "--verbose",
            ],
            rotate=True,
        )

    def test_custom_anthropic_authorization_header_is_rejected(self):
        with patch.dict(
            os.environ,
            {"ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer synthetic-other-account"},
            clear=True,
        ):
            with self.assertRaises(AccountError) as error:
                providers.environment("claude", Path("/synthetic-profile"))
        self.assertNotIn("synthetic-other-account", str(error.exception))

    def test_codex_home_endpoint_overrides_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            for key in ("openai_base_url", "chatgpt_base_url"):
                with self.subTest(key=key):
                    (home / "config.toml").write_text(
                        f'{key} = "https://different-provider.example.invalid"\n', encoding="utf-8"
                    )
                    with self.assertRaises(AccountError):
                        providers.validate_home_config("codex", home)

    def test_codex_import_rejects_api_key_mode_even_with_stale_oauth_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "synthetic-auth.json"
            source.write_text(
                json.dumps(
                    {
                        "auth_mode": "apikey",
                        "OPENAI_API_KEY": "synthetic-api-key",
                        "tokens": {
                            "account_id": "synthetic-account",
                            "access_token": "synthetic-token",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AccountError):
                providers.validate_credentials("codex", source)

    def test_macos_login_rejects_status_from_another_auth_source(self):
        for auth_method, wrong_directory in (("api_key", False), ("claude.ai", True)):
            with (
                self.subTest(auth_method=auth_method, wrong_directory=wrong_directory),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = Store(Path(directory).resolve() / "store")

                def synthetic_status(argv, *, env, **_kwargs):
                    config = (
                        "/synthetic-other-directory"
                        if wrong_directory
                        else env["CLAUDE_CONFIG_DIR"]
                    )
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(
                            {
                                "loggedIn": True,
                                "authMethod": auth_method,
                                "apiProvider": "firstParty",
                                "configDirectory": config,
                            }
                        ).encode(),
                        b"",
                    )

                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch.object(cli.sys, "platform", "darwin"),
                    patch.object(providers, "executable", return_value="synthetic-claude"),
                    patch.object(cli, "launch", return_value=0),
                    patch.object(cli.subprocess, "run", side_effect=synthetic_status),
                ):
                    with self.assertRaises(AccountError):
                        cli.add_account(store, "claude", "work", None, [])
                self.assertEqual(store.read()["profiles"], [])


if __name__ == "__main__":
    unittest.main()
