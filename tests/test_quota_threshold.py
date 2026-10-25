"""Quota thresholds use provider-wide usage, never token counts or model text."""

import unittest

from agent_accounts import quota


class QuotaThresholdTests(unittest.TestCase):
    def test_codex_uses_highest_primary_or_secondary_percentage(self):
        snapshot = quota.parse_usage(
            "codex",
            {"rateLimits": {"primary": {"usedPercent": 20}, "secondary": {"usedPercent": 95}}},
        )
        self.assertEqual(snapshot.used_percent, 95)
        self.assertTrue(snapshot.exceeds(95))
        self.assertFalse(snapshot.exceeds(96))

    def test_codex_canonical_bucket_wins_over_legacy_and_other_buckets(self):
        snapshot = quota.parse_usage(
            "codex",
            {
                "rateLimits": {"primary": {"usedPercent": 100}},
                "rateLimitsByLimitId": {
                    "codex": {"primary": {"usedPercent": 40}},
                    "codex_other": {"primary": {"usedPercent": 100}},
                },
            },
        )
        self.assertFalse(snapshot.exceeds(95))
        self.assertEqual(snapshot.used_percent, 40)

    def test_codex_unrelated_legacy_bucket_is_not_provider_wide_usage(self):
        self.assertIsNone(
            quota.parse_usage(
                "codex", {"rateLimits": {"limitId": "other", "primary": {"usedPercent": 99}}}
            )
        )

    def test_authoritative_codex_denial_is_respected_even_with_low_or_missing_usage(self):
        for limits in ({"primary": {"usedPercent": 1}}, None):
            with self.subTest(limits=limits):
                snapshot = quota.parse_usage(
                    "codex", {"ordinaryUsageAllowed": False, "rateLimits": limits}
                )
                self.assertTrue(snapshot.blocked)
                self.assertTrue(snapshot.exceeds(95))

    def test_claude_usage_is_percentage_not_fraction_and_uses_both_windows(self):
        for percent in (0.95, 90, 94.99, 95, 100, 101):
            with self.subTest(percent=percent):
                snapshot = quota.parse_usage(
                    "claude",
                    {
                        "rate_limits_available": True,
                        "rate_limits": {
                            "five_hour": {"utilization": 0},
                            "seven_day": {"utilization": percent},
                        },
                    },
                )
                self.assertEqual(snapshot.exceeds(95), percent >= 95)
                self.assertEqual(snapshot.used_percent, percent)

    def test_claude_model_scoped_usage_does_not_gate_an_unrelated_model(self):
        snapshot = quota.parse_usage(
            "claude",
            {
                "rate_limits_available": True,
                "rate_limits": {
                    "five_hour": {"utilization": 10},
                    "seven_day": None,
                    "seven_day_opus": {"utilization": 99},
                    "model_scoped": [{"display_name": "Other model", "utilization": 100}],
                },
            },
        )
        self.assertEqual(snapshot.used_percent, 10)
        self.assertFalse(snapshot.exceeds(95))

    def test_unavailable_or_invalid_usage_is_unknown_not_zero(self):
        for provider, payload in (
            ("codex", None),
            ("codex", {}),
            ("codex", {"rateLimits": {"primary": None, "secondary": None}}),
            (
                "claude",
                {"rate_limits_available": False, "rate_limits": {"five_hour": {"utilization": 99}}},
            ),
            ("claude", {"rate_limits_available": True, "rate_limits": {}}),
        ):
            with self.subTest(provider=provider, payload=payload):
                self.assertIsNone(quota.parse_usage(provider, payload))
        for percent in (True, "95", -1, float("nan"), float("inf"), 10**1000):
            for provider in ("codex", "claude"):
                payload = (
                    {"rateLimits": {"primary": {"usedPercent": percent}}}
                    if provider == "codex"
                    else {
                        "rate_limits_available": True,
                        "rate_limits": {"five_hour": {"utilization": percent}},
                    }
                )
                with self.subTest(provider=provider, percent=percent):
                    self.assertIsNone(quota.parse_usage(provider, payload))

    def test_past_reset_does_not_override_current_reported_usage(self):
        snapshot = quota.parse_usage(
            "codex", {"rateLimits": {"primary": {"usedPercent": 98, "resetsAt": 1}}}
        )
        self.assertTrue(snapshot.exceeds(95))


if __name__ == "__main__":
    unittest.main()
