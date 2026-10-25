"""Read native Codex account usage without starting a thread or model turn."""

from __future__ import annotations

from . import __version__, providers
from .store import AccountError
from .usage_process import JsonProcess, UsageUnavailable


def _response(peer: JsonProcess, request_id: int) -> dict:
    while True:
        event = peer.receive()
        if type(event.get("id")) is not int or event["id"] != request_id:
            continue
        if "error" in event or not isinstance(event.get("result"), dict):
            raise UsageUnavailable("Codex usage request was unavailable.")
        return event["result"]


def read_usage(env: dict[str, str]) -> dict | None:
    home = env.get("CODEX_HOME")
    if not home:
        return None
    try:
        command = providers.command("codex", ["app-server", "--listen", "stdio://"])
        with JsonProcess(command, env, cwd=home) as peer:
            peer.send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "agent-accounts",
                            "title": "Agent Accounts",
                            "version": __version__,
                        },
                        "capabilities": {"experimentalApi": False},
                    },
                }
            )
            _response(peer, 1)
            peer.send({"method": "initialized"})
            peer.send({"id": 2, "method": "account/rateLimits/read"})
            return _response(peer, 2)
    except (UsageUnavailable, AccountError):
        return None
