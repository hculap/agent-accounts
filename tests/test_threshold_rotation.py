"""Actual subprocess coverage of proactive account selection before task launch."""

import json
import unittest
from pathlib import Path

import test_acceptance as _acceptance


class ThresholdRotationTests(unittest.TestCase):
    def setUp(self):
        self.h = _acceptance.AcceptanceTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.probes = self.h.root / "quota probes.jsonl"
        self.h.env["AA_TEST_USAGE_CALLS"] = str(self.probes)

    def usage(self, *pairs):
        snapshots = {}
        for profile, percent in pairs:
            snapshots[profile["home"]] = (
                None
                if percent is None
                else {"rateLimits": {"primary": {"usedPercent": percent}}}
                if profile["provider"] == "codex"
                else {
                    "rate_limits_available": True,
                    "rate_limits": {"five_hour": {"utilization": percent}},
                }
            )
        self.h.env["AA_TEST_USAGE"] = json.dumps(snapshots)

    def probe_calls(self):
        return (
            [json.loads(line) for line in self.probes.read_text().splitlines()]
            if self.probes.exists()
            else []
        )

    def test_default_95_skips_before_task_launch_for_both_providers(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                primary, secondary = self.h.pair(provider, "success")
                self.usage((primary, 95), (secondary, 20))
                before = len(self.h.calls())
                result = self.h.batch(provider, "--auto-rotate")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([c["home"] for c in self.h.calls()[before:]], [secondary["home"]])
                self.assertIn("95%", result.stderr)
                self.assertNotIn("rateLimits", result.stdout)
                self.assertNotIn("control_response", result.stdout)
                self.assertEqual(
                    [p["name"] for p in self.h.profiles(provider) if p["active"]], ["secondary"]
                )

    def test_below_threshold_launches_selected_account(self):
        primary, secondary = self.h.pair("claude", "success")
        self.usage((primary, 94.99), (secondary, 0))
        result = self.h.batch("claude", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c["home"] for c in self.h.calls()], [primary["home"]])
        self.assertEqual(len(self.probe_calls()), 1)

    def test_separate_provider_settings_change_the_actual_boundary(self):
        self.h.success("settings", "set", "codex-rotation-threshold", "90")
        self.h.success("settings", "set", "claude-rotation-threshold", "95")
        for provider in ("codex", "claude"):
            primary, secondary = self.h.pair(provider, "success")
            self.usage((primary, 92), (secondary, 0))
            before = len(self.h.calls())
            result = self.h.batch(provider, "--auto-rotate")
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = secondary if provider == "codex" else primary
            self.assertEqual([c["home"] for c in self.h.calls()[before:]], [expected["home"]])

    def test_disabled_rotation_never_queries_usage_or_changes_account(self):
        primary, secondary = self.h.pair("codex", "success")
        self.usage((primary, 99), (secondary, 0))
        self.assertEqual(self.h.batch("codex").returncode, 0)
        self.h.success("settings", "set", "auto-rotate", "on")
        self.assertEqual(self.h.batch("codex", "--no-auto-rotate").returncode, 0)
        self.assertEqual([c["home"] for c in self.h.calls()], [primary["home"], primary["home"]])
        self.assertEqual(self.probe_calls(), [])

    def test_all_accounts_above_threshold_do_not_launch_or_spin(self):
        primary, secondary = self.h.pair("codex", "success")
        self.usage((primary, 95), (secondary, 99))
        result = self.h.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.h.calls(), [])
        self.assertEqual(len(self.probe_calls()), 2)
        self.assertIn("threshold", result.stderr)
        self.assertEqual([p["name"] for p in self.h.profiles("codex") if p["active"]], ["primary"])
        # Threshold skips do not set a quota cooldown: raising the percentage
        # or a newly reported reset can make the same account eligible now.
        self.h.success("settings", "set", "codex-rotation-threshold", "100")
        result = self.h.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c["home"] for c in self.h.calls()], [primary["home"]])

    def test_unknown_usage_warns_without_guessing_or_switching(self):
        primary, secondary = self.h.pair("codex", "success")
        self.usage((primary, None), (secondary, 0))
        result = self.h.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage unavailable", result.stderr)
        self.assertEqual([c["home"] for c in self.h.calls()], [primary["home"]])

    def test_hard_quota_fallback_also_checks_the_next_account_threshold(self):
        primary, secondary = self.h.pair("codex", "quota")
        tertiary = self.h.import_profile("codex", "third")
        self.h.success("use", "codex", "primary")
        self.usage((primary, None), (secondary, 97), (tertiary, 10))
        result = self.h.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c["home"] for c in self.h.calls()], [primary["home"], tertiary["home"]])

    def test_busy_fallback_is_skipped_without_querying_its_credentials(self):
        primary, secondary = self.h.pair("codex", "success", "hold")
        tertiary = self.h.import_profile("codex", "third")
        self.h.success("use", "codex", "secondary")
        process = self.h.holding_run()
        self.h.success("use", "codex", "primary")
        self.usage((primary, 95), (secondary, 0), (tertiary, 10))
        result = self.h.batch("codex", "--auto-rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [c["home"] for c in self.probe_calls()], [primary["home"], tertiary["home"]]
        )
        self.assertEqual([c["home"] for c in self.h.calls()], [secondary["home"], tertiary["home"]])
        Path(self.h.env["AA_TEST_RELEASE"]).touch()
        process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
