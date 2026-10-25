# agent-accounts

[![CI](https://github.com/hculap/agent-accounts/actions/workflows/ci.yml/badge.svg)](https://github.com/hculap/agent-accounts/actions/workflows/ci.yml)
[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Manage named **Codex** and **Claude Code** accounts from one CLI. Each account has its own provider directory, so switching accounts does not replace credentials underneath another session. Optional rotation skips accounts at a configurable usage threshold before a batch task starts, and selects another account when a supported batch command reports a quota limit.

`agent-accounts` and `aa` are the same executable. This is an independent community project, based on [codex-account](https://github.com/hculap/codex-account), and is not affiliated with OpenAI or Anthropic.

The short `aa` command may conflict with another program, including Apple Archive on macOS. Use `agent-accounts` instead anywhere below if that happens.

## Install

Requires Python 3.11+, Git for installation from this repository, and the native `codex` and/or `claude` executable on `PATH`. Install the provider CLIs separately.

On Windows, put the native `codex.exe` or `claude.exe` on `PATH`, or use WSL. Windows `.cmd` and `.bat` launchers, including npm shell shims, are rejected because they can interpret prompt text as shell commands. The Python package itself installs through the same commands on every platform.

With [uv](https://docs.astral.sh/uv/guides/tools/):

```sh
uv tool install git+https://github.com/hculap/agent-accounts.git
```

Or with [pipx](https://pipx.pypa.io/stable/):

```sh
pipx install git+https://github.com/hculap/agent-accounts.git
```

From a local checkout:

```sh
uv tool install .
```

The package has no runtime dependencies and does not edit shell startup files. It works from Bash, Zsh, Fish, PowerShell, and other shells that can launch an executable. If `aa` is not found after installation, follow your installer's instructions for putting its tool directory on `PATH`.

These commands install from GitHub. The package is not currently published to PyPI.

## Add and select accounts

```sh
aa login codex personal
aa login codex work
aa login claude personal
aa login claude work

aa list
aa use codex personal
aa use claude work
```

Login runs the provider's own authentication flow in a private, managed directory. Codex defaults to device authentication; supply native login options after `--` when needed:

```sh
aa login codex personal -- --device-auth
```

Use `aa run` to launch the selected account:

```sh
aa run codex --
aa run claude --
aa run codex --account work -- exec "Explain this project"
```

Arguments after `--` go directly to the native CLI without a shell. Your working directory stays the same. `aa use` changes which managed account future `aa run` calls select; plain `codex` and `claude` commands continue to use their existing configuration.

Provider settings, plugins, and session history are also separate for each account. Existing global settings are not copied automatically. An already running session keeps its account.

## Optional quota rotation

Rotation is **off by default**. Turn it on or off persistently:

```sh
aa settings set auto-rotate on
aa settings set auto-rotate off
aa settings
```

Both providers default to a **95% used** threshold. Configure each independently with an integer from 1 to 100:

```sh
aa settings set codex-rotation-threshold 90
aa settings set claude-rotation-threshold 95
```

When rotation is enabled, the wrapper checks the selected account before each batch attempt. Usage **at or above** its threshold skips that account without sending the task to it. It checks other eligible accounts of the same provider, once each. If threshold or backend-denial checks exhaust eligible accounts before any task starts, it exits with code `75`; it never silently exceeds a known threshold. Raising a threshold takes effect on the next invocation. Threshold skips do not create persistent quota cooldowns.

The percentage is the highest reported provider-wide window: Codex's primary/secondary `codex` quota windows, or Claude's five-hour/seven-day windows. Model-specific limits and usage-credit budgets are not used for this preflight decision. An explicit Codex backend denial also prevents launch, even when its percentage is low.

Usage is read through the native CLIs without sending a model prompt or reading credential values. Codex uses [App Server account/rateLimits/read](https://developers.openai.com/codex/app-server#6-rate-limits-chatgpt). Claude uses its experimental `get_usage` control request, which may change across CLI releases and may return native cached data. [Claude documents last-known usage when a fresh read fails](https://code.claude.com/docs/en/costs#when-the-usage-request-fails); expired Claude windows are ignored. An unavailable, unsupported, or invalid read prints a warning and retains quota-error rotation. It does not guess a percentage.

The threshold is checked **before a task**, not continuously during it. A long task or concurrent usage elsewhere can cross the threshold after it starts. Existing conversations are never interrupted or transferred by this check. Rotation remains off by default, including after upgrading an older installation.

Or select it for one invocation:

```sh
aa run codex --auto-rotate -- exec --json "Explain this project"
aa run claude --auto-rotate -- -p --output-format stream-json --verbose "Explain this project"
```

Automatic rotation requires these structured batch modes. It recognizes provider quota-error events, skips busy or cooling accounts, and tries each eligible account at most once. It does not rotate on ordinary tool output, authentication errors, generic failures, or cancellation. A successful fallback becomes the selected account. Cooldowns are local scheduling hints, not a live view of subscription usage:

```sh
aa settings set cooldown-seconds 3600
```

A busy account explicitly selected with `--account` fails without launching another account. Busy fallback accounts are skipped after a threshold skip or confirmed quota failure.

By default, a task is retried only before assistant or tool work has begun. If a quota error arrives after work has started, the next account is selected, but the task exits without replaying earlier actions. Inspect the existing changes and start a suitable continuation yourself.

For tasks where repeating earlier work is acceptable, explicitly allow replay:

```sh
aa run codex --auto-rotate --allow-replay -- exec --json "Summarize the README"
```

Replay starts the command again under another account; it does not transfer the conversation. It can repeat tool calls and other side effects. Native structured output is streamed from every attempt, so consumers must handle multiple sessions and failed attempts in one output stream. Wrapper notices go to stderr.

Interactive sessions retain their terminal behavior and do not support automatic switching during a conversation. If rotation is enabled in settings, disable it for an interactive launch:

```sh
aa run codex --no-auto-rotate --
aa run claude --no-auto-rotate --
```

## Import an existing login

For a file-based Codex ChatGPT subscription login (API-key logins are not supported):

```sh
aa import codex personal
```

This reads `auth.json` from `CODEX_HOME`, or `~/.codex` when `CODEX_HOME` is unset, and copies only authentication into a new managed account. It does not copy configuration, sessions, skills, or plugins. Keyring-backed Codex logins should be added with `aa login` instead.

Import a saved account from the original `codex-account` plugin explicitly:

```sh
aa import codex work --from /path/to/.codex/accounts/work.json
```

Source files remain untouched. Imported credentials share the original token family: concurrent use or logging out through the original CLI can invalidate the copy. Separate `aa login` flows are preferable when both setups will remain in use.

Claude file-based credentials can be imported on Linux or Windows:

```sh
aa import claude work --from /path/to/.claude/.credentials.json
```

On macOS, use `aa login claude work`. Claude stores credentials in Keychain with a service identity derived from its configuration directory; this tool does not copy Keychain entries. See the [Claude authentication documentation](https://code.claude.com/docs/en/authentication).

## Commands

| Command | Purpose |
| --- | --- |
| `aa list [PROVIDER] [--json]` | Show registered accounts and the current selection. |
| `aa login PROVIDER NAME [-- ARGS]` | Add an account through native authentication. |
| `aa import PROVIDER NAME [--from PATH]` | Copy supported credentials into a managed account. |
| `aa use PROVIDER NAME` | Select an account for future runs. |
| `aa run PROVIDER [OPTIONS] -- ARGS` | Launch the provider with the selected account. |
| `aa remove PROVIDER NAME` | Unregister an account while retaining its directory. |
| `aa settings [--json]` | Show rotation settings. |
| `aa settings set KEY VALUE` | Change a supported setting. |
| `aa doctor [--json]` | Inspect local configuration and provider availability. |
| `aa --help` | Show all options. |

`PROVIDER` is `codex` or `claude`. Account names start with a letter or digit, contain at most 64 letters, digits, dots, dashes, or underscores, and cannot end in a dot. Windows reserved filenames are rejected on every platform. Use `aa COMMAND --help` for details.

## Storage and safety

State lives under `$XDG_DATA_HOME/agent-accounts` on Unix, defaulting to `~/.local/share/agent-accounts`. On Windows it lives under `%LOCALAPPDATA%\agent-accounts`. Override it with `AGENT_ACCOUNTS_HOME` or the global `--home PATH` option:

```sh
aa --home /private/path/agent-accounts list
```

`state.json` holds account metadata and settings. Managed provider homes live under `profiles/PROVIDER/ID`. The wrapper passes their paths through `CODEX_HOME` or `CLAUDE_CONFIG_DIR`. Managed Codex accounts use file-based credentials; Claude uses its native credential store. The storage directory is private on Unix (`0700`), and files written by this tool use `0600`. On Windows, protect the directory with your user account's filesystem permissions.

Conflicting authentication or routing variables, such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN`, are rejected instead of silently overriding the selected account. Unset the named variable before retrying. Managed-home authentication overrides and arguments that select an external provider or remote session are also rejected; native project and administrator policy still applies.

Do not commit or share your storage directory. Keep it outside source repositories, shared folders, and public backups. `aa remove` preserves credentials and session data so removing a registration cannot erase work; it does not revoke a provider login. See [SECURITY.md](SECURITY.md).

The provider CLIs remain responsible for login, token refresh, and subscription rules. Consult [Codex authentication](https://developers.openai.com/codex/auth/) and [Claude's configuration-directory setting](https://code.claude.com/docs/en/env-vars). Changes in their output formats may require a detector update. Automated tests use synthetic accounts and fake provider processes; they do not establish successful live login or quota behavior for every provider release.

## Upgrade and uninstall

```sh
uv tool upgrade agent-accounts
uv tool uninstall agent-accounts
```

Or, when installed with pipx:

```sh
pipx upgrade agent-accounts
pipx uninstall agent-accounts
```

Uninstalling the package leaves managed account directories and native CLI installations intact. Remove stored credentials separately only when you no longer need their logins or session history.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for isolated tests, linting, and package checks. The project is released under the [MIT license](LICENSE), retaining the original `codex-account` copyright.
