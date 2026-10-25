"""Provider-wide quota snapshots obtained through the native CLI protocols."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Snapshot:
    used_percent: float | None
    blocked: bool = False

    def exceeds(self, threshold: int) -> bool:
        return self.blocked or self.used_percent is not None and self.used_percent >= threshold


def _percentage(value) -> float | None:
    # Compare before converting: maliciously large JSON integers must not
    # overflow, and bool is not a reported percentage. Over-100 usage is valid.
    if type(value) in (int, float) and 0 <= value <= 1_000_000:
        return float(value)
    return None


def parse_usage(provider: str, payload: dict | None) -> Snapshot | None:
    if not isinstance(payload, dict):
        return None
    if provider == "codex":
        # Permission comes from the backend, never from a reset timestamp.
        allowed = payload.get("ordinaryUsageAllowed")
        if allowed is not None and type(allowed) is not bool:
            return None
        if payload.get("ordinaryUsageAllowed") is False:
            return Snapshot(None, blocked=True)
        buckets = payload.get("rateLimitsByLimitId")
        if buckets is not None and not isinstance(buckets, dict):
            return None
        limits = (
            buckets["codex"]
            if isinstance(buckets, dict) and "codex" in buckets
            else payload.get("rateLimits")
        )
        if not isinstance(limits, dict) or limits.get("limitId") not in (None, "codex"):
            return None
        windows = [limits.get("primary"), limits.get("secondary")]
        field = "usedPercent"
    else:
        if payload.get("rate_limits_available") is not True:
            return None
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            return None
        # Model-scoped display labels are not stable model IDs. Only the
        # provider-wide five-hour and weekly windows apply to every invocation.
        windows = [limits.get("five_hour"), limits.get("seven_day")]
        field = "utilization"
    percentages = []
    for window in windows:
        if window is None:
            continue
        if not isinstance(window, dict):
            return None
        value = window.get(field)
        if provider == "codex" and type(value) is not int:
            return None
        if value is None:
            continue
        percent = _percentage(value)
        if percent is None:
            return None
        if provider == "claude" and window.get("resets_at") is not None:
            try:
                reset = datetime.fromisoformat(window["resets_at"].replace("Z", "+00:00"))
                if reset.tzinfo is None:
                    return None
                if reset.timestamp() <= time.time():
                    # Claude may fall back to cached data. Expired windows
                    # cannot establish current usage, even if they say 100%.
                    continue
            except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                return None
        percentages.append(percent)
    return Snapshot(max(percentages)) if percentages else None


def read_usage(provider: str, env: dict[str, str]) -> Snapshot | None:
    # Lazy imports keep native process helpers independent of the run loop.
    if provider == "codex":
        from .codex_usage import read_usage as native_usage
    else:
        from .claude_usage import read_usage as native_usage

    return parse_usage(provider, native_usage(env))
