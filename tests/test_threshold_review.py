"""Independent subprocess regression for authoritative native quota denial."""

import json
import unittest

import test_acceptance as _acceptance


class ThresholdReviewTests(unittest.TestCase):
    def setUp(self):
        self.h = _acceptance.AcceptanceTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_codex_denial_without_percentages_never_receives_the_task(self):
        primary, secondary = self.h.pair("codex", "success")
        for denied in (
            {"ordinaryUsageAllowed": False},
            {"ordinaryUsageAllowed": False, "rateLimits": None},
            {"ordinaryUsageAllowed": False, "rateLimits": {}},
        ):
            with self.subTest(denied=denied):
                self.h.success("use", "codex", "primary")
                self.h.env["AA_TEST_USAGE"] = json.dumps(
                    {
                        primary["home"]: denied,
                        secondary["home"]: {"rateLimits": {"primary": {"usedPercent": 10}}},
                    }
                )
                before = len(self.h.calls())

                result = self.h.batch("codex", "--auto-rotate")

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    [call["home"] for call in self.h.calls()[before:]], [secondary["home"]]
                )
                self.assertNotIn("usage unavailable", result.stderr)
                profiles = {profile["name"]: profile for profile in self.h.profiles("codex")}
                self.assertTrue(profiles["secondary"]["active"])
                self.assertEqual(profiles["primary"]["cooldown_until"], 0)


if __name__ == "__main__":
    unittest.main()
