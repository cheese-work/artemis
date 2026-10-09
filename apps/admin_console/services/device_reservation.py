from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.database.repositories.device_repository import (
    DeviceRepository,
    DeviceStoreNotReady,
)
from apps.admin_console.services.device_identity import device_identity
from artemis.runtime.run_device_binding import RunDeviceBinding


@dataclass(frozen=True)
class ReservationSnapshot:
    key: str
    connection_id: str | None
    outcome: str | None


class DeviceReservations:
    def __init__(self):
        self._lock = threading.Lock()
        self._claims: dict[str, tuple[str, str, RunDeviceBinding, ReservationSnapshot]] = {}

    def snapshot(
        self,
        binding: RunDeviceBinding,
        db_path: Path | None,
        *,
        connection_id: str | None = None,
    ) -> ReservationSnapshot:
        target = binding.target
        try:
            repo = DeviceRepository(db_path)
            if connection_id:
                match = repo.connection(connection_id=connection_id)
            else:
                source = (
                    "host" if target.host_id else "bridge" if binding.bridge_session_id else "local"
                )
                serial = (
                    target.serial
                    if target.host_id
                    else device_identity.transport_key(target.endpoint, target.serial)
                )
                match = repo.connection(source=source, host_id=target.host_id, serial=serial)
        except (DeviceStoreNotReady, sqlite3.Error, ValueError) as exc:
            raise AdminAPIError(
                503,
                "Device identity is unavailable.",
                "device_store_not_ready",
                "Try again shortly.",
            ) from exc
        if match is None:
            return ReservationSnapshot(f"connection:{target.lock_key}", None, None)
        key = (
            "uncertain"
            if match.outcome == "uncertain"
            else f"device:{match.device_id}"
            if match.outcome == "confirmed"
            else f"connection:{target.lock_key}"
        )
        return ReservationSnapshot(key, match.connection_id, match.outcome)

    def claim(
        self,
        binding: RunDeviceBinding,
        db_path: Path | None,
        session_id: str,
        group_id: str | None = None,
    ) -> ReservationSnapshot:
        group_id = group_id or session_id
        database = str(db_path)
        with self._lock:
            snapshot = self.snapshot(binding, db_path)
            if snapshot.outcome in {"uncertain", "provisional"}:
                raise AdminAPIError(
                    409,
                    "The phone's identity is unresolved.",
                    "device_identity_unresolved",
                    "Confirm the phone's identity before starting a run.",
                )
            for claimed_database, claimed_group, claimed_binding, claimed in self._claims.values():
                if claimed_database != database or claimed_group == group_id:
                    continue
                current = self.snapshot(
                    claimed_binding, db_path, connection_id=claimed.connection_id
                )
                if current.key == snapshot.key or claimed.key == snapshot.key:
                    raise AdminAPIError(
                        409,
                        "The phone is already reserved for a run.",
                        "device_claimed",
                        "Wait for the run to finish or cancel the run.",
                    )
            self._claims[session_id] = (database, group_id, binding, snapshot)
            return snapshot

    def release(self, session_id: str | None) -> None:
        with self._lock:
            self._claims.pop(str(session_id), None)


device_reservations = DeviceReservations()
