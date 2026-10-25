"""Synthetic process-tree and cancellation scenarios for the second runner review."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    if sys.argv[1:] == ["descendant"]:
        if os.environ.get("AA_R2_IGNORE_TERM") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(os.environ["AA_R2_CHILD"]).write_text(
            json.dumps({"pid": os.getpid(), "group": os.getpgrp()}), encoding="utf-8"
        )
        heartbeat = Path(os.environ["AA_R2_HEARTBEAT"])
        deadline = time.monotonic() + 15
        sequence = 0
        while time.monotonic() < deadline:
            sequence += 1
            heartbeat.write_text(str(sequence), encoding="ascii")
            time.sleep(0.02)
        return

    home = os.environ["CODEX_HOME"]
    with open(os.environ["AA_TEST_CALLS"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"home": home, "pid": os.getpid()}) + "\n")
    mode = os.environ["AA_R2_MODE"]
    if mode == "hold_primary":
        if home == os.environ["AA_R2_PRIMARY_HOME"]:
            Path(os.environ["AA_R2_READY"]).write_text(
                json.dumps({"pid": os.getpid(), "group": os.getpgrp()}), encoding="utf-8"
            )
            print(json.dumps({"type": "thread.started", "thread_id": "synthetic"}), flush=True)
            time.sleep(15)
        else:
            print(json.dumps({"type": "turn.completed"}), flush=True)
        return
    if mode in {"leader_exits", "leader_waits"}:
        subprocess.Popen(
            [sys.executable, __file__, "descendant"],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while not Path(os.environ["AA_R2_CHILD"]).exists():
            if time.monotonic() > deadline:
                sys.exit(98)
            time.sleep(0.01)
        Path(os.environ["AA_R2_READY"]).write_text(
            json.dumps({"pid": os.getpid(), "group": os.getpgrp()}), encoding="utf-8"
        )
        print(json.dumps({"type": "thread.started", "thread_id": "synthetic"}), flush=True)
        if mode == "leader_exits":
            os._exit(0)
        time.sleep(15)
        return
    if mode == "quota_then_signal":
        if home == os.environ["AA_R2_PRIMARY_HOME"]:
            print(
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {
                            "code": "usage_limit_reached",
                            "message": "You've hit your usage limit.",
                        },
                    }
                ),
                flush=True,
            )
            Path(os.environ["AA_R2_READY"]).write_text(
                json.dumps({"pid": os.getpid(), "group": os.getpgrp()}), encoding="utf-8"
            )
            time.sleep(15)
        else:
            print(json.dumps({"type": "turn.completed"}), flush=True)


if __name__ == "__main__":
    main()
