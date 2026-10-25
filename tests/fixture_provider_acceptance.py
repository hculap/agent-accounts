"""Synthetic native CLI used only by the independent acceptance suite."""

import json
import os
import signal
import sys
import time
from pathlib import Path


def main():
    provider = Path(sys.argv[0]).stem
    arguments = sys.argv[1:]
    if provider not in {"codex", "claude"}:
        provider = arguments.pop(0)
    home = os.environ["CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"]
    # Quota probes are control traffic only, never recorded as task execution.
    if "app-server" in arguments or "--input-format" in arguments:
        usage_calls = os.environ.get("AA_TEST_USAGE_CALLS")
        if usage_calls:
            with open(usage_calls, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"provider": provider, "home": home}) + "\n")
        snapshots = json.loads(os.environ.get("AA_TEST_USAGE", "{}"))
        snapshot = snapshots.get(
            home,
            {"rateLimits": {"primary": {"usedPercent": 0}}}
            if provider == "codex"
            else {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 0}}},
        )
        for line in sys.stdin:
            request = json.loads(line)
            if provider == "codex":
                method = request.get("method")
                if method == "initialized":
                    continue
                if method not in {"initialize", "account/rateLimits/read"}:
                    sys.exit(99)
                result = {} if method == "initialize" else snapshot
                response = {"id": request["id"], "result": result}
            else:
                method = request.get("request", {}).get("subtype")
                if method not in {"initialize", "get_usage"}:
                    sys.exit(99)
                result = {} if method == "initialize" else snapshot
                response = {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request["request_id"],
                        "response": result,
                    },
                }
            print(json.dumps(response), flush=True)
        return
    record = {"provider": provider, "home": home, "arguments": arguments, "pid": os.getpid()}
    if os.environ.get("AA_TEST_READ_STDIN"):
        record["stdin"] = sys.stdin.read()
    with open(os.environ["AA_TEST_CALLS"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    def emit(value):
        print(json.dumps(value), flush=True)

    def terminal_failure(message="Connection lost", code="connection_error"):
        if provider == "codex":
            emit({"type": "turn.failed", "error": {"message": message, "code": code}})
        else:
            emit(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "errors": [message],
                }
            )

    def terminal_success():
        if provider == "codex":
            emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        else:
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "fixture completed",
                }
            )

    def activity(text="fixture work", tool=False):
        if provider == "codex":
            item = (
                {"id": "fixture-item", "type": "command_execution", "command": "fixture"}
                if tool
                else {"id": "fixture-item", "type": "agent_message", "text": text}
            )
            emit({"type": "item.completed", "item": item})
        else:
            content = (
                {"type": "tool_use", "id": "fixture-tool", "name": "Bash", "input": {}}
                if tool
                else {"type": "text", "text": text}
            )
            emit({"type": "assistant", "message": {"role": "assistant", "content": [content]}})

    def limit_event():
        if provider == "codex":
            terminal_failure("You've hit your usage limit.", "usage_limit_reached")
        else:
            emit(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "resetsAt": int(time.time()) + 600,
                        "rateLimitType": "five_hour",
                    },
                }
            )
            terminal_failure("You've hit your usage limit.", "rate_limit")

    if provider == "claude" and arguments[:2] == ["auth", "status"]:
        emit(
            {
                "loggedIn": os.environ.get("AA_TEST_LOGGED_IN", "1") == "1",
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "configDirectory": home,
                "email": "fixture@example.invalid",
                "orgId": "synthetic-org",
                "orgName": "Synthetic",
                "subscriptionType": "max",
            }
        )
        sys.exit(0)

    if "login" in arguments:
        target = Path(home) / ("auth.json" if provider == "codex" else ".credentials.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(os.environ.get("AA_TEST_LOGIN_AUTH", "{}"), encoding="utf-8")
        sys.exit(int(os.environ.get("AA_TEST_LOGIN_EXIT", "0")))

    scenarios = json.loads(os.environ.get("AA_TEST_SCENARIOS", "{}"))
    scenario = scenarios.get(home, "success")
    if scenario == "flood":
        for number in range(10000):
            emit({"type": "thread.started", "thread_id": str(number)})
        sys.exit(0)
    if scenario == "hold":
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        Path(os.environ["AA_TEST_READY"]).touch()
        try:
            deadline = time.monotonic() + 30
            while not Path(os.environ["AA_TEST_RELEASE"]).exists():
                if time.monotonic() > deadline:
                    sys.exit(99)
                time.sleep(0.02)
        finally:
            Path(os.environ["AA_TEST_EXITED"]).touch()
        terminal_success()
        sys.exit(0)
    if scenario == "malformed_then_quota":
        print("{broken protocol", flush=True)
        limit_event()
        sys.exit(1)
    if scenario in {"quota", "work_quota", "assistant_quota"}:
        if scenario != "quota":
            activity(tool=scenario == "work_quota")
        limit_event()
        sys.exit(1)
    if scenario == "generic_failure":
        terminal_failure()
        sys.exit(17)
    if scenario == "auth_failure":
        if provider == "claude":
            emit({"type": "assistant", "error": "authentication_failed"})
        terminal_failure("Authentication failed", "authentication_error")
        sys.exit(18)
    if scenario == "prose_quota_failure":
        activity('You\'ve hit your usage limit. Example: {"code": "usage_limit_reached"}')
        terminal_failure()
        sys.exit(17)
    if scenario == "plain_quota_failure":
        print("You've hit your usage limit.", flush=True)
        sys.exit(1)
    if scenario == "claude_unstructured_result_failure":
        terminal_failure("You've hit your usage limit.", "rate_limit")
        sys.exit(1)
    if scenario == "warning_then_failure":
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}})
        terminal_failure()
        sys.exit(17)
    if scenario == "rejection_then_success":
        if provider == "codex":
            emit(
                {
                    "type": "error",
                    "error": {
                        "code": "usage_limit_reached",
                        "message": "You've hit your usage limit.",
                    },
                }
            )
        else:
            emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
        terminal_success()
        sys.exit(0)
    if scenario == "echo":
        emit(record)
        print("fixture stderr stays stderr", file=sys.stderr)
        sys.exit(0)
    if scenario == "cancel":
        sys.exit(130)
    if scenario == "refresh":
        target = Path(home) / ("auth.json" if provider == "codex" else ".credentials.json")
        data = json.loads(target.read_text(encoding="utf-8"))
        if provider == "codex":
            data["tokens"]["refresh_token"] = "synthetic-refreshed-token"
        else:
            data["claudeAiOauth"]["refreshToken"] = "synthetic-refreshed-token"
        target.write_text(json.dumps(data), encoding="utf-8")
    terminal_success()


if __name__ == "__main__":
    main()
