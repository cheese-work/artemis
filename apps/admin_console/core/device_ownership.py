"""Browser-phone ownership: who may see and run on a phone (CHE-1150, slice O3).

A browser phone is a bridge address (``127.0.0.1:<port>``) leased to the person
who connected it; the bridge session records their verified email. Every other
device (an X99 emulator, a computer's shared phone) is shared and visible to
all. Open mode never filters, an admin sees and uses every phone, and a bridge
address with no live session, or a session with no owner, is admin-only: a
missing owner is not a matching one.
"""

from __future__ import annotations

import re
from typing import Any

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.core.ownership import OwnerScope

try:
    from admin_console.services.bridge_session_service import bridge_session_service
except ImportError:
    from apps.admin_console.services.bridge_session_service import bridge_session_service

_BRIDGE_ADDRESS = re.compile(r"(?:127\.0\.0\.1|localhost):\d+")
_HIDDEN = "another person's phone"


def may_use_device(scope: OwnerScope, serial: str) -> bool:
    if not _BRIDGE_ADDRESS.fullmatch(serial):
        return True
    return scope.may_act_on(bridge_session_service.owner_of(serial))


def require_device(scope: OwnerScope, serial: str) -> None:
    """Raise 403 unless the caller may run on ``serial``; nothing is touched."""
    if not may_use_device(scope, serial):
        raise AdminAPIError(
            403,
            "This phone belongs to someone else.",
            "device_not_yours",
            "Pick your own phone or a shared device.",
        )


def own_default_serial(scope: OwnerScope) -> str | None:
    """With no phone named, a signed-in caller's newest browser phone is the target."""
    return bridge_session_service.newest_serial_of(scope.email) if scope.enforced else None


def visible_devices(scope: OwnerScope, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in devices if may_use_device(scope, str(d.get("serial", "")))]


def _foreign_entry(scope: OwnerScope, node: Any) -> bool:
    serial = node.get("serial") if isinstance(node, dict) else None
    return isinstance(serial, str) and not may_use_device(scope, serial)


def _scrub(scope: OwnerScope, node: Any) -> Any:
    if isinstance(node, str):
        return _BRIDGE_ADDRESS.sub(
            lambda m: m.group(0) if may_use_device(scope, m.group(0)) else _HIDDEN, node
        )
    if isinstance(node, list):
        return [_scrub(scope, item) for item in node if not _foreign_entry(scope, item)]
    if not isinstance(node, dict):
        return node
    out = {
        key: None
        if key == "active_device" and _foreign_entry(scope, value)
        else _scrub(scope, value)
        for key, value in node.items()
    }
    if isinstance(out.get("devices"), list) and "device_count" in out:
        out["device_count"] = len(out["devices"])
    return out


def hide_foreign_devices(scope: OwnerScope, report: dict[str, Any]) -> dict[str, Any]:
    """The readiness report minus phones, active device and addresses the caller may not see."""
    return report if not scope.enforced or scope.admin else _scrub(scope, report)
