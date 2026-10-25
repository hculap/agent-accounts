"""Bounded NDJSON exchanges with short-lived native quota readers."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

MAX_LINE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 4 * MAX_LINE_BYTES
MAX_MESSAGES = 256
MAX_REQUEST_BYTES = 64 * 1024


class UsageUnavailable(Exception):
    """The native CLI could not provide a bounded, valid usage response."""


class JsonProcess:
    """Deadline-bound reads and writes, including Windows pipes without select()."""

    def __init__(self, command, env, *, timeout=8.0, cwd=None):
        self.command = command
        self.env = env
        self.timeout = timeout
        self.cwd = cwd
        self.process = None
        self._incoming = queue.Queue(maxsize=MAX_MESSAGES + 1)
        self._outgoing = queue.Queue(maxsize=1)
        self._threads = []
        self._stopping = threading.Event()
        self._previous_term = None
        self._handles_signals = threading.current_thread() is threading.main_thread()

    def __enter__(self):
        self._deadline = time.monotonic() + self.timeout
        if self._handles_signals:
            self._previous_term = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._terminate)
        try:
            self.process = subprocess.Popen(
                self.command,
                env=self.env,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                start_new_session=os.name != "nt",
            )
            for target in (self._reader, self._writer):
                thread = threading.Thread(target=target, daemon=True)
                thread.start()
                self._threads.append(thread)
            return self
        except OSError as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise UsageUnavailable("Native usage reader could not start.") from None
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    @staticmethod
    def _terminate(_number, _frame):
        # Cleanup runs after blocked queue/wait operations unwind, never inside
        # the signal handler where subprocess locks could be held.
        raise SystemExit(143)

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise UsageUnavailable("Native usage reader timed out.")
        return remaining

    def _reader(self):
        total = 0
        messages = 0
        pending = b""
        stream = self.process.stdout

        def emit_line(line):
            nonlocal messages
            messages += 1
            if len(line) > MAX_LINE_BYTES or messages > MAX_MESSAGES:
                raise UsageUnavailable("Native usage output exceeded its limit.")
            self._incoming.put_nowait(line)

        try:
            descriptor = stream.fileno()
            while not self._stopping.is_set():
                chunk = os.read(descriptor, 65536)
                if self._stopping.is_set():
                    return
                if not chunk:
                    if pending:
                        emit_line(pending)
                    self._incoming.put_nowait(UsageUnavailable("Native usage reader ended."))
                    return
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    raise UsageUnavailable("Native usage output exceeded its limit.")
                pending += chunk
                while (boundary := pending.find(b"\n")) >= 0:
                    emit_line(pending[: boundary + 1])
                    pending = pending[boundary + 1 :]
                if len(pending) > MAX_LINE_BYTES:
                    raise UsageUnavailable("Native usage output exceeded its limit.")
        except UsageUnavailable as exc:
            try:
                self._incoming.put_nowait(exc)
            except queue.Full:
                pass
        except (OSError, ValueError, queue.Full):
            try:
                self._incoming.put_nowait(UsageUnavailable("Native usage output was interrupted."))
            except queue.Full:
                pass
        finally:
            self._close_stream(stream)

    def _writer(self):
        stream = self.process.stdin
        try:
            descriptor = stream.fileno()
            while not self._stopping.is_set():
                item = self._outgoing.get()
                if item is None:
                    return
                data, acknowledgement = item
                try:
                    remaining = memoryview(data)
                    while remaining:
                        if self._stopping.is_set():
                            return
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("Native usage input was interrupted.")
                        remaining = remaining[written:]
                    acknowledgement.put_nowait(None)
                except (OSError, ValueError):
                    acknowledgement.put_nowait(
                        UsageUnavailable("Native usage input was interrupted.")
                    )
        finally:
            self._close_stream(stream)

    @staticmethod
    def _close_stream(stream):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def send(self, message: dict):
        try:
            data = (json.dumps(message, allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise UsageUnavailable("Invalid native usage request.") from None
        if len(data) > MAX_REQUEST_BYTES:
            raise UsageUnavailable("Native usage request exceeded its limit.")
        acknowledgement = queue.Queue(maxsize=1)
        try:
            self._outgoing.put((data, acknowledgement), timeout=self._remaining())
            error = acknowledgement.get(timeout=self._remaining())
        except (queue.Empty, queue.Full):
            raise UsageUnavailable("Native usage reader timed out.") from None
        if error is not None:
            raise error

    def receive(self) -> dict:
        try:
            message = self._incoming.get(timeout=self._remaining())
        except queue.Empty:
            raise UsageUnavailable("Native usage reader timed out.") from None
        if isinstance(message, UsageUnavailable):
            raise message
        try:
            event = json.loads(message)
        except (ValueError, UnicodeError, RecursionError):
            raise UsageUnavailable("Native usage output was not valid JSON.") from None
        if not isinstance(event, dict):
            raise UsageUnavailable("Native usage output was not a JSON object.")
        return event

    def __exit__(self, exc_type, exc, traceback):
        previous_int = None
        if self._handles_signals:
            # A second signal must not reenter bounded child cleanup.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            previous_int = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        cleanup_error = None
        try:
            # Workers own pipe closure. A detached descendant may keep a worker
            # blocked after our bounded join; closing its descriptor here could
            # recycle it for a later probe before that worker returns from I/O.
            self._stopping.set()
            if self.process is not None:
                from .runner import _stop

                try:
                    # A one-shot peer may exit immediately after its response.
                    # Reap it before signaling its potentially remaining group.
                    self.process.poll()
                    _stop(self.process, signal.SIGTERM, grouped=os.name != "nt")
                except PermissionError:
                    if sys.platform == "darwin":
                        try:
                            # A response can arrive just before the short-lived
                            # peer becomes reapable on macOS. Bound this grace
                            # period before retrying cleanup of its whole group.
                            self.process.wait(timeout=0.25)
                            _stop(self.process, signal.SIGTERM, grouped=True)
                        except (OSError, subprocess.TimeoutExpired):
                            cleanup_error = UsageUnavailable("Native usage reader cleanup failed.")
                    else:
                        cleanup_error = UsageUnavailable("Native usage reader cleanup failed.")
                except OSError:
                    cleanup_error = UsageUnavailable("Native usage reader cleanup failed.")
                try:
                    self._outgoing.put_nowait(None)
                except queue.Full:
                    pass
                for thread in self._threads:
                    thread.join(timeout=0.25)
                for index, stream in enumerate((self.process.stdout, self.process.stdin)):
                    if index >= len(self._threads) or not self._threads[index].is_alive():
                        self._close_stream(stream)
                self.process.poll()
        finally:
            if self._handles_signals:
                signal.signal(signal.SIGINT, previous_int)
                signal.signal(signal.SIGTERM, self._previous_term)
        if exc_type is None and cleanup_error is not None:
            raise cleanup_error
        return False
