"""Merge and Keep separate, serialized against queue admission (CHE-1480; docs/device-identity.md).

Both take the reservation lock, so a decision and a claim never interleave on one phone.
"""

import time

from apps.admin_console.database.repositories.device_repository import Device, DeviceRepository
from apps.admin_console.services.device_reservation import device_reservations


def merge(
    repo: DeviceRepository,
    device_id: str,
    other_device_id: str,
    *,
    actor_principal_id: str,
    now: float | None = None,
) -> Device:
    """``other_device_id`` joins the survivor ``device_id``; refused with ``device_claimed`` while either is claimed."""
    with device_reservations.serialized(repo, device_id, other_device_id):
        return repo.merge(
            device_id,
            other_device_id,
            actor_principal_id=actor_principal_id,
            now=now or time.time(),
        )


def keep_separate(
    repo: DeviceRepository,
    device_id: str,
    other_device_id: str,
    *,
    actor_principal_id: str,
    now: float | None = None,
) -> None:
    with device_reservations.serialized(repo, device_id, other_device_id):
        repo.keep_separate(
            device_id,
            other_device_id,
            actor_principal_id=actor_principal_id,
            now=now or time.time(),
        )
