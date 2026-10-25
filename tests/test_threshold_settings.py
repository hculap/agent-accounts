"""Provider quota thresholds remain independent and upgrade old state without writes."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_accounts import cli
from agent_accounts.store import AccountError, Store, atomic_write


class ThresholdSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-accounts-threshold-settings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / "private store")

    def command(self, *arguments):
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                code = cli.main(["--home", str(self.store.home), *arguments])
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), error.getvalue()

    def legacy_state(self):
        return {
            "version": 1,
            "settings": {"auto_rotate": False, "cooldown_seconds": 300},
            "active": {"codex": "existing"},
            "profiles": [
                {
                    "provider": "codex",
                    "name": "existing",
                    "id": "a" * 32,
                    "cooldown_until": 0,
                }
            ],
        }

    def raw_state(self, state):
        path = self.store.home / "state.json"
        data = json.dumps(state).encode()
        atomic_write(path, data)
        return path, data

    def test_fresh_settings_default_each_provider_to_95_without_enabling_rotation(self):
        code, output, error = self.command("settings", "--json")
        self.assertEqual(code, 0, error)
        settings = json.loads(output)
        self.assertEqual(settings["codex_rotation_threshold"], 95)
        self.assertEqual(settings["claude_rotation_threshold"], 95)
        self.assertIs(settings["auto_rotate"], False)
        self.assertFalse(self.store.home.exists(), "Reading defaults must not initialize the store")

    def test_legacy_read_supplies_defaults_without_rewriting_the_file(self):
        legacy = self.legacy_state()
        path, original = self.raw_state(legacy)
        before = path.stat()

        state = self.store.read()

        self.assertEqual(state["settings"]["codex_rotation_threshold"], 95)
        self.assertEqual(state["settings"]["claude_rotation_threshold"], 95)
        self.assertEqual(state["profiles"], legacy["profiles"])
        self.assertEqual(state["active"], legacy["active"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_partial_upgrade_preserves_an_explicit_provider_threshold(self):
        for provider, other in (("codex", "claude"), ("claude", "codex")):
            with self.subTest(provider=provider):
                legacy = self.legacy_state()
                legacy["settings"][f"{provider}_rotation_threshold"] = 82
                path, original = self.raw_state(legacy)
                settings = self.store.read()["settings"]
                self.assertEqual(settings[f"{provider}_rotation_threshold"], 82)
                self.assertEqual(settings[f"{other}_rotation_threshold"], 95)
                self.assertEqual(path.read_bytes(), original)

    def test_present_malformed_threshold_is_not_replaced_by_a_default(self):
        for provider in ("codex", "claude"):
            for value in (None, True, False, "95", 95.0, 0, 101, -1, 10**1000, [], {}):
                with self.subTest(provider=provider, value=value):
                    state = self.legacy_state()
                    state["settings"][f"{provider}_rotation_threshold"] = value
                    path, original = self.raw_state(state)
                    with self.assertRaises(AccountError):
                        self.store.read()
                    with self.assertRaises(AccountError):
                        self.store.write(state)
                    self.assertEqual(path.read_bytes(), original)

    def test_cli_updates_each_provider_and_preserves_existing_account_state(self):
        legacy = self.legacy_state()
        self.raw_state(legacy)
        for provider, value in (("codex", "80"), ("claude", "90")):
            code, _output, error = self.command(
                "settings", "set", f"{provider}-rotation-threshold", value
            )
            self.assertEqual(code, 0, error)

        state = self.store.read()
        self.assertEqual(state["settings"]["codex_rotation_threshold"], 80)
        self.assertEqual(state["settings"]["claude_rotation_threshold"], 90)
        self.assertIs(state["settings"]["auto_rotate"], False)
        self.assertEqual(state["profiles"], legacy["profiles"])
        self.assertEqual(state["active"], legacy["active"])
        persisted = json.loads((self.store.home / "state.json").read_bytes())
        self.assertEqual(persisted["settings"], state["settings"])

    def test_threshold_bounds_are_accepted_and_bad_cli_values_do_not_mutate_state(self):
        for provider in ("codex", "claude"):
            key = f"{provider}-rotation-threshold"
            for value in ("1", "100"):
                with self.subTest(provider=provider, accepted=value):
                    code, _output, error = self.command("settings", "set", key, value)
                    self.assertEqual(code, 0, error)
                    self.assertEqual(
                        self.store.read()["settings"][key.replace("-", "_")], int(value)
                    )
            path = self.store.home / "state.json"
            original = path.read_bytes()
            for value in ("0", "101", "-1", "95.5", "on", "95%", ""):
                with self.subTest(provider=provider, rejected=value):
                    code, _output, error = self.command("settings", "set", key, value)
                    self.assertNotEqual(code, 0)
                    self.assertIn("1", error)
                    self.assertIn("100", error)
                    self.assertEqual(path.read_bytes(), original)

    def test_auto_rotation_toggle_does_not_reset_custom_thresholds(self):
        state = self.legacy_state()
        state["settings"].update(codex_rotation_threshold=85, claude_rotation_threshold=75)
        self.raw_state(state)
        for value in ("on", "off"):
            code, output, error = self.command("settings", "set", "auto-rotate", value)
            self.assertEqual(code, 0, error)
            settings = self.store.read()["settings"]
            self.assertEqual(settings["codex_rotation_threshold"], 85)
            self.assertEqual(settings["claude_rotation_threshold"], 75)
            self.assertEqual(settings["auto_rotate"], value == "on")
            self.assertIn("codex-rotation-threshold: 85%", output)
            self.assertIn("claude-rotation-threshold: 75%", output)


if __name__ == "__main__":
    unittest.main()
