"""Windows batch launchers must never receive unescaped agent prompt text."""

import os
import unittest
from unittest.mock import MagicMock, patch

from agent_accounts.providers import executable
from agent_accounts.runner import _stop
from agent_accounts.store import AccountError


class WindowsLaunchTests(unittest.TestCase):
    def test_windows_cancellation_stops_the_native_process_tree(self):
        process = MagicMock(pid=12345)
        process.poll.return_value = None
        with (
            patch.object(os, "name", "nt"),
            patch.dict(os.environ, {"SystemRoot": "C:/Windows"}),
            patch("agent_accounts.runner.subprocess.run") as kill_tree,
        ):
            _stop(process, 15, grouped=False)
        self.assertTrue(kill_tree.called)
        args = kill_tree.call_args.args[0]
        self.assertTrue(args[0].lower().endswith("taskkill.exe"))
        self.assertEqual(args[1:], ["/PID", "12345", "/T", "/F"])
        self.assertLessEqual(kill_tree.call_args.kwargs["timeout"], 3)
        process.wait.assert_called()

    def test_batch_shim_is_rejected_without_echoing_arguments(self):
        for suffix in ("cmd", "BAT"):
            with (
                self.subTest(suffix=suffix),
                patch.object(os, "name", "nt"),
                patch(
                    "agent_accounts.providers.shutil.which",
                    side_effect=lambda name: (
                        None if name.endswith(".exe") else f"C:/bin/codex.{suffix}"
                    ),
                ),
            ):
                with self.assertRaises(AccountError):
                    executable("codex")

    def test_native_executable_is_preferred_over_batch_shim(self):
        def which(name):
            return "C:/bin/codex.exe" if name.endswith(".exe") else "C:/bin/codex.cmd"

        with (
            patch.object(os, "name", "nt"),
            patch("agent_accounts.providers.shutil.which", side_effect=which),
        ):
            self.assertEqual(executable("codex"), "C:/bin/codex.exe")
