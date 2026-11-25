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
SERVICE_IMAGE = (
    "ghcr.io/tevo-studio/tevo-license-mobile-sdk@sha256:"
    "cfcf54ffbc989049d85bca3e74677cd4e42aca2ed8c5b244f5ba4c066b9d2d8b"
)


class LicenseGateContractTest(unittest.TestCase):
    def test_both_sdk_journeys_are_ready_for_text_review(self):
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
                self.assertEqual(
                    set(data["app"]),
                    {"package", "proof_variant", "proof_variant_debuggable", "license_proof_port"},
                )
                self.assertEqual(data["inputs"]["service_image"], SERVICE_IMAGE)
                self.assertEqual(data["observation"]["wait_timeout_seconds"], 12)
                self.assertIn(data["inputs"]["service_image"], text)
                self.assertIn(data["inputs"]["service_sha"], text)
                for binary_requirement in (
                    "apk",
                    "signer_certificate_sha256",
                    "artifact_location",
                    "build_command",
                ):
                    self.assertNotIn(binary_requirement, text.casefold())
                self.assertEqual(set(data["scenarios"]), set(SCENARIOS))
                self.assertEqual(data["case_order"], CASE_ORDER)
                self.assertFalse(data["debug_case_input"]["available_in_pinned_sources"])
                self.assertEqual(
                    data["debug_case_input"]["disposition"],
                    "NOT-RUN in CHE-3101; device follow-up is CHE-9713",
                )
                self.assertIn("Device cases are NOT-RUN in CHE-3101", text)
                self.assertIn("adb -s <device_serial> shell pm clear", text)
                self.assertIn("adb -s <device_serial> logcat --pid <launch_pid>", text)
                self.assertIn("12 seconds", text)
                self.assertNotIn("leave `Checking license`", text)
                self.assertIn("terminal result", text)
                self.assertIn("logcat --pid <launch_pid> -v epoch -d", text)
                self.assertIn("timeout 15", text)
                self.assertIn("next whole second", text)
                self.assertIn("Cf-Access-Jwt-Assertion", text)
                self.assertIn("Origin: http://127.0.0.1:8080", text)
                self.assertIn("license-test-local", text)
                self.assertIn("--data-urlencode", text)
                self.assertIn("database", text)
                self.assertIn("docker compose", text)
                self.assertTrue(all(value in text for value in SCENARIOS.values()))
                self.assertEqual(data["initialization"]["coverage"], "ASSERTED_PER_CASE")
                init_log = data["observation"]["initialization_log"]
                self.assertEqual(init_log["tag"], "LicenseProof")
                self.assertEqual(init_log["level"], "INFO")
                self.assertEqual(init_log["matches_per_case"], 1)
                self.assertTrue(init_log["same_launch_pid"])
                self.assertEqual(init_log["wait_timeout_seconds"], 12)
                self.assertTrue(init_log["missing_or_duplicate_or_error_fails"])
                self.assertEqual(
                    init_log["message_regex_by_case"],
                    {
                        "approved": "init=proceeded",
                        "pending": "init=denied",
                        "revoked": "init=denied",
                        "offline-no-cache": "init=proceeded",
                    },
                )
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
                self.assertIn("queues a background license fetch", text)
                self.assertIn("does not skip contacting the service", text)
                self.assertIn("NOT-RUN", data["scenarios"]["debug-pending"])
                self.assertTrue(
                    {"logcat", "screen", "server_row", "app_package", "device_serial"}
                    <= set(data["evidence_per_case"])
                )


if __name__ == "__main__":
    unittest.main()
