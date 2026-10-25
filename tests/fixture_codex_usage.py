"""Offline NDJSON protocol peer; never reads credentials or contacts a service."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    mode = sys.argv[1]
    if mode == "detached_host":
        from agent_accounts.usage_process import JsonProcess, UsageUnavailable

        try:
            with JsonProcess(
                [sys.executable, __file__, "detached_parent"], dict(os.environ), timeout=0.15
            ) as peer:
                peer.receive()
        except UsageUnavailable:
            print("unavailable", flush=True)
        return
    if mode == "detached_child":
        Path(os.environ["USAGE_TEST_DETACHED_PID"]).write_text(str(os.getpid()))
        time.sleep(20)
        return
    if mode == "probe_host":
        from agent_accounts.usage_process import JsonProcess

        try:
            with JsonProcess(
                [sys.executable, __file__, "silence"], dict(os.environ), timeout=10
            ) as peer:
                peer.receive()
        except KeyboardInterrupt:
            sys.exit(130)
        return
    if os.environ.get("USAGE_TEST_PID"):
        Path(os.environ["USAGE_TEST_PID"]).write_text(str(os.getpid()))
    if mode == "detached_parent":
        subprocess.Popen([sys.executable, __file__, "detached_child"], start_new_session=True)
        time.sleep(20)
        return
    if mode == "echo_fragments":
        event = json.loads(sys.stdin.buffer.readline())
        response = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        for offset in range(0, len(response), 13):
            os.write(sys.stdout.fileno(), response[offset : offset + 13])
        return
    if mode == "silence":
        time.sleep(20)
        return
    if mode == "flood":
        print("x" * (1024 * 1024 + 1), flush=True)
        time.sleep(20)
        return
    if mode == "malformed":
        print("not a JSON envelope", flush=True)
        time.sleep(20)
        return
    if mode == "message_flood":
        for index in range(300):
            print(json.dumps({"method": "synthetic/notification", "index": index}), flush=True)
        time.sleep(20)
        return
    initialized = False
    for line in sys.stdin:
        event = json.loads(line)
        if os.environ.get("USAGE_TEST_LOG"):
            with open(os.environ["USAGE_TEST_LOG"], "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
        method = event.get("method")
        if method == "initialize":
            print(json.dumps({"id": event["id"], "result": {"userAgent": "synthetic"}}), flush=True)
        elif method == "initialized":
            initialized = True
        elif method == "account/rateLimits/read":
            if not initialized:
                sys.exit(98)
            if mode == "unsupported":
                result = {"id": event["id"], "error": {"code": -32601, "message": "unsupported"}}
            elif mode == "missing_result":
                result = {"id": event["id"], "result": []}
            elif mode == "denied":
                result = {"id": event["id"], "result": {"ordinaryUsageAllowed": False}}
            elif mode == "canonical_only":
                result = {
                    "id": event["id"],
                    "result": {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 95}}}},
                }
            else:
                result = {
                    "id": event["id"],
                    "result": {
                        "accountId": "synthetic-account",
                        "ordinaryUsageAllowed": True,
                        "rateLimits": {
                            "limitId": "codex",
                            "primary": {
                                "usedPercent": 95,
                                "resetsAt": 4102444800,
                                "windowDurationMins": 300,
                            },
                            "secondary": None,
                        },
                    },
                }
                if mode == "notifications":
                    print(json.dumps({"method": "account/updated", "params": {}}), flush=True)
                    print(json.dumps({"id": 987, "result": {"rateLimits": {}}}), flush=True)
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
