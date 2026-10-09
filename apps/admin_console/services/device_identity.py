"""Match every connection to a device by hardware identity (CHE-1473).

Contract: docs/device-identity.md, "Matching rules". The hardware identity of a
phone is HMAC-SHA256(org device pepper, ro.serialno): the host agent sends it as
``hardware_id``; for server adb and the browser bridge the server reads
``ro.serialno`` and hashes it with the same pepper. An AVD is ``(host_id, AVD
name)``. Hooks: host registration (``host_registry.set_devices``) and server adb
discovery, which also lists bridge phones (``device_pool.identity_observer``).
Nothing schedules from the outcome yet (CHE-1475).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import sqlite3
import threading
from typing import Any

from apps.admin_console.database.repositories.device_repository import (
    DeviceRepository,
    DeviceStoreNotReady,
    Match,
)
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.utils.device_kind import DeviceKind, classify_properties

logger = logging.getLogger(__name__)

HARDWARE_ID = re.compile(r"^[0-9a-f]{64}$")
# ro.serialno values many phones share; host-agent/identity.go ignores the same ones.
PLACEHOLDER_SERIALS = frozenset(
    {"", "unknown", "0123456789abcdef", "0123456789", "0000000000000000"}
)


def _registry():
    from apps.admin_console.services.host_registry import host_registry

    return host_registry


def _bridge_serials() -> set[str]:
    from apps.admin_console.services.bridge_session_service import bridge_session_service

    return {session.serial for session in bridge_session_service.live_sessions()}


class DeviceIdentity:
    def __init__(self) -> None:
        # Last observation per connection key: discovery repeats every few seconds, and
        # only a change needs a write. Heartbeats belong to the reconciler (CHE-1477).
        self._seen: dict[tuple, tuple[tuple, Match]] = {}
        self._peppers: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def hardware_hash(self, serialno: str | None) -> str | None:
        """HMAC-SHA256(org pepper, ro.serialno) as hex; None for a missing or placeholder value."""
        value = (serialno or "").strip()
        if value.lower() in PLACEHOLDER_SERIALS:
            return None
        return self._mac(value)

    def avd_hash(self, host_id: str | None, avd: str) -> str:
        """The AVD is the device: one identity per ``(host_id, AVD name)``."""
        return self._mac(f"avd\0{host_id or ''}\0{avd}")

    def _mac(self, message: str) -> str:
        registry = _registry()
        key = str(registry.db_path)
        with self._lock:
            pepper = self._peppers.get(key)
        if pepper is None:
            pepper = base64.b64decode(registry.device_pepper())
            with self._lock:
                self._peppers[key] = pepper
        return hmac.new(pepper, message.encode(), hashlib.sha256).hexdigest()

    def observe_adb(
        self, endpoint: AdbEndpoint, devices: list[tuple[str, str, str | None, dict[str, str]]]
    ) -> list[Match]:
        """Server adb discovery: ``(serial, state, model, getprop)`` per listed device."""
        if endpoint.host_id is not None:
            return []  # a host tunnel lists the agent's phones; observe_host owns them
        # ponytail: every server adb endpoint shares source "local"; key by endpoint if two are ever live.
        bridged = _bridge_serials()
        observed = []
        for serial, state, model, props in devices:
            readable = state == "device" and bool(props)
            hardware = None
            if serial in bridged:
                kind, source = "bridge", "bridge"
            elif classify_properties(props) is DeviceKind.EMULATOR or serial.startswith(
                "emulator-"
            ):
                kind, source = "avd", "local"
                avd = props.get("ro.boot.qemu.avd_name") or props.get("ro.kernel.qemu.avd_name")
                hardware = self.avd_hash(None, avd) if readable and avd else None
            else:
                wifi = ":" in serial or "._adb" in serial
                kind, source = ("wifi" if wifi else "usb"), "local"
            if hardware is None and readable and kind != "avd":
                hardware = self.hardware_hash(props.get("ro.serialno"))
            observed.append(
                dict(
                    kind=kind,
                    source=source,
                    host_id=None,
                    serial=serial,
                    hardware_hash=hardware,
                    readable=readable,
                    label=model,
                )
            )
        return self._match_all(observed)

    def observe_host(self, host_id: str, devices: list[dict[str, Any]]) -> list[Match]:
        """Host registration: the agent's validated device rows plus its ``hardware_id``."""
        observed = []
        for item in devices:
            serial, state = item["serial"], item.get("state")
            readable = state in (None, "device")  # agents before B3a-2 send no state
            emulator = item.get("kind") == "emulator"
            hardware = None
            if readable and emulator and not item.get("attention"):
                # The agent's emulator id is an HMAC of its AVD name: stable across instances.
                hardware = self.avd_hash(host_id, serial)
            elif (
                readable and not emulator and HARDWARE_ID.match(str(item.get("hardware_id") or ""))
            ):
                hardware = item["hardware_id"]
            # The agent publishes one entry per phone; its transport is not on the wire.
            observed.append(
                dict(
                    kind="avd" if emulator else "usb",
                    source="host",
                    host_id=host_id,
                    serial=serial,
                    hardware_hash=hardware,
                    readable=readable,
                    label=item.get("model"),
                )
            )
        return self._match_all(observed)

    def _match_all(self, observed: list[dict[str, Any]]) -> list[Match]:
        """Never breaks the caller: a failing store logs and leaves the rest unmatched."""
        db_path = _registry().db_path
        repo = DeviceRepository(db_path)
        matches = []
        try:
            for item in observed:
                key = (str(db_path), item["source"], item["host_id"], item["serial"])
                fingerprint = (item["kind"], item["hardware_hash"], item["readable"])
                with self._lock:
                    seen = self._seen.get(key)
                if seen is None or seen[0] != fingerprint:
                    seen = (fingerprint, repo.match_connection(**item))
                    with self._lock:
                        self._seen[key] = seen
                matches.append(seen[1])
        except DeviceStoreNotReady:
            logger.debug("Device identity skipped: the device tables are not ready")
        except sqlite3.Error:
            logger.exception("Device identity resolution failed")
        return matches


device_identity = DeviceIdentity()
