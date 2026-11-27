import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JOURNEY = ROOT / "qualification/journeys/billing_paywall_test_store_mmonetize.md"
MANIFEST = ROOT / "qualification/manifests/billing_paywall_test_store_mmonetize.v1.json"
EXPECTED_EVIDENCE = {
    "privacy_safe_screenshot_or_continuous_screen_recording",
    "specified_logcat_evidence_or_case_E_absence_capture",
    "revenuecat_dashboard_customer_entitlement_or_explicit_not_applicable",
    "testcase_revision",
    "sample_source_commit_sha",
    "apk_sha256",
    "device_serial",
    "command_exit_status",
    "timeout_or_completion_disposition",
}
KEY_LIKE_VALUE = re.compile(r"\b(?:test|appl|goog|amzn|amazon|rcb|sk_live)_[A-Za-z0-9_-]{16,}\b")


class BillingPaywallContractTest(unittest.TestCase):
    def test_manifest_and_journey_cover_reviewable_cases_and_evidence(self):
        text = JOURNEY.read_text(encoding="utf-8")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["issue"], "CHE-9833")
        self.assertEqual(manifest["status"], "provisional")
        self.assertEqual(set(manifest["cases"]), {"A", "B", "C", "D", "E"})
        self.assertEqual(set(manifest["evidence_per_case"]), EXPECTED_EVIDENCE)
        self.assertEqual(manifest["execution_status"], "NOT-RUN")
        self.assertEqual(manifest["app"]["package_name"], "io.mmonetize.sample")
        builds = manifest["app"]["builds"]
        self.assertEqual(builds["configured_test_store"]["variant"], "adsTestDebug")
        self.assertEqual(builds["configured_test_store"]["cases"], ["A", "B", "C", "D"])
        self.assertEqual(builds["billing_not_configured"]["variant"], "adsTestDebug")
        self.assertEqual(builds["billing_not_configured"]["cases"], ["E"])
        self.assertEqual(
            builds["configured_test_store"]["sample_source_commit_sha"],
            builds["billing_not_configured"]["sample_source_commit_sha"],
        )
        self.assertEqual(
            manifest["revenuecat_test_store"]["product_id"], "che801_premium_monthly_test"
        )
        self.assertEqual(
            manifest["revenuecat_test_store"]["monthly_package_id"],
            "recorded_from_dashboard_before_run",
        )
        self.assertEqual(manifest["revenuecat_test_store"]["offering_id"], "che801_test")
        self.assertEqual(manifest["revenuecat_test_store"]["entitlement_id"], "premium")
        lookup_method = manifest["customer_lookup_method"]
        self.assertIn("sort by **Last Seen** newest first", text)
        self.assertIn("sandbox-data view", lookup_method)
        self.assertIn("sandbox-data view", text)
        self.assertIn("dashboard environment and filter", lookup_method)
        self.assertIn("dashboard environment and filter", text)
        for phrase in (
            "unique customer",
            "last-seen time",
            "after that case's launch",
            "60 seconds",
            "NOT-RUN",
            "BLOCKED",
        ):
            self.assertIn(phrase, lookup_method)
            self.assertIn(phrase, text)
        self.assertFalse(manifest["authoring_boundary"]["device_or_adb_commands_run"])
        self.assertFalse(manifest["authoring_boundary"]["apk_built_or_installed"])
        self.assertFalse(manifest["authoring_boundary"]["artemis_execution"])
        self.assertEqual(
            manifest["review_contract"]["independent_non_author_reviewers_required"], 2
        )
        self.assertTrue(manifest["review_contract"]["same_revision_required"])
        self.assertTrue(manifest["review_contract"]["must_precede_execution"])

        case_headings = list(re.finditer(r"(?m)^\d+\. \*\*([A-E])\b", text))
        journey_cases = {
            heading.group(1): text[
                heading.end() : case_headings[index + 1].start()
                if index + 1 < len(case_headings)
                else len(text)
            ]
            for index, heading in enumerate(case_headings)
        }
        self.assertEqual(set(journey_cases), set(manifest["cases"]))
        for case_id, case in manifest["cases"].items():
            self.assertIn(case["assertion"], journey_cases[case_id])
            self.assertIn(case["logcat_oracle"], journey_cases[case_id])
            self.assertIn(case["build"], {"configured_test_store", "billing_not_configured"})
        case_c = manifest["cases"]["C"]
        self.assertIn("perform Restore for either identity", case_c["identity_precondition"])
        self.assertIn("required Active restore result is NOT-RUN", case_c["assertion"])
        self.assertIn("case C is BLOCKED, never PASS", case_c["assertion"])
        self.assertNotIn("RestoreEmpty", case_c["assertion"])
        self.assertIn("No purchases to restore", case_c["supplementary_restore_ui"])
        self.assertIn("Restore failed. Please try again.", case_c["supplementary_restore_ui"])
        self.assertEqual(case_c["restore_failure_disposition"], "BLOCKED: infra")
        self.assertIn("B's recorded ID before and after Restore", case_c["customer_entitlement"])
        self.assertIn(case_c["identity_mismatch_expected"], journey_cases["C"])
        self.assertEqual(case_c["completion_timeout_seconds"], 120)
        self.assertIn("within 120 seconds", journey_cases["C"])
        self.assertIn("NOT-RUN", journey_cases["C"])
        self.assertIn(
            "Always attempt Restore",
            manifest["fixture_reset"]["case_C_restore_outcomes"],
        )
        self.assertIn(
            "NOT-RUN",
            manifest["fixture_reset"]["case_C_restore_outcomes"],
        )
        self.assertIn(
            "No purchases to restore", manifest["fixture_reset"]["case_C_restore_outcomes"]
        )
        self.assertIn(
            "Restore failed. Please try again.",
            manifest["fixture_reset"]["case_C_restore_outcomes"],
        )
        self.assertIn(
            "NOT-RUN because the anonymous App User ID changed after pm clear", journey_cases["C"]
        )
        self.assertIn("case C is BLOCKED, never PASS", journey_cases["C"])
        self.assertIn("supplementary evidence only", journey_cases["C"])
        self.assertIn("dashboard Restore Behavior setting", journey_cases["C"])
        self.assertIn("BLOCKED (infra)", journey_cases["C"])
        self.assertIn("test", KEY_LIKE_VALUE.pattern)
        self.assertEqual(
            manifest["cases"]["E"]["customer_entitlement"],
            "NOT_APPLICABLE: billing not initialized",
        )
        self.assertEqual(
            manifest["cases"]["E"]["logcat_expected"],
            "absent during the captured startup log window",
        )

        for phrase in (
            "**A — Offering and prices.**",
            "**B — Purchase.**",
            "**C — Restore.**",
            "**D — Cancel.**",
            "**E — Billing not configured.**",
            "Premium active",
            "Billing is not configured",
            "two independent non-author reviewers",
            "or use a device or ADB.",
            "within 60 seconds",
            "following 30 seconds",
            "NOT-RUN",
        ):
            self.assertIn(phrase, text)

        self.assertIsNone(KEY_LIKE_VALUE.search(text))
        self.assertIsNone(KEY_LIKE_VALUE.search(MANIFEST.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
