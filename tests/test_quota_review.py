"""Independent native-schema and cached-window edge cases for quota decisions."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agent_accounts import quota


class QuotaReviewTests(unittest.TestCase):
    now = 1_800_000_000

    def stamp(self, delta, offset=0):
        zone = timezone(timedelta(hours=offset))
        return datetime.fromtimestamp(self.now + delta, tz=zone).isoformat()

    def claude(self, five_hour, seven_day=None):
        return {
            "rate_limits_available": True,
            "rate_limits": {"five_hour": five_hour, "seven_day": seven_day},
        }

    def test_malformed_codex_permission_flag_does_not_become_a_quota_decision(self):
        for flag in (0, 1, "false", "true", [], {}):
            with self.subTest(flag=flag):
                self.assertIsNone(
                    quota.parse_usage(
                        "codex",
                        {
                            "ordinaryUsageAllowed": flag,
                            "rateLimits": {"primary": {"usedPercent": 98}},
                        },
                    )
                )

    def test_malformed_codex_bucket_map_does_not_silently_fall_back_to_legacy(self):
        for buckets in ([], "codex", 0, False):
            with self.subTest(buckets=buckets):
                self.assertIsNone(
                    quota.parse_usage(
                        "codex",
                        {
                            "rateLimitsByLimitId": buckets,
                            "rateLimits": {"primary": {"usedPercent": 99}},
                        },
                    )
                )

    def test_existing_codex_windows_require_the_native_integer_percentage(self):
        for window in ({}, {"usedPercent": None}, {"usedPercent": 95.5}):
            with self.subTest(window=window):
                self.assertIsNone(
                    quota.parse_usage(
                        "codex",
                        {"rateLimits": {"primary": window, "secondary": {"usedPercent": 96}}},
                    )
                )

    def test_nullable_codex_metadata_and_absent_windows_still_allow_known_usage(self):
        for buckets in (None, {}, {"another-model": {"primary": {"usedPercent": 100}}}):
            with self.subTest(buckets=buckets):
                snapshot = quota.parse_usage(
                    "codex",
                    {
                        "ordinaryUsageAllowed": None,
                        "rateLimitsByLimitId": buckets,
                        "rateLimits": {
                            "limitId": None,
                            "primary": None,
                            "secondary": {"usedPercent": 95},
                        },
                    },
                )
                self.assertIsNotNone(snapshot)
                self.assertTrue(snapshot.exceeds(95))

    def test_malformed_or_conflicting_canonical_codex_bucket_does_not_use_legacy(self):
        for canonical in (None, [], {"limitId": "another-model", "primary": {"usedPercent": 20}}):
            with self.subTest(canonical=canonical):
                self.assertIsNone(
                    quota.parse_usage(
                        "codex",
                        {
                            "rateLimitsByLimitId": {"codex": canonical},
                            "rateLimits": {"primary": {"usedPercent": 99}},
                        },
                    )
                )

    def test_authoritative_codex_denial_does_not_depend_on_other_snapshot_fields(self):
        snapshot = quota.parse_usage(
            "codex",
            {"ordinaryUsageAllowed": False, "rateLimitsByLimitId": [], "rateLimits": None},
        )
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.blocked)
        self.assertTrue(snapshot.exceeds(100))

    def test_claude_ignores_expired_cache_but_keeps_a_current_general_window(self):
        with patch.object(quota.time, "time", return_value=self.now):
            for expiry in (-1, 0):
                with self.subTest(expiry=expiry):
                    stale = {"utilization": 100, "resets_at": self.stamp(expiry)}
                    current = {"utilization": 35, "resets_at": self.stamp(3600, offset=2)}
                    for five_hour, seven_day in ((stale, current), (current, stale)):
                        snapshot = quota.parse_usage("claude", self.claude(five_hour, seven_day))
                        self.assertIsNotNone(snapshot)
                        self.assertEqual(snapshot.used_percent, 35)
                        self.assertFalse(snapshot.exceeds(95))

    def test_claude_only_expired_or_unknown_windows_are_unknown_usage(self):
        with patch.object(quota.time, "time", return_value=self.now):
            expired = {"utilization": 98, "resets_at": self.stamp(-1)}
            self.assertIsNone(quota.parse_usage("claude", self.claude(expired)))
            self.assertIsNone(
                quota.parse_usage("claude", self.claude(expired, {"utilization": None}))
            )

    def test_malformed_claude_reset_timestamp_cannot_establish_usage(self):
        for reset in (True, 1_800_000_000, "2030-01-01T12:00:00", "not-a-timestamp", {}):
            with self.subTest(reset=reset):
                self.assertIsNone(
                    quota.parse_usage(
                        "claude",
                        self.claude(
                            {"utilization": 99, "resets_at": reset},
                            {"utilization": 10, "resets_at": None},
                        ),
                    )
                )

    def test_claude_fractional_percentages_and_overage_use_inclusive_thresholds(self):
        for threshold in (1, 95, 100):
            for percent, expected in ((threshold - 0.01, False), (threshold, True), (100.5, True)):
                with self.subTest(threshold=threshold, percent=percent):
                    snapshot = quota.parse_usage("claude", self.claude({"utilization": percent}))
                    self.assertIsNotNone(snapshot)
                    self.assertEqual(snapshot.exceeds(threshold), expected)


if __name__ == "__main__":
    unittest.main()
