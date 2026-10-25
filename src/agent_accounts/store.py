"""Private, atomic metadata storage. Provider credentials never enter state.json."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import uuid
from pathlib import Path

PROVIDERS = ("codex", "claude")
DEFAULT_SETTINGS = {
    "auto_rotate": False,
    "cooldown_seconds": 300,
    "codex_rotation_threshold": 95,
    "claude_rotation_threshold": 95,
}


class AccountError(Exception):
    """An actionable error safe to display without credential values."""


class BusyError(AccountError):
    """An account or metadata lock is held by another operation."""


def valid_name(name: str) -> str:
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)
        or name.endswith(".")
        or name.split(".")[0].upper() in reserved
    ):
        raise AccountError(
            "Account names must start with a letter or digit, use only "
            "letters, digits, dots, dashes or underscores, and be at most 64 characters."
        )
    return name


def reject_symlinks(path: Path) -> None:
    # Check all existing components, including a nonexistent leaf's ancestors.
    for part in (path, *path.parents):
        if part.is_symlink():
            if (
                sys.platform == "darwin"
                and part in (Path("/tmp"), Path("/var"))
                and part.resolve() == Path("/private") / part.name
            ):
                continue
            raise AccountError("Refusing a symlink in a managed storage path.")


def private_dir(path: Path) -> None:
    reject_symlinks(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise AccountError("Managed storage path is not a directory.")
    if os.name != "nt":
        path.chmod(0o700)


def atomic_write(path: Path, content: bytes) -> None:
    reject_symlinks(path)
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_object(path: Path, *, credential: bool = False) -> dict:
    reject_symlinks(path)
    try:
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 8 * 1024 * 1024:
            raise AccountError("Expected a regular JSON file smaller than 8 MiB.")
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        label = "Credential file" if credential else "State file"
        raise AccountError(f"{label} is missing or invalid JSON; no changes made.") from exc
    if not isinstance(value, dict):
        raise AccountError("Expected a JSON object; no changes made.")
    return value


def default_home() -> Path:
    if os.environ.get("AGENT_ACCOUNTS_HOME"):
        return Path(os.environ["AGENT_ACCOUNTS_HOME"]).expanduser()
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "agent-accounts"


@contextlib.contextmanager
def file_lock(path: Path):
    """Nonblocking OS locks release even after a crash; never unlink lock files."""
    private_dir(path.parent)
    reject_symlinks(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BusyError(
                    "Account store is busy; retry when the other operation finishes."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BusyError(
                    "Account store is busy; retry when the other operation finishes."
                ) from exc
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, home: Path | None = None):
        # abspath preserves symlinks so validation can reject them. macOS /tmp is
        # a platform symlink: resolve only the system temp alias before passing
        # a test/custom home; user supplied managed symlinks remain prohibited.
        self.home = Path(os.path.abspath((home or default_home()).expanduser()))
        # macOS's standard /var and /tmp aliases are not user-controlled storage.
        if os.name != "nt" and str(self.home).startswith(("/tmp/", "/var/")):
            prefix = Path("/tmp" if str(self.home).startswith("/tmp/") else "/var")
            self.home = prefix.resolve() / self.home.relative_to(prefix)

    def lock(self):
        return file_lock(self.home / ".lock")

    def profile_lock(self, profile: dict):
        return file_lock(self.profile_home(profile) / ".agent-accounts.lock")

    def registration_lock(self, provider: str, name: str):
        valid_name(name)
        if provider not in PROVIDERS:
            raise AccountError("Unknown provider.")
        private_dir(self.home)
        return file_lock(self.home / ".registrations" / provider / f"{name.casefold()}.lock")

    def read(self) -> dict:
        path = self.home / "state.json"
        reject_symlinks(path)
        if not path.exists():
            return {"version": 1, "settings": dict(DEFAULT_SETTINGS), "active": {}, "profiles": []}
        state = read_object(path)
        self.validate(state)
        # Version 1 stores predate per-provider thresholds. Supply only missing
        # new settings in memory; reads must not rewrite private account state.
        for provider in PROVIDERS:
            key = f"{provider}_rotation_threshold"
            state["settings"].setdefault(key, DEFAULT_SETTINGS[key])
        return state

    def validate(self, state: dict) -> None:
        try:
            if (
                type(state["version"]) is not int
                or state["version"] != 1
                or not isinstance(state["settings"], dict)
                or not isinstance(state["profiles"], list)
                or not isinstance(state["active"], dict)
            ):
                raise ValueError
            settings = state["settings"]
            if (
                type(settings["auto_rotate"]) is not bool
                or type(settings["cooldown_seconds"]) is not int
                or not 1 <= settings["cooldown_seconds"] <= 604800
            ):
                raise ValueError
            for provider in PROVIDERS:
                key = f"{provider}_rotation_threshold"
                threshold = settings.get(key, DEFAULT_SETTINGS[key])
                if type(threshold) is not int or not 1 <= threshold <= 100:
                    raise ValueError
            seen = set()
            ids = set()
            for p in state["profiles"]:
                if p["provider"] not in PROVIDERS:
                    raise ValueError
                valid_name(p["name"])
                if not re.fullmatch(r"[0-9a-f]{32}", p["id"]):
                    raise ValueError
                key = (p["provider"], p["name"].casefold())
                if key in seen or p["id"] in ids:
                    raise ValueError
                seen.add(key)
                ids.add(p["id"])
                until = p["cooldown_until"]
                if type(until) not in (float, int) or not math.isfinite(until) or until < 0:
                    raise ValueError
            for provider, name in state["active"].items():
                if not any(
                    p["provider"] == provider and p["name"] == name for p in state["profiles"]
                ):
                    raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError, AccountError) as exc:
            raise AccountError(
                "Invalid account state; restore a backup before continuing."
            ) from exc

    def write(self, state: dict) -> None:
        self.validate(state)
        atomic_write(self.home / "state.json", (json.dumps(state, indent=2) + "\n").encode())

    def profile_home(self, profile: dict) -> Path:
        if profile.get("provider") not in PROVIDERS or not re.fullmatch(
            r"[0-9a-f]{32}", profile.get("id", "")
        ):
            raise AccountError("Invalid managed profile path.")
        path = self.home / "profiles" / profile["provider"] / profile["id"]
        reject_symlinks(path)
        return path

    def create_profile(self, state: dict, provider: str, name: str) -> dict:
        valid_name(name)
        if provider not in PROVIDERS:
            raise AccountError("Unknown provider.")
        if any(
            p["provider"] == provider and p["name"].casefold() == name.casefold()
            for p in state["profiles"]
        ):
            raise AccountError(f"Account '{name}' already exists for {provider}.")
        return {"provider": provider, "name": name, "id": uuid.uuid4().hex, "cooldown_until": 0}

    @staticmethod
    def find(state: dict, provider: str, name: str | None = None) -> dict:
        name = name or state["active"].get(provider)
        for p in state["profiles"]:
            if p["provider"] == provider and p["name"] == name:
                return p
        raise AccountError(
            f"No saved {provider} account selected; use login, import, or use first."
        )

    def list_profiles(self, provider: str | None = None) -> list[dict]:
        state = self.read()
        return [
            {
                "provider": p["provider"],
                "name": p["name"],
                "active": state["active"].get(p["provider"]) == p["name"],
                "home": str(self.profile_home(p)),
                "cooldown_until": p["cooldown_until"],
            }
            for p in state["profiles"]
            if provider is None or p["provider"] == provider
        ]
