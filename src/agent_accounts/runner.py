"""Stream native output and rotate only on a completed, confirmed quota failure."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import providers
from .store import AccountError, BusyError, Store


def exit_code(code: int) -> int:
    return code if code >= 0 else 128 - code


def _stop(process: subprocess.Popen, signum: int, *, grouped: bool) -> None:
    if process.poll() is not None:
        return
    try:
        if grouped and os.name != "nt":
            os.killpg(process.pid, signum)
        elif os.name == "nt":
            # Native launchers (including console-script .exe wrappers) may
            # own a child process. Killing only the launcher leaves that child
            # running with inherited pipes and potentially active tool calls.
            taskkill = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
            )
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            process.send_signal(signum)
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if grouped and os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
    except ProcessLookupError:
        process.wait()


def launch(argv: list[str], env: dict[str, str], *, detector=None, stdin=None) -> int:
    grouped = detector is not None and os.name != "nt"
    try:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE if detector else None,
            start_new_session=grouped,
        )
    except OSError as exc:
        raise AccountError(
            "Could not start the native CLI; check that it is installed and executable."
        ) from exc
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame):
        _stop(process, signal.SIGTERM, grouped=grouped)
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    try:
        if detector:
            assert process.stdout is not None
            oversized = False
            while chunk := process.stdout.readline(1024 * 1024):
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except OSError as exc:
                    # Windows' CRT reports a closed pipe as EINVAL instead of
                    # EPIPE. Normalize only stdout writes, so unrelated I/O
                    # failures still propagate as genuine filesystem errors.
                    if os.name == "nt" and exc.errno == errno.EINVAL:
                        raise BrokenPipeError from exc
                    raise
                if len(chunk) == 1024 * 1024 and not chunk.endswith(b"\n"):
                    oversized = True
                    detector.malformed = True
                elif oversized:
                    oversized = False
                else:
                    detector.feed(chunk)
        return exit_code(process.wait())
    except KeyboardInterrupt:
        _stop(process, signal.SIGINT, grouped=grouped)
        return 130
    except BrokenPipeError:
        _stop(process, signal.SIGTERM, grouped=grouped)
        # Avoid Python's shutdown flush replacing 141 with 120 on a closed pipe.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 141
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            _stop(process, signal.SIGTERM, grouped=grouped)


def _eligible(state: dict, provider: str, attempted: set[str], now: float) -> list[dict]:
    return [
        p
        for p in state["profiles"]
        if p["provider"] == provider and p["id"] not in attempted and p["cooldown_until"] <= now
    ]


def _available(store: Store, choices: list[dict]) -> list[dict]:
    available = []
    for candidate in choices:
        try:
            with store.profile_lock(candidate):
                available.append(candidate)
        except BusyError:
            continue
    return available


def run(
    store: Store,
    provider: str,
    args: list[str],
    *,
    account: str | None,
    rotate: bool | None,
    allow_replay: bool,
) -> int:
    with store.lock():
        state = store.read()
        rotate = state["settings"]["auto_rotate"] if rotate is None else rotate
        profile = Store.find(state, provider, account)
        if rotate and profile["cooldown_until"] > time.time():
            choices = _eligible(state, provider, set(), time.time())
            if not choices:
                raise AccountError(
                    "All accounts are cooling down. Wait, or use --no-auto-rotate to retry manually."
                )
            profile = choices[0]
        settings = dict(state["settings"])
    providers.validate_args(provider, args, rotate=rotate)
    if allow_replay and not rotate:
        raise AccountError("--allow-replay requires auto-rotation.")
    # Preflight before consuming redirected input or changing active selection.
    providers.executable(provider)
    providers.environment(provider, store.profile_home(profile))
    attempted: set[str] = set()
    last_code = 1
    with contextlib.ExitStack() as stack:
        source = None
        if rotate and not sys.stdin.isatty():
            # Disk backed, private, and automatically removed. Rewind for each attempt.
            source = stack.enter_context(tempfile.TemporaryFile())
            shutil.copyfileobj(sys.stdin.buffer, source)
        while True:
            home = store.profile_home(profile)
            with contextlib.ExitStack() as attempt:
                try:
                    attempt.enter_context(store.profile_lock(profile))
                except BusyError:
                    if not rotate:
                        raise
                    attempted.add(profile["id"])
                    with store.lock():
                        choices = _eligible(store.read(), provider, attempted, time.time())
                    if not choices:
                        print(
                            "agent-accounts: no available accounts; remaining accounts are busy or cooling down.",
                            file=sys.stderr,
                        )
                        return last_code
                    profile = choices[0]
                    continue
                providers.validate_home_config(provider, home)
                env = providers.environment(provider, home)
                argv = providers.command(provider, args)
                with store.lock():
                    state = store.read()
                    current = Store.find(state, provider, profile["name"])
                    if current["id"] != profile["id"]:
                        raise AccountError("Account changed while starting the process; retry.")
                    state["active"][provider] = profile["name"]
                    store.write(state)
                detector = providers.LimitDetector(provider) if rotate else None
                if source is not None:
                    source.seek(0)
                code = launch(argv, env, detector=detector, stdin=source)
                last_code = code
            if not detector or not detector.limited(code):
                return code
            attempted.add(profile["id"])
            now = time.time()
            reset = detector.reset_at
            # Ignore stale/implausible reset timestamps. Weekly plans may reset in 7 days.
            until = (
                reset
                if reset and now < reset <= now + 8 * 86400
                else now + settings["cooldown_seconds"]
            )
            with store.lock():
                state = store.read()
                for saved in state["profiles"]:
                    if saved["id"] == profile["id"]:
                        saved["cooldown_until"] = until
                choices = _available(store, _eligible(state, provider, attempted, now))
                if choices:
                    state["active"][provider] = choices[0]["name"]
                store.write(state)
            if not choices:
                print(
                    "agent-accounts: quota reached; no eligible accounts remain.", file=sys.stderr
                )
                return code
            profile = choices[0]
            print(
                f"agent-accounts: quota reached; selected {provider}/{profile['name']}.",
                file=sys.stderr,
            )
            if detector.work and not allow_replay:
                print(
                    "agent-accounts: partial work was observed. Review it before continuing; "
                    "--allow-replay permits repeating the full command.",
                    file=sys.stderr,
                )
                return code
