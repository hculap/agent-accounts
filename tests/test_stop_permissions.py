"""Process cleanup must not silently ignore unexpected permission failures."""

import os
import signal
import unittest
from unittest.mock import MagicMock, patch

from agent_accounts.runner import _stop


@unittest.skipIf(os.name == "nt", "POSIX process-group permission handling")
class StopPermissionTests(unittest.TestCase):
    def test_non_macos_permission_failure_is_not_silenced(self):
        process = MagicMock(pid=12345)
        process.poll.return_value = 0
        with (
            patch("agent_accounts.runner.sys.platform", "linux"),
            patch("agent_accounts.runner.os.killpg", side_effect=[None, PermissionError]),
        ):
            with self.assertRaises(PermissionError):
                _stop(process, signal.SIGTERM, grouped=True)

    def test_reaped_macos_group_permission_race_keeps_cancellation_status(self):
        process = MagicMock(pid=12345)
        process.poll.return_value = 0
        with (
            patch("agent_accounts.runner.sys.platform", "darwin"),
            patch("agent_accounts.runner.os.killpg", side_effect=[None, PermissionError]),
        ):
            _stop(process, signal.SIGTERM, grouped=True)
