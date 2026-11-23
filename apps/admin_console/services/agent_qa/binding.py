"""Server-owned SmartQA target and APK resolution."""

import os
from pathlib import Path
import re
import sqlite3
from subprocess import SubprocessError

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.core.config import TEST_DATA_DIR
from apps.admin_console.database.repositories.device_repository import (
    DeviceRepository,
    DeviceStoreNotReady,
)
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.routers.leases import _pools, _restricted
from apps.admin_console.routers.smartqa_check import _enabled
from apps.admin_console.schemas.task_schema import SmartQAOptions
from apps.admin_console.services import device_leases
from apps.admin_console.services.device_identity import device_identity
from artemis.config.device_leases import device_leases_enabled
from artemis.runtime import device_policy
from artemis.runtime.endpoint_transport import EndpointTransport


SERIAL = re.compile(r"emulator-[0-9]+")


def stored_apk(options: SmartQAOptions) -> Path:
    root = Path(os.getenv("ARTEMIS_SMARTQA_APK_DIR") or TEST_DATA_DIR / "smartqa_apks")
    path = root / f"{options.apk_sha256}.apk"
    if path.is_symlink() or not path.is_file():
        raise AdminAPIError(
            422, "SmartQA APK not found.", "smartqa_apk_missing", "Upload the APK first."
        )
    return path.resolve()


def _refused() -> AdminAPIError:
    return AdminAPIError(
        409,
        "SmartQA requires your active lease on the dedicated local emulator.",
        "smartqa_device_not_dedicated",
        "Acquire an unrestricted lease in the x99-avd pool.",
    )


def verify_device(
    options: SmartQAOptions,
    holder: str,
    transport: EndpointTransport,
    serial: str | None = None,
) -> str:
    if not (_enabled() and device_leases_enabled()):
        raise AdminAPIError(
            404, "SmartQA is unavailable.", "smartqa_disabled", "Use a SmartQA server."
        )
    if not transport.endpoint.is_local_default or (serial and not SERIAL.fullmatch(serial)):
        raise _refused()
    db_path = run_catalog_repo.db_path
    repo = DeviceRepository(db_path)
    try:
        lease = next(
            (row for row in device_leases.active(db_path) if row.lease_id == options.lease_id), None
        )
        if not lease or lease.holder != holder or lease.restricted:
            raise _refused()
        device = repo.get(lease.device_id)
        pool = {repo.resolve(device_id) for device_id in _pools().get("x99-avd", [])}
        restricted = _restricted()
        if (
            not device
            or not device.hardware_hash
            or device.match_state != "confirmed"
            or device.device_id not in pool
            or device.device_id in {repo.resolve(item) for item in restricted}
        ):
            raise _refused()
        if serial is None:
            listing = transport.run(["devices"], capture_output=True, timeout=5, check=True)
            candidates = [
                parts[0]
                for line in listing.stdout.decode().splitlines()
                if len(parts := line.split()) == 2
                and parts[1] == "device"
                and SERIAL.fullmatch(parts[0])
            ]
        else:
            candidates = [serial]
        bound = []
        for candidate in candidates:
            if candidate in restricted or device_policy.is_restricted(candidate):
                continue
            match = repo.connection(
                source="local",
                host_id=None,
                serial=device_identity.transport_key(transport.endpoint, candidate),
            )
            if match and match.device_id == device.device_id and match.outcome == "confirmed":
                bound.append(candidate)
        if len(bound) != 1:
            raise _refused()
        target = bound[0]
        if serial is not None:
            name = ""
            for prop in ("ro.boot.qemu.avd_name", "ro.kernel.qemu.avd_name"):
                result = transport.run(
                    ["-s", target, "shell", "getprop", prop],
                    capture_output=True,
                    timeout=5,
                    check=True,
                )
                name = result.stdout.decode().strip()
                if name:
                    break
            if (
                not name
                or device_identity.avd_hash(transport.endpoint.identity, name)
                != device.hardware_hash
            ):
                raise _refused()
        return target
    except (DeviceStoreNotReady, OSError, sqlite3.Error, SubprocessError, ValueError) as exc:
        raise _refused() from exc
