"""Read Claude Code's experimental native usage control response without a prompt."""

from __future__ import annotations

from . import providers
from .store import AccountError
from .usage_process import JsonProcess, UsageUnavailable


def _request(child: JsonProcess, request_id: str, request: dict) -> dict:
    child.send({"type": "control_request", "request_id": request_id, "request": request})
    while True:
        event = child.receive()
        response = event.get("response")
        if (
            event.get("type") != "control_response"
            or not isinstance(response, dict)
            or response.get("request_id") != request_id
        ):
            continue
        data = response.get("response")
        if response.get("subtype") != "success" or not isinstance(data, dict):
            raise UsageUnavailable
        return data


def read_usage(env: dict[str, str]) -> dict | None:
    """Read experimental native usage; unsupported versions return unknown.

    Native Claude handles credential storage, refresh, and scope eligibility.
    This sends no prompt and does not scan transcripts. Its returned usage may
    come from Claude's own cache and does not include a freshness timestamp.
    """
    home = env.get("CLAUDE_CONFIG_DIR")
    if not home:
        return None
    args = [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--settings",
        '{"disableAllHooks":true}',
        "--tools",
        "",
    ]
    try:
        with JsonProcess(providers.command("claude", args), env, cwd=home) as child:
            _request(child, "usage-initialize", {"subtype": "initialize"})
            return _request(child, "usage-read", {"subtype": "get_usage", "skip_behaviors": True})
    except (UsageUnavailable, AccountError):
        return None
