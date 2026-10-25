"""Independent storage and CLI review regressions using only synthetic state."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_accounts import cli, providers
from agent_accounts.store import AccountError, Store, atomic_write


class StoreRoundTwoReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="agent-accounts-store-review-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = Store(self.root / "private store")

    def add_existing_profile(self):
        state = self.store.read()
        profile = self.store.create_profile(state, "codex", "existing")
        state["profiles"].append(profile)
        state["active"]["codex"] = profile["name"]
        with self.store.lock():
            self.store.write(state)
        return profile

    def test_unrepresentable_integer_cooldown_is_a_safe_state_error(self):
        self.add_existing_profile()
        state = self.store.read()
        state["profiles"][0]["cooldown_until"] = 10**1000
        path = self.store.home / "state.json"
        original = json.dumps(state).encode()
        atomic_write(path, original)

        with self.assertRaises(AccountError):
            self.store.read()
        self.assertEqual(path.read_bytes(), original)

    def test_native_login_does_not_block_other_settings_and_merges_them(self):
        self.add_existing_profile()
        concurrent_codes = []

        def authenticate(_argv, env):
            # This occurs while the browser/native login is pending. Opening a
            # second store lock models another CLI invocation without timing races.
            concurrent_codes.append(
                cli.main(
                    [
                        "--home",
                        str(self.store.home),
                        "settings",
                        "set",
                        "cooldown-seconds",
                        "901",
                    ]
                )
            )
            credentials = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "synthetic-new-account",
                    "access_token": "synthetic-access-token",
                },
            }
            atomic_write(Path(env["CODEX_HOME"]) / "auth.json", json.dumps(credentials).encode())
            return 0

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(providers, "executable", return_value="synthetic-codex"),
            patch.object(cli, "launch", side_effect=authenticate),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.add_account(self.store, "codex", "new", None, [])

        self.assertEqual(code, 0)
        self.assertEqual(concurrent_codes, [0], "Browser login held the global metadata lock")
        saved = self.store.read()
        self.assertEqual(saved["settings"]["cooldown_seconds"], 901)
        self.assertEqual({p["name"] for p in saved["profiles"]}, {"existing", "new"})

    def test_pending_login_locks_same_casefold_name_but_allows_another_registration(self):
        self.add_existing_profile()
        credentials = {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": "synthetic-new-account",
                "access_token": "synthetic-access-token",
            },
        }
        source = self.root / "synthetic-source.json"
        source.write_text(json.dumps(credentials), encoding="utf-8")
        concurrent_codes = []
        launch_count = 0

        def authenticate(_argv, env):
            nonlocal launch_count
            launch_count += 1
            if launch_count > 1:
                self.fail("A duplicate registration started a second native login")
            concurrent_codes.append(
                cli.main(["--home", str(self.store.home), "login", "codex", "NeW"])
            )
            concurrent_codes.append(
                cli.main(
                    [
                        "--home",
                        str(self.store.home),
                        "import",
                        "codex",
                        "other",
                        "--from",
                        str(source),
                    ]
                )
            )
            atomic_write(Path(env["CODEX_HOME"]) / "auth.json", source.read_bytes())
            return 0

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(providers, "executable", return_value="synthetic-codex"),
            patch.object(cli, "launch", side_effect=authenticate),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.add_account(self.store, "codex", "new", None, [])

        self.assertEqual(code, 0)
        self.assertEqual(concurrent_codes, [1, 0])
        self.assertEqual(launch_count, 1)
        self.assertEqual(
            {p["name"] for p in self.store.read()["profiles"]}, {"existing", "new", "other"}
        )

    def test_nonrun_commands_handle_closed_stdout_without_shutdown_traceback(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        for args in (("list",), ("settings",), ("doctor",), ("--help",)):
            with self.subTest(command=args):
                child = subprocess.Popen(
                    [sys.executable, "-m", "agent_accounts", "--home", str(self.store.home), *args],
                    cwd=self.root,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertIsNotNone(child.stdout)
                child.stdout.close()
                child.stdout = None
                _stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 141, stderr.decode(errors="replace"))
                self.assertEqual(stderr, b"")

    def test_windows_closed_pipe_during_print_is_silent(self):
        class ClosedWindowsPipe:
            def write(self, _value):
                raise OSError(errno.EINVAL, "synthetic closed Windows pipe")

            def flush(self):
                raise OSError(errno.EINVAL, "synthetic closed Windows pipe")

            def fileno(self):
                return 1

        store = Mock()
        store.list_profiles.return_value = []
        errors = io.StringIO()
        with (
            patch.object(cli, "Store", return_value=store),
            patch.object(cli.os, "name", "nt"),
            patch.object(cli.os, "dup2"),
            contextlib.redirect_stdout(ClosedWindowsPipe()),
            contextlib.redirect_stderr(errors),
        ):
            code = cli.main(["list"])
        self.assertEqual(code, 141)
        self.assertEqual(errors.getvalue(), "")

    @unittest.skipIf(os.name == "nt", "POSIX symlink creation without administrator privileges")
    def test_remove_does_not_report_failure_after_committing_registration_deletion(self):
        profile = self.add_existing_profile()
        home = self.store.profile_home(profile)
        home.parent.mkdir(parents=True)
        target = self.root / "unrelated directory"
        target.mkdir()
        home.symlink_to(target, target_is_directory=True)
        state_path = self.store.home / "state.json"
        original = state_path.read_bytes()

        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(["--home", str(self.store.home), "remove", "codex", "existing"])

        if code:
            self.assertEqual(
                state_path.read_bytes(),
                original,
                "The failed remove command silently deleted the account registration",
            )
        else:
            self.assertEqual(self.store.read()["profiles"], [])
        self.assertTrue(home.is_symlink())
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
