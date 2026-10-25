"""Independent second-pass regressions for process lifecycle and replay boundaries."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_acceptance as _acceptance


@unittest.skipIf(os.name == "nt", "POSIX process-group integration scenarios")
class RunnerRoundTwoTests(unittest.TestCase):
    def setUp(self):
        self.h = _acceptance.AcceptanceTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        fixture = Path(__file__).with_name("fixture_review_runner_round2.py")
        native = self.h.bin / "codex"
        native.write_text(
            f"#!{sys.executable}\n" + fixture.read_text(encoding="utf-8"), encoding="utf-8"
        )
        native.chmod(0o700)
        for key in ("READY", "CHILD", "HEARTBEAT"):
            self.h.env["AA_R2_" + key] = str(self.h.root / key.lower())

    def wait_ready(self):
        path = Path(self.h.env["AA_R2_READY"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                time.sleep(0.01)
        self.fail("Synthetic provider did not become ready")

    def start(self, mode, *, rotate=True):
        self.h.env["AA_R2_MODE"] = mode
        options = ["--auto-rotate"] if rotate else ["--no-auto-rotate"]
        wrapper = subprocess.Popen(
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
            env=self.h.env,
            cwd=self.h.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        info = self.wait_ready()

        def cleanup():
            # Both groups are created only for this synthetic test. Never signal
            # the test runner's own process group.
            for group in {info["group"], wrapper.pid}:
                self.assertNotEqual(group, os.getpgrp())
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            wrapper.communicate(timeout=5)

        self.addCleanup(cleanup)
        return wrapper, info

    def heartbeat(self):
        path = Path(self.h.env["AA_R2_HEARTBEAT"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                return int(path.read_text())
            except (FileNotFoundError, ValueError):
                time.sleep(0.01)
        self.fail("Synthetic descendant heartbeat missing")

    def assert_descendant_stopped(self):
        before = self.heartbeat()
        time.sleep(0.15)
        self.assertEqual(
            self.heartbeat(),
            before,
            "A native descendant continued executing after wrapper cancellation",
        )

    def test_cancellation_stops_descendants_after_native_leader_already_exited(self):
        self.h.import_profile("codex", "personal")
        wrapper, _ = self.start("leader_exits")
        # The synthetic leader exits immediately after readiness. Give its
        # descendant several heartbeat intervals before cancelling the wrapper.
        time.sleep(0.1)
        wrapper.send_signal(signal.SIGTERM)
        wrapper.communicate(timeout=3)
        self.assertEqual(wrapper.returncode, 143)
        self.assert_descendant_stopped()

    def test_cancellation_stops_descendant_that_ignores_sigterm(self):
        self.h.import_profile("codex", "personal")
        self.h.env["AA_R2_IGNORE_TERM"] = "1"
        wrapper, _ = self.start("leader_waits")
        wrapper.send_signal(signal.SIGTERM)
        wrapper.communicate(timeout=5)
        self.assertEqual(wrapper.returncode, 143)
        self.assert_descendant_stopped()

    def test_nonrotating_batch_cancellation_stops_native_descendant(self):
        self.h.import_profile("codex", "personal")
        wrapper, _ = self.start("leader_waits", rotate=False)
        wrapper.send_signal(signal.SIGTERM)
        # Wait for the wrapper itself: a surviving descendant still owns its
        # output pipe, so communicate() would otherwise hide the failure in a timeout.
        wrapper.wait(timeout=5)
        self.assertEqual(wrapper.returncode, 143)
        self.assert_descendant_stopped()

    def test_sigkill_after_quota_output_does_not_trigger_retry(self):
        primary = self.h.import_profile("codex", "primary")
        self.h.import_profile("codex", "secondary")
        self.h.success("use", "codex", "primary")
        self.h.env["AA_R2_PRIMARY_HOME"] = primary["home"]
        wrapper, info = self.start("quota_then_signal")
        os.kill(info["pid"], signal.SIGKILL)
        wrapper.communicate(timeout=5)
        self.assertEqual(wrapper.returncode, 137, "Cancellation was replaced by fallback success")
        calls = [json.loads(line) for line in self.h.calls_file.read_text().splitlines()]
        self.assertEqual(len(calls), 1, "A cancelled run was automatically replayed")

    def test_busy_explicit_account_does_not_switch_without_a_quota_failure(self):
        primary = self.h.import_profile("codex", "primary")
        self.h.import_profile("codex", "secondary")
        self.h.success("use", "codex", "primary")
        self.h.env["AA_R2_PRIMARY_HOME"] = primary["home"]
        self.start("hold_primary", rotate=False)
        result = self.h.batch("codex", "--auto-rotate", "--account", "primary")
        self.assertNotEqual(
            result.returncode,
            0,
            "A busy account silently selected another account without hitting quota",
        )
        calls = [json.loads(line) for line in self.h.calls_file.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
