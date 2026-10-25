"""Synthetic native control-protocol peer. Never reads credentials or calls a model."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def emit(event):
    print(json.dumps(event), flush=True)


def answer(request, payload, *, subtype="success", request_id=None):
    emit(
        {
            "type": "control_response",
            "response": {
                "subtype": subtype,
                "request_id": request_id or request["request_id"],
                "response": payload,
            },
        }
    )


mode = os.environ.get("SYNTHETIC_USAGE_MODE", "success")
capture = Path(os.environ["SYNTHETIC_USAGE_CAPTURE"])
requests = []
for line in sys.stdin:
    request = json.loads(line)
    requests.append(request)
    capture.write_text(
        json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "requests": requests}),
        encoding="utf-8",
    )
    assert request["type"] == "control_request", "Usage probes must never send model input"
    kind = request["request"]["subtype"]
    if kind == "initialize":
        if mode == "malformed_json":
            print("synthetic invalid JSON", flush=True)
            break
        if mode == "init_error":
            answer(request, {}, subtype="error")
            break
        emit({"type": "system", "subtype": "ui_invalidate"})
        answer(request, {"claude_code_version": "synthetic"})
    else:
        assert kind == "get_usage", "Only native usage requests are permitted"
        assert len(requests) == 2 and requests[0]["request"]["subtype"] == "initialize"
        assert request["request"]["skip_behaviors"] is True
        if mode == "unsupported":
            answer(request, {}, subtype="error")
            break
        if mode == "malformed_payload":
            answer(request, ["synthetic bad shape"])
            break
        if mode == "unavailable":
            answer(
                request,
                {"subscription_type": None, "rate_limits_available": False, "rate_limits": None},
            )
            break
        answer(
            request,
            {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 100}}},
            request_id="another-request",
        )
        if mode == "wrong_id_only":
            break
        answer(
            request,
            {
                "subscription_type": "max",
                "rate_limits_available": True,
                "rate_limits": {
                    "five_hour": {"utilization": 95.25, "resets_at": "2099-01-02T03:04:05Z"},
                    "seven_day": {"utilization": 21, "resets_at": "2099-01-08T03:04:05+00:00"},
                },
                "behaviors": None,
            },
        )
        break
