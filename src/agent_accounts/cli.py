"""Shell-independent command line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, providers
from .runner import launch, run
from .store import PROVIDERS, AccountError, Store, atomic_write


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="agent-accounts", description="Named, isolated Codex and Claude Code accounts."
    )
    root.add_argument("--version", action="version", version=f"agent-accounts {__version__}")
    root.add_argument("--home", type=Path, help="Private store (or AGENT_ACCOUNTS_HOME).")
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List accounts without exposing credentials.")
    listing.add_argument("provider", choices=PROVIDERS, nargs="?")
    listing.add_argument("--json", action="store_true")
    for name in ("import", "login", "use", "remove"):
        sub = commands.add_parser(name)
        sub.add_argument("provider", choices=PROVIDERS)
        sub.add_argument("name")
        if name == "import":
            sub.add_argument("--from", dest="source", type=Path)
    running = commands.add_parser("run", help="Run a native CLI with the selected account.")
    running.add_argument("provider", choices=PROVIDERS)
    running.add_argument("--account")
    rotation = running.add_mutually_exclusive_group()
    rotation.add_argument("--auto-rotate", dest="rotate", action="store_true")
    rotation.add_argument("--no-auto-rotate", dest="rotate", action="store_false")
    running.set_defaults(rotate=None)
    running.add_argument(
        "--allow-replay",
        action="store_true",
        help="Allow retrying the full command even after partial work.",
    )
    settings = commands.add_parser("settings", help="View settings or set optional rotation.")
    settings.add_argument("--json", action="store_true")
    settings_sub = settings.add_subparsers(dest="settings_command")
    setting = settings_sub.add_parser("set")
    setting.add_argument("key", choices=("auto-rotate", "cooldown-seconds"))
    setting.add_argument("value")
    doctor = commands.add_parser(
        "doctor", help="Check prerequisites without network calls or login."
    )
    doctor.add_argument("--json", action="store_true")
    return root


def add_account(
    store: Store, provider: str, name: str, source: Path | None, login_args: list[str] | None
) -> int:
    with store.lock():
        state = store.read()
        profile = store.create_profile(state, provider, name)
        home = store.profile_home(profile)
        if login_args is None:
            data = providers.validate_credentials(
                provider, providers.import_source(provider, source)
            )
            providers.prepare_home(provider, home)
            atomic_write(providers.credential_file(provider, home), data)
        else:
            env = providers.environment(provider, home)
            providers.executable(provider)
            # Stable path: Claude's macOS Keychain namespace depends on it.
            providers.prepare_home(provider, home)
            args = (
                ["login", *(login_args or ["--device-auth"])]
                if provider == "codex"
                else ["auth", "login", *login_args]
            )
            code = launch(providers.command(provider, args), env)
            if code:
                print(
                    f"agent-accounts: login failed; previous selection preserved. "
                    f"Unregistered profile retained at {home}.",
                    file=sys.stderr,
                )
                return code
            if provider == "claude" and sys.platform == "darwin":
                try:
                    result = subprocess.run(
                        providers.command(provider, ["auth", "status"]),
                        env=env,
                        capture_output=True,
                        timeout=30,
                    )
                    status = json.loads(result.stdout)
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    raise AccountError(
                        "Could not verify Claude login; previous selection preserved."
                    ) from exc
                if (
                    result.returncode
                    or not isinstance(status, dict)
                    or status.get("loggedIn") is not True
                    or status.get("authMethod") != "claude.ai"
                    or status.get("apiProvider", "firstParty") != "firstParty"
                    or ("configDirectory" in status and status["configDirectory"] != str(home))
                ):
                    raise AccountError(
                        "Claude did not confirm login; previous selection preserved."
                    )
            else:
                providers.validate_credentials(provider, providers.credential_file(provider, home))
            cred = providers.credential_file(provider, home)
            if cred.exists() and os.name != "nt":
                from .store import reject_symlinks

                reject_symlinks(cred)
                cred.chmod(0o600)
        state["profiles"].append(profile)
        state["active"][provider] = name
        store.write(state)
    print(f"Saved {provider}/{name} and selected it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    native_args = []
    if "--" in argv:
        index = argv.index("--")
        argv, native_args = argv[:index], argv[index + 1 :]
    cli = parser()
    options = cli.parse_args(argv)
    if native_args and options.command not in ("run", "login"):
        cli.error("Native arguments after -- are supported only for run and login.")
    store = Store(options.home)
    try:
        if options.command == "list":
            profiles = store.list_profiles(options.provider)
            if options.json:
                print(json.dumps(profiles))
            elif not profiles:
                print("No accounts yet. Use 'login PROVIDER NAME' or 'import codex NAME'.")
            else:
                for profile in profiles:
                    mark = "*" if profile["active"] else " "
                    cooldown = (
                        f" (cooldown until {int(profile['cooldown_until'])})"
                        if profile["cooldown_until"]
                        else ""
                    )
                    print(f"{mark} {profile['provider']}/{profile['name']}{cooldown}")
        elif options.command in ("import", "login"):
            return add_account(
                store,
                options.provider,
                options.name,
                getattr(options, "source", None),
                native_args if options.command == "login" else None,
            )
        elif options.command == "use":
            with store.lock():
                state = store.read()
                Store.find(state, options.provider, options.name)
                state["active"][options.provider] = options.name
                store.write(state)
            print(f"Selected {options.provider}/{options.name} for future managed runs.")
        elif options.command == "remove":
            with store.lock():
                state = store.read()
                profile = Store.find(state, options.provider, options.name)
                state["profiles"].remove(profile)
                if state["active"].get(options.provider) == options.name:
                    state["active"].pop(options.provider)
                store.write(state)
            print(
                f"Removed {options.provider}/{options.name}. Credentials and session files remain at "
                f"{store.profile_home(profile)}."
            )
        elif options.command == "run":
            return run(
                store,
                options.provider,
                native_args,
                account=options.account,
                rotate=options.rotate,
                allow_replay=options.allow_replay,
            )
        elif options.command == "settings":
            if options.settings_command == "set":
                value = options.value
                if options.key == "auto-rotate":
                    if value not in ("on", "off"):
                        raise AccountError("auto-rotate must be on or off.")
                    value = value == "on"
                else:
                    try:
                        value = int(value)
                    except ValueError as exc:
                        raise AccountError(
                            "cooldown-seconds must be an integer from 1 to 604800."
                        ) from exc
                    if not 1 <= value <= 604800:
                        raise AccountError("cooldown-seconds must be from 1 to 604800.")
                with store.lock():
                    state = store.read()
                    state["settings"][options.key.replace("-", "_")] = value
                    store.write(state)
            settings = store.read()["settings"]
            if options.json:
                print(json.dumps(settings))
            else:
                print(f"auto-rotate: {'on' if settings['auto_rotate'] else 'off'}")
                print(f"cooldown-seconds: {settings['cooldown_seconds']}")
        elif options.command == "doctor":
            store.read()  # Include validation, never initialize a store or contact providers.
            report = {
                "version": __version__,
                "home": str(store.home),
                "providers": {
                    p: {
                        "installed": shutil.which(p) is not None,
                        "conflicting_environment": [
                            n for n in providers.AUTH_ENV[p] if os.environ.get(n)
                        ],
                    }
                    for p in PROVIDERS
                },
            }
            if options.json:
                print(json.dumps(report))
            else:
                print(f"agent-accounts {__version__}\nStore: {store.home}")
                for name, item in report["providers"].items():
                    print(f"{name}: {'installed' if item['installed'] else 'not found on PATH'}")
                    if item["conflicting_environment"]:
                        print(
                            "  conflicting variables: " + ", ".join(item["conflicting_environment"])
                        )
        return 0
    except AccountError as exc:
        print(f"agent-accounts: {exc}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        # Native OS exception text can contain paths controlled by credentials/config.
        print(
            "agent-accounts: filesystem operation failed; check paths, permissions, and available space.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130
