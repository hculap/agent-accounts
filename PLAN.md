# Implementation plan and design decisions

## Analysis

The predecessor, [codex-account](https://github.com/hculap/codex-account), stores
Codex auth snapshots and installs shell functions. Its baseline scenario passes
under both Bash and Zsh. Extending live-file swapping to Claude would introduce
Keychain, refresh-token, concurrent-process, and interrupted-login problems.

This project uses a Python 3.11+ package with no runtime dependencies. Native CLIs
own authentication and refresh. Stable managed homes separate credentials,
configuration, plugins, and history; selection never overwrites the original home.
The new repository retains the original MIT attribution.

## Delivery steps

1. Verify native authentication and quota signaling with official documentation
   and installed CLI help; independently review the predecessor.
2. Specify subprocess acceptance tests using synthetic profiles and fake native
   executables. Observe missing-command failures before implementation.
3. Implement private atomic storage, native login, explicit import, selection,
   removal that retains data, settings, diagnostics, and process forwarding.
4. Implement optional bounded rotation: structured batch output only, confirmed
   terminal quota failures, persisted cooldowns, provider-local selection, and
   no replay after partial work unless explicitly enabled.
5. Independently test and review failure paths; fix verified defects and repeat
   the affected checks. Validate source distributions, wheels, and uv/pipx installs.
6. Inspect the exact publication tree for credentials; create a separate public
   GitHub repository, push the tested commit, and verify remote identity and CI.

## Decisions and limits

- Interactive runs preserve native terminal behavior; automatic in-session
  credential replacement is unsupported. Batch runs support automatic rotation.
- Rotation is off by default. Structured output is forwarded unchanged, so a
  retry produces more than one attempt's events on stdout. Notices use stderr.
- Tool or assistant activity prevents automatic replay; selection can still
  advance. `--allow-replay` explicitly permits repeating the full task.
- No unofficial usage endpoints, background polling, credential refresh, or
  live paid prompt is needed to manage accounts.
- macOS Claude accounts use native login and directory-specific Keychain entries.
  File import is supported on Linux/Windows only. Codex supports explicit import
  of the predecessor's snapshots without modifying source files.
- Claims about Windows/Linux behavior require their CI jobs. Fake-provider tests
  verify orchestration; they do not establish live quota/account acceptance.
