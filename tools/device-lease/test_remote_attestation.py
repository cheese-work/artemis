"""Fake attestation only. Does not inspect device data or qualify a device class."""

import copy
import unittest

from remote_attestation import MINIMUM_CLASSES, attest, verified


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.profile = dict(
            version=1,
            review="independent-exact-profile-security-PASS",
            build="os-build-digest",
            services="r1-service-digest",
            grant="privacy-grant-digest",
            verifier="verifier-implementation-and-normalization-digest",
            classes=sorted(MINIMUM_CLASSES),
            reachable=sorted(MINIMUM_CLASSES),
        )
        self.baseline = dict(
            build="os-build-digest",
            services="r1-service-digest",
            grant="privacy-grant-digest",
            verifier="verifier-implementation-and-normalization-digest",
            wireless_debugging=False,
            tcp_adbd=False,
            adb_keys=["fingerprint-digest"],
            users={"0": {name: "protected-normalized-state-digest" for name in MINIMUM_CLASSES}},
        )

    def test_clean_authorized_normalized_state_is_accepted(self):
        self.assertIsNotNone(attest(self.profile, self.baseline))
        self.assertTrue(verified(self.profile, self.baseline, copy.deepcopy(self.baseline)))

    def test_every_persistent_state_difference_refuses_reuse(self):
        self.assertTrue(verified(self.profile, self.baseline, self.baseline))
        for category in MINIMUM_CLASSES:
            after = copy.deepcopy(self.baseline)
            after["users"]["0"][category] = "unexplained-change"
            self.assertFalse(verified(self.profile, self.baseline, after), category)

    def test_added_removed_or_hidden_profile_refuses_reuse(self):
        self.assertTrue(verified(self.profile, self.baseline, self.baseline))
        for users in (
            {},
            {"0": self.baseline["users"]["0"], "hidden-work-profile": self.baseline["users"]["0"]},
        ):
            after = dict(self.baseline, users=users)
            self.assertFalse(verified(self.profile, self.baseline, after))

    def test_network_adbd_pairing_and_unavailable_evidence_refuse_reuse(self):
        self.assertTrue(verified(self.profile, self.baseline, self.baseline))
        for name, value in (
            ("wireless_debugging", True),
            ("tcp_adbd", True),
            ("adb_keys", ["new-key"]),
            ("tcp_adbd", None),
        ):
            self.assertFalse(
                verified(self.profile, self.baseline, dict(self.baseline, **{name: value}))
            )
        self.assertFalse(verified(self.profile, self.baseline, None))

    def test_incomplete_stale_unreviewed_and_unlisted_classes_deny_admission(self):
        self.assertIsNotNone(attest(self.profile, self.baseline))
        for name in ("review", "build", "services", "grant", "verifier"):
            self.assertIsNone(attest(dict(self.profile, **{name: ""}), self.baseline), name)
        self.assertIsNone(attest(dict(self.profile, classes=["packages"]), self.baseline))
        self.assertIsNone(
            attest(
                dict(self.profile, reachable=self.profile["reachable"] + ["unknown"]), self.baseline
            )
        )
        self.assertIsNone(attest(self.profile, dict(self.baseline, build="new-build")))

    def test_normalization_cannot_hide_an_unknown_persistent_state_class(self):
        self.assertIsNotNone(attest(self.profile, self.baseline))
        observed = copy.deepcopy(self.baseline)
        observed["users"]["0"]["holder-selected-wildcard-exclusion"] = "ignored"
        self.assertIsNone(attest(self.profile, observed))
        self.assertFalse(verified(self.profile, self.baseline, observed))


if __name__ == "__main__":
    unittest.main()
