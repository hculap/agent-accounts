"""Native provider boundaries, without private API calls or token refresh logic."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

from .store import AccountError, atomic_write, private_dir, read_object

AUTH_ENV = {
    "codex": (
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_BASE_URL",
        "CODEX_CHATGPT_BASE_URL",
        "CODEX_REMOTE",
        "CODEX_REMOTE_AUTH_TOKEN",
    ),
    "claude": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    ),
}


def environment(provider: str, home: Path) -> dict[str, str]:
    conflicts = [name for name in AUTH_ENV[provider] if os.environ.get(name)]
    if conflicts:
        raise AccountError(
            "Unset conflicting authentication/routing variables for managed accounts: "
            + ", ".join(conflicts)
        )
    env = dict(os.environ)
    env["CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"] = str(home)
    return env


def executable(provider: str) -> str:
    binary = (shutil.which(provider + ".exe") if os.name == "nt" else None) or shutil.which(
        provider
    )
    if binary is None:
        raise AccountError(
            f"{provider} is not installed or is not on PATH. Install the native CLI first."
        )
    if os.name == "nt" and not binary.lower().endswith(".exe"):
        raise AccountError(
            f"{provider} resolves to a Windows shell shim. Put the native {provider}.exe "
            "on PATH, or run this tool inside WSL; .cmd/.bat launchers are not supported."
        )
    return binary


def credential_file(provider: str, home: Path) -> Path:
    return home / ("auth.json" if provider == "codex" else ".credentials.json")


def validate_credentials(provider: str, path: Path) -> bytes:
    data = read_object(path, credential=True)
    if provider == "codex" and (
        data.get("OPENAI_API_KEY") or data.get("auth_mode") not in (None, "chatgpt")
    ):
        raise AccountError(
            "Expected a ChatGPT subscription login, not API-key or external-token credentials."
        )
    tokens = data.get("tokens") if provider == "codex" else data.get("claudeAiOauth")
    key = "access_token" if provider == "codex" else "accessToken"
    if (
        not isinstance(tokens, dict)
        or not isinstance(tokens.get(key), str)
        or not tokens[key].strip()
    ):
        raise AccountError(
            "Expected subscription login credentials. Use the native login command through this tool."
        )
    if provider == "codex" and (
        not isinstance(tokens.get("account_id"), str) or not tokens["account_id"]
    ):
        raise AccountError("Codex credentials have no account identity; log in again.")
    # Serialize the exact validated object, avoiding a second read racing a refresh.
    import json

    return (json.dumps(data, indent=2) + "\n").encode()


def prepare_home(provider: str, home: Path) -> None:
    private_dir(home)
    if provider == "codex":
        atomic_write(home / "config.toml", b'cli_auth_credentials_store = "file"\n')


def import_source(provider: str, source: Path | None) -> Path:
    if provider == "claude" and sys.platform == "darwin":
        raise AccountError(
            "Claude Code uses a directory-specific macOS Keychain entry. Use 'login claude NAME'."
        )
    if source is not None:
        return source.expanduser()
    if provider == "claude":
        raise AccountError(
            "Claude file import requires --from PATH; use 'login claude NAME' for native authentication."
        )
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"


def command(provider: str, args: list[str]) -> list[str]:
    prefix = [executable(provider)]
    if provider == "codex":
        prefix += ["-c", 'cli_auth_credentials_store="file"']
    return prefix + args


def _parse_args(provider: str, args: list[str]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Separate native flags, their values, and command words in one pass.

    Unknown flags are left to the native parser; recognized value options must
    be skipped so e.g. --model NAME logout is still rejected as a command.
    """
    values = (
        {
            "-c",
            "--config",
            "-m",
            "--model",
            "-s",
            "--sandbox",
            "-a",
            "--ask-for-approval",
            "-C",
            "--cd",
            "-i",
            "--image",
            "--add-dir",
            "--enable",
            "--disable",
            "--color",
            "--output-schema",
            "-o",
            "--output-last-message",
            "--thread-source",
            "--cyber-access-program",
            "-p",
            "--profile",
        }
        if provider == "codex"
        else {
            "--model",
            "--fallback-model",
            "--agent",
            "--agents",
            "--append-system-prompt",
            "--system-prompt",
            "--system-prompt-file",
            "--append-system-prompt-file",
            "--output-format",
            "--input-format",
            "--permission-mode",
            "--permission-prompts",
            "--mcp-config",
            "--allowedTools",
            "--allowed-tools",
            "--disallowedTools",
            "--disallowed-tools",
            "--tools",
            "--add-dir",
            "--max-turns",
            "--max-budget-usd",
            "--json-schema",
            "--plugin-dir",
            "--plugin-url",
            "--session-id",
            "-n",
            "--name",
            "--effort",
            "--autocompact",
            "--debug-file",
            "--permission-prompt-tool",
        }
    )
    optional = (
        {"-d", "--debug", "-r", "--resume", "-w", "--worktree", "--prompt-suggestions"}
        if provider == "claude"
        else set()
    )
    options: list[tuple[str, str | None]] = []
    words = []
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            # Literal prompt data cannot change command routing or run mode.
            break
        if not arg.startswith("-") or arg == "-":
            words.append(arg)
            continue
        if arg.startswith("--"):
            key, separator, value = arg.partition("=")
            if separator:
                options.append((key, value))
                continue
            keys = [key]
        else:
            keys = ["-" + letter for letter in arg[1:]]
        for offset, key in enumerate(keys):
            value = None
            if key in values or key in optional:
                attached = arg[offset + 2 :] if not arg.startswith("--") else ""
                if attached:
                    # Native parsers accept both -cKEY=VALUE and -c=KEY=VALUE.
                    # Only this attached separator is syntax; a separate
                    # value beginning with '=' remains literal data.
                    value = attached.removeprefix("=")
                elif index < len(args) and (key in values or not args[index].startswith("-")):
                    value = args[index]
                    index += 1
                options.append((key, value))
                break
            options.append((key, None))
    return options, words


def positionals(provider: str, args: list[str]) -> list[str]:
    return _parse_args(provider, args)[1]


def validate_args(provider: str, args: list[str], *, rotate: bool) -> None:
    # A managed profile must not route to another account or a remote daemon.
    options, words = _parse_args(provider, args)
    flags = {key for key, _ in options}
    blocked = (
        {"--oss", "--local-provider", "--remote", "--remote-auth-token-env", "-p", "--profile"}
        if provider == "codex"
        else {
            "--settings",
            "--bare",
            "--cloud",
            "--remote",
            "--remote-control",
            "--bg",
            "--background",
            "--settings-sources",
            "--setting-sources",
            "--desktop",
            "--environment",
        }
    )
    for flag in flags:
        if flag in blocked:
            raise AccountError(f"{flag} conflicts with managed account isolation.")
    if provider == "codex":
        for flag, config in options:
            if flag in ("-c", "--config") and config is not None:
                key = config.split("=", 1)[0].strip().strip("\"'")
                if any(
                    part in key for part in ("auth", "provider", "base_url", "api_key", "home")
                ) or key.split(".")[0] in {"access_token", "refresh_token", "id_token"}:
                    raise AccountError(
                        "Authentication/provider config overrides conflict with managed accounts."
                    )
        if words[:1] and words[0] in {
            "login",
            "logout",
            "app",
            "app-server",
            "remote-control",
            "cloud",
            "exec-server",
            "agents",
            "queue",
        }:
            raise AccountError(
                "Use the dedicated login command; managed run supports local agent sessions."
            )
    elif any(word in {"auth", "setup-token", "gateway", "attach", "respawn"} for word in words):
        # Extra bare words cannot form a Claude prompt argument. Inspect all
        # of them so a new native option with a value cannot hide an auth command.
        raise AccountError(
            "Use the dedicated login command; managed run supports local agent sessions."
        )
    if not rotate:
        return
    if "--" in args:
        raise AccountError(
            "Rotation does not accept a nested -- separator; pass the prompt as a normal argument."
        )
    if provider == "codex":
        if not words or words[0] not in ("exec", "e") or "--json" not in flags:
            raise AccountError(
                "Auto-rotation requires 'codex -- exec --json ...'; use --no-auto-rotate for interactive sessions."
            )
    else:
        formats = [value for flag, value in options if flag == "--output-format"]
        if not flags & {"-p", "--print"} or formats != ["stream-json"] or "--verbose" not in flags:
            raise AccountError(
                "Auto-rotation requires '-p --output-format stream-json --verbose'; "
                "use --no-auto-rotate for interactive sessions."
            )
        if flags & {"--input-format", "--resume", "--continue", "-r", "-c"}:
            raise AccountError(
                "Rotation supports a fresh text prompt, not streaming input or cross-account resume."
            )
    if provider == "codex" and len(words) > 1 and words[1] in ("resume", "fork"):
        raise AccountError("Rotation cannot resume a session stored in another account's home.")


def is_batch(provider: str, args: list[str]) -> bool:
    options, words = _parse_args(provider, args)
    if provider == "codex":
        return words[:1] in (["exec"], ["e"], ["review"])
    return any(key in ("-p", "--print") for key, _ in options)


def validate_home_config(provider: str, home: Path) -> None:
    """Fail closed on user-home auth routing. Project/admin policy still belongs to the CLI."""
    if provider == "codex":
        path = home / "config.toml"
        if not path.exists():
            return
        try:
            from .store import reject_symlinks

            reject_symlinks(path)
            data = tomllib.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise AccountError("Invalid managed Codex configuration.") from exc
        if (
            data.get("model_provider", "openai") != "openai"
            or data.get("model_providers")
            or data.get("openai_base_url")
            or data.get("chatgpt_base_url")
        ):
            raise AccountError(
                "Custom providers in the managed Codex home conflict with account selection."
            )
    else:
        path = home / "settings.json"
        if not path.exists():
            return
        data = read_object(path)
        env = data.get("env", {})
        if not isinstance(env, dict):
            raise AccountError("Invalid Claude settings env object.")
        if data.get("apiKeyHelper") or any(
            env.get(k) for k in (*AUTH_ENV[provider], "CLAUDE_CONFIG_DIR")
        ):
            raise AccountError(
                "Authentication/routing overrides in Claude settings conflict with managed accounts."
            )


class LimitDetector:
    """Inspect provider protocol envelopes, never model/tool-generated text."""

    def __init__(self, provider: str):
        self.provider = provider
        self.quota = False
        self.failed = False
        self.succeeded = False
        self.work = False
        self.malformed = False
        self.reset_at: float | None = None

    def feed(self, line: bytes) -> None:
        import json
        import math

        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            if line.strip():
                self.malformed = True
            return
        if not isinstance(event, dict):
            self.malformed = True
            return
        kind = event.get("type")
        if self.provider == "codex":
            if kind == "turn.completed":
                self.succeeded = True
                self.quota = False
                self.failed = False
            elif kind in ("turn.failed", "error"):
                error = event.get("error", event)
                if not isinstance(error, dict):
                    error = {}
                message = error.get("message", "")
                code = error.get("code", error.get("codexErrorInfo"))
                limited = (
                    code in ("usage_limit_reached", "usage_limit_exceeded", "UsageLimitExceeded")
                    or isinstance(message, str)
                    and bool(
                        re.match(r"^You[’']ve hit your usage limit(?:\.| for [^\r\n]+\.)", message)
                    )
                )
                if kind == "turn.failed":
                    self.failed = True
                    self.succeeded = False
                    self.quota = limited
                elif limited:
                    self.quota = True
            elif kind in ("item.started", "item.updated", "item.completed"):
                # Even reasoning or unknown future item types are conservatively work.
                self.work = True
        else:
            if kind == "rate_limit_event":
                info = event.get("rate_limit_info", {})
                if isinstance(info, dict) and info.get("status") in ("allowed", "allowed_warning"):
                    self.quota = False
                    self.reset_at = None
                if isinstance(info, dict) and info.get("status") == "rejected":
                    self.quota = True
                    reset = info.get("resetsAt")
                    if (
                        type(reset) in (int, float)
                        and 0 < reset <= 253402300799
                        and math.isfinite(reset)
                    ):
                        self.reset_at = float(reset)
            elif kind == "assistant":
                if event.get("error") == "rate_limit":
                    self.quota = True
                elif event.get("error"):
                    self.quota = False
                else:
                    self.work = True
            elif kind == "result":
                self.succeeded = (
                    event.get("is_error") is False and event.get("subtype") == "success"
                )
                self.failed = (
                    event.get("is_error") is True
                    and event.get("subtype") == "error_during_execution"
                )
                errors = event.get("errors", [])
                # A rejected rate snapshot must not reclassify a later unrelated
                # terminal failure. Native error text is only corroboration of
                # a typed quota signal, never sufficient on its own.
                if errors and (
                    not isinstance(errors, list)
                    or any(
                        not isinstance(error, str)
                        or not re.search(
                            r"(?:usage limit|rate.?limit|hit your limit|limit reached)",
                            error,
                            re.IGNORECASE,
                        )
                        for error in errors
                    )
                ):
                    self.quota = False
                if self.succeeded or not self.failed:
                    self.quota = False
            elif kind in ("stream_event", "tool_use", "tool_result"):
                self.work = True

    def limited(self, returncode: int) -> bool:
        return (
            0 < returncode < 128
            and self.quota
            and self.failed
            and not self.succeeded
            and not self.malformed
        )
