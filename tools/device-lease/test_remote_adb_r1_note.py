"""Offline acceptance checks for the R1 design note, not gateway/device tests."""

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
NOTE = Path(__file__).with_name("remote-adb-r1.md")
BULLETS = {
    "Authentication",
    "Tunnel",
    "Isolation",
    "Gateway",
    "End of access",
    "Broker restart",
    "Rejected",
}
CASES = {
    "stale-credential-after-reacquire": "Authentication",
    "nat-shared-node-without-credential": "Authentication",
    "restricted-command-bypass": "Gateway",
    "malformed-fragmented-coalesced-frames": "Gateway",
    "global-controls-and-implicit-transport": "Gateway",
    "release-expiry-revoke-authority-change": "End of access",
    "restart-reconcile": "Broker restart",
}


class RemoteAdbR1NoteTest(unittest.TestCase):
    def setUp(self):
        # Missing content is a failed acceptance assertion, not a collection error.
        self.note = NOTE.read_text(encoding="utf-8") if NOTE.exists() else ""

    def test_design_rules_trace_to_every_r1_bullet(self):
        rules = re.findall(r"^\*\*(R1-[A-Z]\d+) .+$", self.note, re.MULTILINE)
        self.assertTrue(rules, "No R1 design rules with DLP-1 citations")
        self.assertEqual(len(rules), len(set(rules)), "Duplicate design rule id")
        citations = set()
        for line in self.note.splitlines():
            if re.match(r"^\*\*R1-[A-Z]\d+ ", line):
                match = re.search(r"\(DLP-1 R1: ([^)]+)\)", line)
                self.assertIsNotNone(match, f"Untraced design rule: {line}")
                citations.add(match[1])
        self.assertEqual(citations, BULLETS)

    def test_slice_5b_negative_cases_have_citations_and_denial_assertions(self):
        for case, bullet in CASES.items():
            with self.subTest(case=case):
                row = next(
                    (line for line in self.note.splitlines() if line.startswith(f"| `{case}` |")),
                    "",
                )
                self.assertTrue(row, f"Missing slice 5b case: {case}")
                self.assertIn(f"DLP-1 R1: {bullet}", row)
                self.assertIn("DENY", row, "Case must state a denial assertion")
        for service in (
            "host:kill",
            "host:connect",
            "host:disconnect",
            "host:transport-any",
            "host:transport-usb",
        ):
            self.assertIn(f"`{service}`", self.note)

    def test_client_pin_and_shared_code_are_explicit(self):
        golden = json.loads((ROOT / "tests/support/golden/adb_protocol.json").read_text())
        self.assertIn(golden["versions"]["adb"], self.note)
        for required in (
            "artemis/runtime/adb_gateway.py",
            "apps/admin_console/services/host_tunnel.py",
            "No second parser",
            "NOT-RUN",
            "500c592",
        ):
            self.assertIn(required, self.note)

    def test_security_review_corrections_have_explicit_contracts(self):
        requirements = {
            "F1": (
                "deny every direct route to device-facing networks",
                "Wireless debugging is off",
                "TCP adbd is off",
                "paired/authorized adb key fingerprints match the pre-lease baseline",
                "`device-enabled-network-adbd`",
            ),
            "F2": (
                "Synthesize both feature replies from the per-lease approved set",
                "Never send plain `host:features` upstream",
                "feature-intersection",
            ),
            "F3": ("Reject any public-key fingerprint ever bound before", "old-key-registration"),
            "F4": ("log only the service family and decision", "payload-log-redaction"),
            "F5": ("loopback OUTPUT owner match", "tunnel account UID"),
        }
        for finding, clauses in requirements.items():
            for clause in clauses:
                with self.subTest(finding=finding, clause=clause):
                    self.assertTrue(clause in self.note, f"Missing {finding} contract: {clause}")

    def test_persistence_and_device_network_verification_are_explicit(self):
        clauses = (
            "installed packages with signer/version",
            "runtime permissions and appop grants",
            "enabled accessibility services",
            "notification listeners, device admins, input methods and role holders",
            "global/secure/system settings",
            "shell-UID processes and processes absent from the baseline",
            "Quarantine on any unexplained persistent-state difference",
            "`device-side-persistence`",
            "AP client isolation",
            "device-to-device paths",
        )
        for clause in clauses:
            with self.subTest(clause=clause):
                self.assertTrue(
                    clause in self.note, f"Missing persistence/network contract: {clause}"
                )

    def test_attestation_profile_closes_unlisted_persistent_state(self):
        release = self.note.partition("**R1-E2 ")[2].partition("**R1-B1 ")[0]
        for clause in (
            "reviewed attestation profile",
            "including input injection through shell",
            "Every reachable persistent-state class must be attested",
            "Unlisted, unknown or unattestable classes make the device class ineligible",
            "user/profile set itself",
            "user CA certificates and credential storage entries",
            "accounts, Wi-Fi and VPN profiles including per-network proxy",
            "screen-lock state",
            "DeviceConfig flags",
            "package enabled/suspended/hidden and component state",
            "preferred activities",
            "`/data/local/tmp`, shared storage and `run-as` data",
            "reviewed normalization rules",
            "No device class is qualified by this note",
        ):
            with self.subTest(clause=clause):
                self.assertTrue(clause in release, f"Missing attestation closure: {clause}")
        row = next(
            (
                line
                for line in self.note.splitlines()
                if line.startswith("| `device-side-persistence` |")
            ),
            "",
        )
        for mutation in ("new managed profile", "user CA", "per-network proxy", "/data/local/tmp"):
            with self.subTest(mutation=mutation):
                self.assertIn(mutation, row)


if __name__ == "__main__":
    unittest.main()
