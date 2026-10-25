# Security

This tool manages local account selection and launches the official provider CLIs. It is not a credential vault: file-based provider logins contain reusable secrets, and native credential stores retain their own security properties.

## Credential handling

- Keep the managed storage directory outside repositories and shared directories.
- Unix directories created by the tool are private (`0700`); files written by it use `0600`. Provider-created files follow the provider's behavior. Windows directory access depends on filesystem ACLs.
- Never attach `auth.json`, `.credentials.json`, Keychain exports, complete provider homes, or unredacted login logs to an issue.
- Imported credentials are copies. Revoking or rotating the original token family can invalidate an imported account. Independent login flows avoid this coupling.
- Removing an account registration retains its provider directory and does not log it out. Uninstalling the package also retains account data.

Rotation is disabled by default. It acts on supported structured quota-error events and stops after trying each eligible account once. It cannot guarantee a safe replay after an agent has changed files or called tools; `--allow-replay` explicitly permits restarting that work. The wrapper cannot change credentials already held by another process.

The application executes provider binaries found on `PATH`; use trusted installations and do not prepend untrusted directories. Provider configuration, project instructions, hooks, plugins, and tools execute under the native CLI's permission model.

## Report a vulnerability

Use GitHub's **Report a vulnerability** option on the repository's Security tab when available. If it is unavailable, open an issue containing only a request for a private reporting channel, without exploit details or credentials. Do not post secret values or personal account data publicly.

The project is new and has no guaranteed response time or long-term support schedule. Security fixes target the current development version.
