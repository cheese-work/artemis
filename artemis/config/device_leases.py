"""Feature flag for the device lease API (CHE-2099). Off unless explicitly enabled.

Accepted spellings, case-insensitive: ``enabled``, ``1``, ``true``, ``yes``, ``on``;
anything else, or unset, is off. Read from the environment on every call, never cached.
"""

from __future__ import annotations

import os

ENV_DEVICE_LEASES = "ARTEMIS_DEVICE_LEASES"
_TRUTHY = frozenset({"enabled", "1", "true", "yes", "on"})


def device_leases_enabled() -> bool:
    return os.environ.get(ENV_DEVICE_LEASES, "").strip().lower() in _TRUTHY
