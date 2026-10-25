"""Native quota probes have deadlines and bounded output without leaking children."""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class UsageProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = Path(__file__).with_name("fixture_codex_usage.py")

    def test_unresponsive_child_is_stopped_within_bounded_time(self):
        from agent_accounts.usage_process import JsonProcess, UsageUnavailable

        started = time.monotonic()
        peer = JsonProcess(
            [sys.executable, str(self.fixture), "silence"],
            dict(os.environ),
            timeout=0.15,
            cwd=self.root,
        )
        with self.assertRaises(UsageUnavailable):
            with peer:
                peer.receive()
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(peer.process.poll())

    def test_oversized_native_output_is_stopped_without_unbounded_buffering(self):
        from agent_accounts.usage_process import JsonProcess, UsageUnavailable

        peer = JsonProcess(
            [sys.executable, str(self.fixture), "flood"], dict(os.environ), timeout=2
        )
        with self.assertRaises(UsageUnavailable):
            with peer:
                peer.receive()
        self.assertIsNotNone(peer.process.poll())

    def test_child_that_does_not_read_stdin_cannot_block_send_forever(self):
        from agent_accounts.usage_process import JsonProcess, UsageUnavailable

        started = time.monotonic()
        peer = JsonProcess(
            [sys.executable, str(self.fixture), "silence"], dict(os.environ), timeout=0.15
        )
        with self.assertRaises(UsageUnavailable):
            with peer:
                for _ in range(10):
                    peer.send({"synthetic": "x" * 60000})
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(peer.process.poll())

    def test_notification_count_is_bounded(self):
        from agent_accounts.usage_process import JsonProcess, UsageUnavailable

        count = 0
        peer = JsonProcess(
            [sys.executable, str(self.fixture), "message_flood"], dict(os.environ), timeout=2
        )
        with self.assertRaises(UsageUnavailable):
            with peer:
                while True:
                    peer.receive()
                    count += 1
        self.assertLessEqual(count, 256)
        self.assertIsNotNone(peer.process.poll())

    def test_partial_pipe_writes_and_fragmented_unicode_response_preserve_message(self):
        from agent_accounts.usage_process import JsonProcess

        original_write = os.write
        message = {"value": "é" * 2000}
        with patch(
            "agent_accounts.usage_process.os.write",
            side_effect=lambda descriptor, data: original_write(descriptor, data[:257]),
        ):
            with JsonProcess(
                [sys.executable, str(self.fixture), "echo_fragments"], dict(os.environ), timeout=2
            ) as peer:
                peer.send(message)
                self.assertEqual(peer.receive(), message)

    def test_cleanup_does_not_recycle_descriptor_while_reader_can_still_use_it(self):
        from agent_accounts.usage_process import JsonProcess, UsageUnavailable

        original_read = os.read
        entered = threading.Event()
        release = threading.Event()
        descriptors = []

        def paused_read(descriptor, size):
            if threading.current_thread() is not threading.main_thread():
                descriptors.append(descriptor)
                entered.set()
                release.wait(timeout=5)
            return original_read(descriptor, size)

        peer = JsonProcess(
            [sys.executable, str(self.fixture), "silence"], dict(os.environ), timeout=0.15
        )
        with patch("agent_accounts.usage_process.os.read", side_effect=paused_read):
            try:
                with self.assertRaises(UsageUnavailable):
                    with peer:
                        self.assertTrue(entered.wait(timeout=1))
                        peer.receive()
                # The worker passed its stop check but has not yet called read.
                # Closing now would let another probe reuse this descriptor.
                os.fstat(descriptors[0])
                replacement = os.pipe()
                try:
                    self.assertNotIn(descriptors[0], replacement)
                finally:
                    for descriptor in replacement:
                        os.close(descriptor)
            finally:
                release.set()
                for thread in peer._threads:
                    thread.join(timeout=2)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_darwin_exit_race_reaps_before_retrying_group_cleanup(self):
        from agent_accounts.usage_process import JsonProcess

        peer = JsonProcess([], {})
        peer._handles_signals = False
        peer.process = MagicMock()
        peer.process.stdin = None
        peer.process.stdout = None
        peer.process.poll.return_value = None

        def reap(*_args, **_kwargs):
            peer.process.poll.return_value = 0
            return 0

        peer.process.wait.side_effect = reap
        with (
            patch("agent_accounts.usage_process.sys.platform", "darwin"),
            patch("agent_accounts.runner._stop", side_effect=[PermissionError(), None]) as stop,
        ):
            peer.__exit__(None, None, None)
        peer.process.wait.assert_called_once()
        self.assertEqual(stop.call_count, 2)

    @unittest.skipIf(os.name == "nt", "POSIX detached process group")
    def test_detached_descendant_holding_pipe_cannot_block_probe_cleanup(self):
        pidfile = self.root / "detached.pid"
        native_pidfile = self.root / "native.pid"
        host = subprocess.Popen(
            [sys.executable, str(self.fixture), "detached_host"],
            env=dict(
                os.environ,
                USAGE_TEST_DETACHED_PID=str(pidfile),
                USAGE_TEST_PID=str(native_pidfile),
                PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            try:
                output, error = host.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                self.fail("Probe cleanup blocked on a detached descendant's inherited pipe")
            self.assertEqual(host.returncode, 0, error.decode())
            self.assertEqual(output.strip(), b"unavailable")
            self.assertTrue(pidfile.exists(), "Fixture must leave a detached pipe owner alive")
            os.kill(int(pidfile.read_text()), 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(native_pidfile.read_text()), 0)
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if host.poll() is None:
                host.kill()
            host.communicate(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX signal forwarding")
    def test_sigterm_and_sigint_preserve_status_and_stop_native_probe(self):
        for requested, expected in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.subTest(signal=requested):
                pidfile = self.root / f"native-{requested}.pid"
                env = dict(
                    os.environ,
                    USAGE_TEST_PID=str(pidfile),
                    PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                )
                host = subprocess.Popen(
                    [sys.executable, str(self.fixture), "probe_host"],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(pidfile.exists())
                    pid = int(pidfile.read_text())
                    host.send_signal(requested)
                    _, error = host.communicate(timeout=5)
                    self.assertEqual(host.returncode, expected, error.decode())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                finally:
                    if host.poll() is None:
                        host.kill()
                        host.communicate(timeout=5)
