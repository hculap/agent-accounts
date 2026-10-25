"""Persistence failures must preserve credentials and never silently reset state."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_accounts.providers import LimitDetector
from agent_accounts.store import AccountError, Store, atomic_write, valid_name


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / "private store")

    def test_replace_failure_keeps_previous_complete_file_and_removes_temporary(self):
        target = self.root / "private store" / "credentials.json"
        atomic_write(target, b"original synthetic credentials")
        with patch(
            "agent_accounts.store.os.replace", side_effect=OSError("synthetic disk failure")
        ):
            with self.assertRaises(OSError):
                atomic_write(target, b"replacement synthetic credentials")
        self.assertEqual(target.read_bytes(), b"original synthetic credentials")
        self.assertEqual(list(target.parent.glob(".write-*")), [])

    def test_corrupt_state_is_not_reinitialized(self):
        path = self.store.home / "state.json"
        original = b'{"corrupt":'
        atomic_write(path, original)
        with self.assertRaises(AccountError):
            self.store.read()
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_schema_and_nonfinite_cooldown_fail_closed(self):
        initial = self.store.read()
        profile = self.store.create_profile(initial, "codex", "personal")
        initial["profiles"].append(profile)
        for mutate in (
            lambda state: state.update(version=True),
            lambda state: state["profiles"][0].pop("cooldown_until"),
            lambda state: state["profiles"][0].update(cooldown_until=float("nan")),
            lambda state: state["profiles"][0].update(id="../../outside"),
            lambda state: state["active"].update(codex="missing"),
            lambda state: state["settings"].update(auto_rotate="false"),
            lambda state: state["profiles"].append(dict(state["profiles"][0])),
        ):
            state = json.loads(json.dumps(initial))
            mutate(state)
            with self.subTest(state=state), self.assertRaises(AccountError):
                self.store.write(state)

    def test_deeply_nested_json_is_a_safe_parse_failure(self):
        path = self.store.home / "state.json"
        atomic_write(path, b'{"data":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}")
        with self.assertRaises(AccountError):
            self.store.read()
        detector = LimitDetector("codex")
        # JSON recursion limits differ across Python releases. Exercise the
        # parser failure explicitly instead of assuming a fixed nesting limit.
        with patch("json.loads", side_effect=RecursionError):
            detector.feed(b'{"synthetic": "deep event"}')
        self.assertFalse(detector.limited(1))
        self.assertTrue(detector.malformed)

    @unittest.skipIf(os.name == "nt", "POSIX symlink and permission assertions")
    def test_managed_symlink_does_not_modify_its_target(self):
        target = self.root / "target"
        target.mkdir(mode=0o755)
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(AccountError):
            with Store(alias).lock():
                self.fail("Managed symlink was followed")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX private modes")
    def test_permissive_umask_does_not_expose_new_files(self):
        previous = os.umask(0)
        try:
            with self.store.lock():
                self.store.write(self.store.read())
        finally:
            os.umask(previous)
        self.assertEqual(self.store.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store.home / "state.json").stat().st_mode & 0o777, 0o600)

    def test_case_alias_and_windows_reserved_names_are_rejected(self):
        state = self.store.read()
        state["profiles"].append(self.store.create_profile(state, "codex", "Work"))
        with self.assertRaises(AccountError):
            self.store.create_profile(state, "codex", "work")
        for name in ("CON", "nul.json", "COM1", "LPT9", "work.", "../work", "a" * 65):
            with self.subTest(name=name), self.assertRaises(AccountError):
                valid_name(name)
