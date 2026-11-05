import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "approved": "Licensed",
    "pending": "NotLicensed",
    "revoked": "NotLicensed",
    "offline-no-cache": "Unverified",
    "debug-pending": "debuggable",
}


class LicenseGateContractTest(unittest.TestCase):
    def test_both_sdk_journeys_cover_the_device_contract(self):
        for sdk in ("mmonetize", "tinos"):
            with self.subTest(sdk=sdk):
                journey = ROOT / "qualification/journeys" / f"license_proof_{sdk}.md"
                manifest = ROOT / "qualification/manifests" / f"license_proof_{sdk}.v1.json"
                text = journey.read_text(encoding="utf-8")
                data = json.loads(manifest.read_text(encoding="utf-8"))

                self.assertEqual(data["issue"], "CHE-3101")
                self.assertEqual(data["sdk"], sdk)
                self.assertEqual(data["service"]["license_test_port"], 18080)
                self.assertEqual(set(data["scenarios"]), set(SCENARIOS))
                self.assertFalse(data["debug_case_input"]["available_in_pinned_sources"])
                self.assertEqual(data["debug_case_input"]["disposition"], "NOT-RUN until a compatible APK is pinned")
                self.assertIn("adb -s <device_serial> shell pm clear", text)
                self.assertIn("fixture DB", text)
                self.assertIn("docker compose", text)
                self.assertTrue(all(value in text for value in SCENARIOS.values()))
                self.assertTrue(
                    {"logcat", "screen", "server_row", "apk_sha256", "device_serial"}
                    <= set(data["evidence_per_case"])
                )


if __name__ == "__main__":
    unittest.main()
