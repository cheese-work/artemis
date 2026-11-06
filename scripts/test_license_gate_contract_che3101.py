import json
import re
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
CASE_ORDER = ["pending", "approved", "revoked", "debug-pending", "offline-no-cache"]


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
                self.assertEqual(data["service"]["license_test_port"], 8080)
                self.assertEqual(data["app"]["license_proof_port"], 8080)
                self.assertEqual(data["service"]["app_base_url"], "http://10.0.2.2:8080")
                for pin in ("apk_sha256", "signer_certificate_sha256"):
                    self.assertRegex(data["app"][pin], re.compile(r"^[0-9a-f]{64}$"))
                self.assertTrue(data["app"]["artifact_location"])
                self.assertTrue(data["app"]["build_command"])
                self.assertEqual(data["app"]["artifact_available"], sdk == "mmonetize")
                self.assertEqual(data["observation"]["wait_timeout_seconds"], 12)
                self.assertIn(data["inputs"]["service_image"], text)
                self.assertIn(data["inputs"]["service_sha"], text)
                self.assertIn(data["app"]["apk_sha256"], text)
                self.assertIn(data["app"]["signer_certificate_sha256"], text)
                self.assertEqual(set(data["scenarios"]), set(SCENARIOS))
                self.assertEqual(data["case_order"], CASE_ORDER)
                self.assertFalse(data["debug_case_input"]["available_in_pinned_sources"])
                self.assertEqual(
                    data["debug_case_input"]["disposition"],
                    "NOT-RUN until a compatible APK is pinned",
                )
                self.assertIn("adb -s <device_serial> shell pm clear", text)
                self.assertIn("adb -s <device_serial> logcat --pid <launch_pid>", text)
                self.assertIn("12 seconds", text)
                self.assertIn("Cf-Access-Jwt-Assertion", text)
                self.assertIn("Origin: http://127.0.0.1:8080", text)
                self.assertIn("license-test-local", text)
                self.assertIn("--data-urlencode", text)
                self.assertIn("database", text)
                self.assertIn("docker compose", text)
                self.assertTrue(all(value in text for value in SCENARIOS.values()))
                self.assertEqual(data["initialization"]["coverage"], "NOT-PROVEN")
                self.assertTrue(data["initialization"]["gap_owner"])
                self.assertTrue(data["observation"]["denial_log"]["tag"])
                self.assertIn(data["observation"]["denial_log"]["level"], {"ERROR", "WARN"})
                self.assertTrue(data["observation"]["denial_log"]["message_contains"])
                expected_result_logs = data["observation"]["result_log"].get(
                    "message_regex_by_case", {}
                )
                self.assertEqual(set(expected_result_logs), set(SCENARIOS))
                for case, pattern in expected_result_logs.items():
                    compiled = re.compile(pattern)
                    self.assertIsNotNone(
                        compiled.search(
                            data["observation"]["result_log"]["message_contains_by_case"][case]
                        )
                    )
                if sdk == "mmonetize":
                    for case in ("approved", "debug-pending"):
                        self.assertIsNone(
                            re.compile(expected_result_logs[case]).search("NotLicensed")
                        )
                self.assertTrue(
                    all(json.dumps(value)[1:-1] in text for value in expected_result_logs.values())
                )
                self.assertIn("without contacting the service", text)
                self.assertIn("NOT-RUN", data["scenarios"]["debug-pending"])
                self.assertTrue(
                    {"logcat", "screen", "server_row", "apk_sha256", "device_serial"}
                    <= set(data["evidence_per_case"])
                )


if __name__ == "__main__":
    unittest.main()
