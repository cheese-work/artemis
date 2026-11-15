"""Fail-closed admission/release comparison for independently collected digests."""

import hashlib
import json

MINIMUM_CLASSES = frozenset(
    {
        "packages",
        "permissions",
        "appops",
        "accessibility",
        "notification_listeners",
        "device_admins",
        "input_methods",
        "roles",
        "settings_global",
        "settings_secure",
        "settings_system",
        "process_identities",
        "ca_credentials",
        "accounts",
        "wifi_vpn_proxy",
        "lock_state",
        "device_config",
        "package_component_state",
        "preferred_activities",
        "shell_storage",
        "shared_storage",
        "run_as_storage",
    }
)


def attest(profile, observed):
    """Return a protected baseline digest, or None. Never collect device state.

    Both arguments come from the trusted, authorized verifier, not the holder.
    The review reference attests reachability closure and normalization for this
    exact OS/build/service/grant/verifier profile. A minimum list is NOT closure.
    No default profile, post-lease learning, wildcard exclusion or reset exists.
    """
    try:
        pins = {"build", "services", "grant", "verifier"}
        if (
            set(profile) != pins | {"version", "review", "classes", "reachable"}
            or type(profile["version"]) is not int
            or profile["version"] < 1
            or not isinstance(profile["review"], str)
            or not profile["review"]
        ):
            return None
        classes = set(profile["classes"])
        if (
            not MINIMUM_CLASSES <= classes
            or classes != set(profile["reachable"])
            or any(not isinstance(c, str) or not c or "*" in c for c in classes)
        ):
            return None
        if set(observed) != pins | {"wireless_debugging", "tcp_adbd", "adb_keys", "users"}:
            return None
        if any(
            not isinstance(profile[k], str) or not profile[k] or profile[k] != observed[k]
            for k in pins
        ):
            return None
        if observed["wireless_debugging"] is not False or observed["tcp_adbd"] is not False:
            return None
        keys, users = observed["adb_keys"], observed["users"]
        if (
            not isinstance(keys, list)
            or not keys
            or len(set(keys)) != len(keys)
            or any(not isinstance(k, str) or not k for k in keys)
            or not isinstance(users, dict)
            or not users
        ):
            return None
        for user, state in users.items():
            if (
                not isinstance(user, str)
                or not user
                or set(state) != classes
                or any(not isinstance(v, str) or not v for v in state.values())
            ):
                return None
        return hashlib.sha256(
            json.dumps([profile, observed], sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
    except (TypeError, ValueError, KeyError):
        return None


def verified(profile, baseline, observed):
    """Unexplained state/profile/key differences cannot clear quarantine."""
    before, after = attest(profile, baseline), attest(profile, observed)
    return before is not None and before == after
