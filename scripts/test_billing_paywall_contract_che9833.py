import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JOURNEY = ROOT / "qualification/journeys/billing_paywall_test_store_mmonetize.md"
MANIFEST = ROOT / "qualification/manifests/billing_paywall_test_store_mmonetize.v1.json"
EXPECTED_EVIDENCE = {
    "screenshot",
    "logcat_line",
    "revenuecat_customer_entitlement_state",
    "testcase_revision",
    "app_build_sha",
    "device_serial",
    "command_exit_status",
}


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
        self.assertEqual(manifest["revenuecat_test_store"]["offering_id"], "che801_test")
        self.assertEqual(manifest["revenuecat_test_store"]["entitlement_id"], "premium")
        self.assertFalse(manifest["authoring_boundary"]["device_or_adb_commands_run"])
        self.assertFalse(manifest["authoring_boundary"]["apk_built_or_installed"])
        self.assertFalse(manifest["authoring_boundary"]["artemis_execution"])
        self.assertEqual(manifest["review_contract"]["independent_non_author_reviewers_required"], 2)
        self.assertTrue(manifest["review_contract"]["same_revision_required"])
        self.assertTrue(manifest["review_contract"]["must_precede_execution"])

        for phrase in (
            "**A — Offering and prices.**",
            "**B — Purchase.**",
            "**C — Restore.**",
            "**D — Cancel.**",
            "**E — Billing not configured.**",
            "Premium active",
            "onPurchased",
            "Billing is not configured",
            "two independent non-author reviewers",
            "or use a device or ADB.",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
