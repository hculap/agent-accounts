"""Independent, subprocess-level acceptance tests using isolated synthetic credentials."""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-accounts-acceptance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / "private account store"
        self.env = dict(os.environ)
        for name in (
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "AGENT_ACCOUNTS_HOME",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        ):
            self.env.pop(name, None)
        self.env["PYTHONPATH"] = os.pathsep.join((str(PROJECT / "src"), str(PROJECT / "tests")))
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["AGENT_ACCOUNTS_HOME"] = str(self.store)
        self.env["CODEX_HOME"] = str(self.root / "original codex")
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "original claude")
        self.calls_file = self.root / "calls.jsonl"
        self.env["AA_TEST_CALLS"] = str(self.calls_file)
        self.bin = self.root / "fake native tools"
        self.bin.mkdir()
        fixture = Path(__file__).with_name("fixture_provider_acceptance.py")
        for provider in ("codex", "claude"):
            if os.name == "nt":
                from distlib.scripts import ScriptMaker, enquote_executable

                maker = ScriptMaker(None, str(self.bin))
                maker.variants = {""}
                maker.executable = enquote_executable(sys.executable)
                maker.make(f"{provider} = fixture_provider_acceptance:main")
                self.assertTrue((self.bin / (provider + ".exe")).is_file())
            else:
                executable = self.bin / provider
                executable.write_text(
                    f"#!{sys.executable}\n" + fixture.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                executable.chmod(0o700)
        self.env["PATH"] = str(self.bin) + os.pathsep + os.defpath

    def cli(self, *args, env=None, input=None):
        return subprocess.run(
            [sys.executable, "-m", "agent_accounts", *args],
            env=self.env if env is None else env,
            cwd=self.root,
            input=input,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
        )

    def success(self, *args, env=None):
        result = self.cli(*args, env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def auth(self, provider, identity):
        if provider == "claude":
            return {
                "claudeAiOauth": {
                    "accessToken": f"synthetic-access-{identity}",
                    "refreshToken": f"synthetic-refresh-{identity}",
                    "expiresAt": (int(time.time()) + 3600) * 1000,
                    "scopes": ["user:inference", "user:profile"],
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_5x",
                }
            }
        claims = {
            "sub": f"user-{identity}",
            "email": f"{identity}@example.invalid",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": f"account-{identity}",
                "chatgpt_plan_type": "pro",
            },
        }
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "account_id": f"account-{identity}",
                "id_token": "header." + payload + ".signature",
                "access_token": f"synthetic-access-{identity}",
                "refresh_token": f"synthetic-refresh-{identity}",
            },
            "last_refresh": "2026-01-01T00:00:00Z",
        }

    def import_profile(self, provider, name):
        source = self.root / f"synthetic-{provider}-{name}.json"
        source.write_text(json.dumps(self.auth(provider, name)), encoding="utf-8")
        if provider == "claude" and sys.platform == "darwin":
            # macOS supports native login, not portable file import. The fake CLI
            # confirms auth status through the same public protocol as Claude.
            env = dict(self.env, AA_TEST_LOGIN_AUTH=json.dumps(self.auth(provider, name)))
            self.success("login", provider, name, env=env)
        else:
            self.success("import", provider, name, "--from", str(source))
        profiles = self.profiles(provider)
        return next(item for item in profiles if item["name"] == name)

    def profiles(self, provider=None):
        args = ("list", provider, "--json") if provider else ("list", "--json")
        return json.loads(self.success(*args).stdout)

    def calls(self, *, include_claude_auth=False):
        if not self.calls_file.exists():
            return []
        records = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        if not include_claude_auth:
            records = [
                record
                for record in records
                if not (record["provider"] == "claude" and record["arguments"][:1] == ["auth"])
            ]
        return records

    def configure_scenarios(self, *pairs):
        self.env["AA_TEST_SCENARIOS"] = json.dumps(
            {profile["home"]: scenario for profile, scenario in pairs}
        )

    def pair(self, provider, first_scenario="quota", second_scenario="success"):
        first = self.import_profile(provider, "primary")
        second = self.import_profile(provider, "secondary")
        self.success("use", provider, "primary")
        self.configure_scenarios((first, first_scenario), (second, second_scenario))
        return first, second

    def batch(self, provider, *options):
        args = (
            ["exec", "--json", "synthetic task"]
            if provider == "codex"
            else ["-p", "synthetic task", "--output-format", "stream-json", "--verbose"]
        )
        return self.cli("run", provider, *options, "--", *args)

    def holding_run(self, *, rotate=False):
        self.env["AA_TEST_READY"] = str(self.root / "child-ready")
        self.env["AA_TEST_RELEASE"] = str(self.root / "release-child")
        self.env["AA_TEST_EXITED"] = str(self.root / "child-exited")
        options = ["--auto-rotate"] if rotate else []
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_accounts",
                "run",
                "codex",
                *options,
                "--",
                "exec",
                "--json",
                "synthetic task",
            ],
            cwd=self.root,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
        )

        def release():
            Path(self.env["AA_TEST_RELEASE"]).touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

        self.addCleanup(release)
        deadline = time.monotonic() + 8
        while not Path(self.env["AA_TEST_READY"]).exists():
            if process.poll() is not None:
                out, err = process.communicate()
                self.fail(f"Holding fake provider did not start: {out} {err}")
            if time.monotonic() > deadline:
                self.fail("Holding fake provider did not become ready")
            time.sleep(0.02)
        return process

    def test_settings_default_to_manual_account_selection(self):
        result = self.success("settings", "--json")
        settings = json.loads(result.stdout)
        self.assertIs(settings["auto_rotate"], False)
        self.assertEqual(settings["cooldown_seconds"], 300)

    def test_list_starts_empty_without_reading_real_accounts(self):
        result = self.success("list", "--json")
        self.assertEqual(json.loads(result.stdout), [])

    def test_settings_persist_and_flags_have_precedence(self):
        self.success("settings", "set", "auto-rotate", "on")
        self.success("settings", "set", "cooldown-seconds", "42")
        settings = json.loads(self.success("settings", "--json").stdout)
        self.assertIs(settings["auto_rotate"], True)
        self.assertEqual(settings["cooldown_seconds"], 42)
        self.pair("codex")
        result = self.batch("codex", "--no-auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_settings_do_not_destroy_existing_settings(self):
        self.success("settings", "set", "cooldown-seconds", "45")
        for key, value in (
            ("auto-rotate", "maybe"),
            ("cooldown-seconds", "-1"),
            ("cooldown-seconds", "text"),
            ("unknown-setting", "1"),
        ):
            with self.subTest(key=key, value=value):
                result = self.cli("settings", "set", key, value)
                self.assertNotEqual(result.returncode, 0)
        settings = json.loads(self.success("settings", "--json").stdout)
        self.assertEqual(settings["cooldown_seconds"], 45)
        self.assertIs(settings["auto_rotate"], False)

    def test_explicit_home_overrides_environment(self):
        self.import_profile("codex", "existing")
        other = self.root / "other store"
        result = self.success("--home", str(other), "list", "--json")
        self.assertEqual(json.loads(result.stdout), [])
        self.assertEqual(len(self.profiles()), 1)

    def test_import_use_remove_are_provider_isolated_and_non_destructive(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                original = Path(
                    self.env["CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"]
                )
                original.mkdir()
                filename = "auth.json" if provider == "codex" else ".credentials.json"
                live = original / filename
                live.write_text(json.dumps(self.auth(provider, "original")), encoding="utf-8")
                previous = live.read_bytes()
                profile = self.import_profile(provider, "personal")
                home = Path(profile["home"])
                self.assertTrue(home.is_relative_to(self.store))
                self.assertTrue((home / filename).is_file())
                self.success("use", provider, "personal")
                self.assertTrue(self.profiles(provider)[0]["active"])
                self.assertEqual(live.read_bytes(), previous)
                self.success("remove", provider, "personal")
                self.assertEqual(self.profiles(provider), [])
                self.assertTrue((home / filename).is_file(), "remove must retain managed data")
                self.assertEqual(live.read_bytes(), previous)

    def test_same_profile_name_can_exist_for_both_providers(self):
        codex = self.import_profile("codex", "personal")
        claude = self.import_profile("claude", "personal")
        self.assertNotEqual(codex["home"], claude["home"])
        self.assertEqual(len(self.profiles()), 2)

    def test_duplicate_import_does_not_overwrite_credentials(self):
        profile = self.import_profile("codex", "personal")
        managed = Path(profile["home"]) / "auth.json"
        before = managed.read_bytes()
        source = self.root / "replacement.json"
        source.write_text(json.dumps(self.auth("codex", "different")), encoding="utf-8")
        result = self.cli("import", "codex", "personal", "--from", str(source))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(managed.read_bytes(), before)

    def test_invalid_names_and_malformed_credentials_leave_store_empty(self):
        source = self.root / "valid.json"
        source.write_text(json.dumps(self.auth("codex", "fixture")), encoding="utf-8")
        for name in ("../escape", ".hidden", "space name", "slash/name", ""):
            with self.subTest(name=name):
                result = self.cli("import", "codex", name, "--from", str(source))
                self.assertNotEqual(result.returncode, 0)
        for data in ("not-json", "[]", "{}", '{"tokens": {}}'):
            source.write_text(data, encoding="utf-8")
            result = self.cli("import", "codex", "broken", "--from", str(source))
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.profiles(), [])

    def test_default_codex_import_uses_explicit_codex_home(self):
        source = Path(self.env["CODEX_HOME"])
        source.mkdir()
        (source / "auth.json").write_text(
            json.dumps(self.auth("codex", "source")), encoding="utf-8"
        )
        self.success("import", "codex", "personal")
        self.assertEqual(self.profiles("codex")[0]["name"], "personal")

    @unittest.skipUnless(sys.platform == "darwin", "macOS Keychain-specific import boundary")
    def test_macos_claude_file_import_is_rejected(self):
        source = self.root / "credentials.json"
        source.write_text(json.dumps(self.auth("claude", "fixture")), encoding="utf-8")
        result = self.cli("import", "claude", "personal", "--from", str(source))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("login claude", result.stderr)
        self.assertEqual(self.profiles(), [])

    @unittest.skipUnless(sys.platform == "darwin", "macOS native authentication verification")
    def test_macos_claude_login_requires_confirmed_native_auth_status(self):
        self.env["AA_TEST_LOGIN_AUTH"] = json.dumps(self.auth("claude", "fixture"))
        self.env["AA_TEST_LOGGED_IN"] = "0"
        result = self.cli("login", "claude", "personal")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.profiles(), [])
        self.assertEqual(
            [call["arguments"] for call in self.calls(include_claude_auth=True)],
            [["auth", "login"], ["auth", "status"]],
        )

    @unittest.skipIf(os.name == "nt", "POSIX permission and symlink contract")
    def test_credentials_are_private_and_symlink_import_is_rejected(self):
        profile = self.import_profile("codex", "personal")
        managed = Path(profile["home"]) / "auth.json"
        self.assertEqual(managed.stat().st_mode & 0o077, 0)
        self.assertEqual(Path(profile["home"]).stat().st_mode & 0o077, 0)
        self.assertEqual(self.store.stat().st_mode & 0o077, 0)
        link = self.root / "linked.json"
        link.symlink_to(managed)
        result = self.cli("import", "codex", "linked", "--from", str(link))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.profiles()), 1)

    def test_listing_settings_and_doctor_do_not_expose_tokens(self):
        self.import_profile("codex", "personal")
        self.import_profile("claude", "personal")
        for args in (("list",), ("list", "--json"), ("settings", "--json"), ("doctor", "--json")):
            with self.subTest(args=args):
                result = self.success(*args)
                for secret in (
                    "synthetic-access-personal",
                    "synthetic-refresh-personal",
                    "header.",
                ):
                    self.assertNotIn(secret, result.stdout + result.stderr)

    def test_codex_login_uses_fresh_home_and_device_auth(self):
        old = self.import_profile("codex", "existing")
        self.success("use", "codex", "existing")
        self.env["AA_TEST_LOGIN_AUTH"] = json.dumps(self.auth("codex", "new"))
        self.success("login", "codex", "new")
        records = self.calls()
        self.assertEqual(len(records), 1)
        self.assertIn("login", records[0]["arguments"])
        self.assertIn("--device-auth", records[0]["arguments"])
        self.assertNotEqual(records[0]["home"], old["home"])
        self.assertNotEqual(records[0]["home"], self.env["CODEX_HOME"])
        self.assertEqual({p["name"] for p in self.profiles("codex")}, {"existing", "new"})

    def test_failed_login_keeps_previous_active_profile_and_credentials(self):
        old = self.import_profile("codex", "existing")
        self.success("use", "codex", "existing")
        original = (Path(old["home"]) / "auth.json").read_bytes()
        self.env["AA_TEST_LOGIN_AUTH"] = json.dumps(self.auth("codex", "new"))
        self.env["AA_TEST_LOGIN_EXIT"] = "9"
        result = self.cli("login", "codex", "new")
        self.assertNotEqual(result.returncode, 0)
        profiles = self.profiles("codex")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["name"], "existing")
        self.assertTrue(profiles[0]["active"])
        self.assertEqual((Path(old["home"]) / "auth.json").read_bytes(), original)

    def test_login_duplicate_name_does_not_invoke_native_provider(self):
        self.import_profile("codex", "existing")
        result = self.cli("login", "codex", "existing")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_run_forwards_arguments_stdin_and_separate_output_streams(self):
        profile = self.import_profile("codex", "personal")
        self.success("use", "codex", "personal")
        self.configure_scenarios((profile, "echo"))
        self.env["AA_TEST_READ_STDIN"] = "1"
        args = [
            "exec",
            "literal space",
            "$(no-shell)",
            "semi;colon",
            "--flag=value",
            "&not-a-command",
            "pipe|literal",
            "%SYNTHETIC%",
            'embedded"quote',
        ]
        result = self.cli("run", "codex", "--", *args, input="synthetic stdin\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.calls()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["arguments"][-len(args) :], args)
        self.assertEqual(records[0]["stdin"], "synthetic stdin\n")
        self.assertIn("literal space", result.stdout)
        self.assertIn("fixture stderr stays stderr", result.stderr)
        self.assertNotIn("fixture stderr stays stderr", result.stdout)

    def test_run_account_override_uses_requested_managed_home(self):
        first, second = self.pair("claude", "success", "success")
        result = self.cli("run", "claude", "--account", "secondary", "--", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0]["home"], second["home"])
        self.assertNotEqual(self.calls()[0]["home"], first["home"])

    def test_run_refuses_conflicting_provider_credentials(self):
        for provider, key in (
            ("codex", "OPENAI_API_KEY"),
            ("codex", "CODEX_API_KEY"),
            ("claude", "ANTHROPIC_API_KEY"),
            ("claude", "ANTHROPIC_AUTH_TOKEN"),
            ("claude", "CLAUDE_CODE_OAUTH_TOKEN"),
        ):
            with self.subTest(provider=provider, key=key):
                if not self.profiles(provider):
                    self.import_profile(provider, "personal")
                    self.success("use", provider, "personal")
                env = dict(self.env, **{key: "synthetic-secret-conflict"})
                result = self.cli("run", provider, "--", "--version", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("synthetic-secret-conflict", result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_run_keeps_refreshed_credentials_inside_selected_profile(self):
        first, second = self.pair("codex", "refresh", "success")
        source = self.root / "synthetic-codex-primary.json"
        before_source = source.read_bytes()
        before_second = (Path(second["home"]) / "auth.json").read_bytes()
        result = self.batch("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads((Path(first["home"]) / "auth.json").read_text())
        self.assertEqual(updated["tokens"]["refresh_token"], "synthetic-refreshed-token")
        self.assertEqual(source.read_bytes(), before_source)
        self.assertEqual((Path(second["home"]) / "auth.json").read_bytes(), before_second)

    def test_rotation_off_does_not_retry_quota(self):
        self.pair("codex")
        result = self.batch("codex")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_rotation_can_be_enabled_by_persisted_settings(self):
        self.pair("codex")
        self.success("settings", "set", "auto-rotate", "on")
        result = self.batch("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 2)

    def test_codex_quota_before_work_rotates_once_and_records_cooldown(self):
        first, second = self.pair("codex")
        result = self.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["home"] for call in self.calls()], [first["home"], second["home"]])
        profiles = {p["name"]: p for p in self.profiles("codex")}
        self.assertTrue(profiles["secondary"]["active"])
        self.assertGreater(profiles["primary"]["cooldown_until"], time.time())

    def test_claude_rejected_rate_limit_then_failure_rotates(self):
        first, second = self.pair("claude")
        result = self.batch("claude", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["home"] for call in self.calls()], [first["home"], second["home"]])

    def test_rotation_exhaustion_is_bounded_to_one_attempt_per_profile(self):
        first, second = self.pair("codex", "quota", "quota")
        result = self.batch("codex", "--auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call["home"] for call in self.calls()], [first["home"], second["home"]])

    def test_rotation_never_crosses_provider_boundary(self):
        profile = self.import_profile("codex", "personal")
        self.import_profile("claude", "spare")
        self.success("use", "codex", "personal")
        self.configure_scenarios((profile, "quota"))
        result = self.batch("codex", "--auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call["provider"] for call in self.calls()], ["codex"])

    def test_after_tool_activity_rotation_selects_next_but_does_not_replay(self):
        self.pair("codex", "work_quota")
        result = self.batch("codex", "--auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        active = [p["name"] for p in self.profiles("codex") if p["active"]]
        self.assertEqual(active, ["secondary"])

    def test_after_assistant_activity_requires_explicit_replay(self):
        self.pair("claude", "assistant_quota")
        result = self.batch("claude", "--auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_allow_replay_explicitly_restarts_after_activity(self):
        self.pair("codex", "work_quota")
        result = self.batch("codex", "--auto-rotate", "--allow-replay")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 2)

    def test_generic_failure_does_not_rotate_and_preserves_exit_code(self):
        self.pair("codex", "generic_failure")
        result = self.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_auth_failure_does_not_rotate(self):
        self.pair("claude", "auth_failure")
        result = self.batch("claude", "--auto-rotate")
        self.assertEqual(result.returncode, 18, result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_cancellation_exit_does_not_rotate(self):
        self.pair("codex", "cancel")
        result = self.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_assistant_quota_text_does_not_trigger_rotation(self):
        self.pair("codex", "prose_quota_failure")
        result = self.batch("codex", "--auto-rotate", "--allow-replay")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_unstructured_quota_text_does_not_trigger_rotation(self):
        self.pair("codex", "plain_quota_failure")
        result = self.batch("codex", "--auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_claude_result_error_text_without_rejection_does_not_rotate(self):
        self.pair("claude", "claude_unstructured_result_failure")
        result = self.batch("claude", "--auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_malformed_protocol_disables_rotation_even_after_confirmed_quota(self):
        self.pair("codex", "malformed_then_quota")
        result = self.batch("codex", "--auto-rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_redirected_stdin_is_replayed_identically_after_prework_quota(self):
        self.pair("codex")
        self.env["AA_TEST_READ_STDIN"] = "1"
        prompt = "Synthetic piped prompt — Unicode and newline\n" * 100
        result = self.cli(
            "run", "codex", "--auto-rotate", "--", "exec", "--json", "-", input=prompt
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["stdin"] for call in self.calls()], [prompt, prompt])

    def test_concurrent_run_of_same_profile_fails_without_starting_second_child(self):
        self.pair("codex", "hold")
        process = self.holding_run()
        result = self.batch("codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("busy", result.stderr.lower())
        self.assertEqual(len(self.calls()), 1)
        self.success("settings", "set", "cooldown-seconds", "46")
        Path(self.env["AA_TEST_RELEASE"]).touch()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_different_profiles_can_run_concurrently(self):
        first, second = self.pair("codex", "hold")
        process = self.holding_run()
        result = self.cli("run", "codex", "--account", "secondary", "--", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["home"] for call in self.calls()], [first["home"], second["home"]])
        Path(self.env["AA_TEST_RELEASE"]).touch()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_rotation_skips_busy_profile_and_uses_available_later_profile(self):
        primary, secondary = self.pair("codex", "quota", "hold")
        tertiary = self.import_profile("codex", "tertiary")
        self.success("use", "codex", "secondary")
        process = self.holding_run()
        self.success("use", "codex", "primary")
        result = self.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call["home"] for call in self.calls()],
            [secondary["home"], primary["home"], tertiary["home"]],
        )
        Path(self.env["AA_TEST_RELEASE"]).touch()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_closed_output_pipe_stops_cleanly_with_broken_pipe_status(self):
        profile = self.import_profile("codex", "personal")
        self.configure_scenarios((profile, "flood"))
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_accounts",
                "run",
                "codex",
                "--auto-rotate",
                "--",
                "exec",
                "--json",
                "synthetic task",
            ],
            cwd=self.root,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(process.stdout.readline())
            process.stdout.close()
            process.stdout = None
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 141, error.decode())
            self.assertNotIn(b"Exception ignored while flushing", error)
            if os.name == "nt":
                # The native .exe fixture launcher starts a Python descendant.
                # Closing the consumer must terminate that process too.
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                kernel.WaitForSingleObject.restype = wintypes.DWORD
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel.CloseHandle.restype = wintypes.BOOL
                child = kernel.OpenProcess(0x00100000, False, self.calls()[0]["pid"])
                if child:
                    try:
                        self.assertEqual(
                            kernel.WaitForSingleObject(child, 0),
                            0,
                            "Native fixture child survived wrapper shutdown",
                        )
                    finally:
                        kernel.CloseHandle(child)
                else:
                    self.assertEqual(ctypes.get_last_error(), 87, "Could not inspect native child")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX signal forwarding")
    def test_sigint_stops_native_child_without_rotating(self):
        self.pair("codex", "hold")
        process = self.holding_run(rotate=True)
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(Path(self.env["AA_TEST_EXITED"]).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(self.calls()[0]["pid"], 0)

    @unittest.skipIf(os.name == "nt", "POSIX signal forwarding")
    def test_sigterm_stops_native_child_without_rotating(self):
        self.pair("codex", "hold")
        process = self.holding_run(rotate=True)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(Path(self.env["AA_TEST_EXITED"]).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(self.calls()[0]["pid"], 0)

    def test_claude_warning_does_not_rotate(self):
        self.pair("claude", "warning_then_failure")
        result = self.batch("claude", "--auto-rotate")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_terminal_success_overrides_earlier_limit_signal(self):
        self.pair("claude", "rejection_then_success")
        result = self.batch("claude", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_missing_native_binary_is_actionable_without_mutating_profiles(self):
        self.import_profile("codex", "personal")
        self.success("use", "codex", "personal")
        before = self.profiles("codex")
        missing = dict(self.env, PATH=str(self.root / "no binaries"))
        result = self.cli("run", "codex", "--", "--version", env=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("codex", result.stderr.lower())
        self.assertEqual(self.profiles("codex"), before)

    @unittest.skipUnless(os.name == "nt", "Windows batch shim boundary")
    def test_windows_batch_shim_is_refused_without_running_it(self):
        self.import_profile("codex", "personal")
        (self.bin / "codex.exe").rename(self.bin / "disabled-codex.fixture")
        (self.bin / "codex.cmd").write_text("@exit /b 0\n", encoding="utf-8")
        result = self.cli("run", "codex", "--", "--version")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".exe", result.stderr.lower())
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(os.name == "nt", "Windows native executable preference")
    def test_windows_native_binary_is_preferred_over_earlier_batch_shim(self):
        self.import_profile("codex", "personal")
        shims = self.root / "earlier shims"
        shims.mkdir()
        (shims / "codex.cmd").write_text("@exit /b 98\n", encoding="utf-8")
        env = dict(self.env, PATH=str(shims) + os.pathsep + self.env["PATH"])
        result = self.cli("run", "codex", "--", "--version", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
