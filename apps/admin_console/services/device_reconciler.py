"""One reconciler for device state, whatever the source (CHE-1477).

Contract: docs/device-identity.md ("Device state"), docs/board-operations.md (config keys).
Sources call ``heartbeat``; a periodic caller calls ``tick``. Time is always the server's
clock, never the sender's. State is ``unknown`` once no connection has reported within
``ARTEMIS_DEVICE_TTL_S``, never ``idle``.
"""

from dataclasses import dataclass, field
import logging
import math
import os
import threading
import time
from typing import Callable

from apps.admin_console.database.repositories.device_event_repository import (
    UNKNOWN,
    DeviceEventRepository,
)

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 60.0
TTL_RANGE_S = (10.0, 3600.0)
HYSTERESIS_S = 5.0
# What a source can report; running beats busy beats idle when connections disagree.
REPORTED = ("running", "busy", "idle", "disconnected")


def device_ttl_s() -> float:
    """``ARTEMIS_DEVICE_TTL_S``; a missing, non-numeric or out-of-range value uses the default."""
    raw = os.environ.get("ARTEMIS_DEVICE_TTL_S")
    if not raw:
        return DEFAULT_TTL_S
    try:
        ttl = float(raw)
    except ValueError:
        ttl = math.nan
    if not TTL_RANGE_S[0] <= ttl <= TTL_RANGE_S[1]:  # also false for nan
        logger.warning(
            "ARTEMIS_DEVICE_TTL_S=%r is not in %s; using %s", raw, TTL_RANGE_S, DEFAULT_TTL_S
        )
        return DEFAULT_TTL_S
    return ttl


@dataclass(slots=True)
class _Device:
    committed: str | None = None  # the last state written; None until first read
    pending: tuple[str, float] | None = None  # (state, since) waiting out the hysteresis
    # connection_id -> (reported state, server receive time)
    seen: dict[str, tuple[str, float]] = field(default_factory=dict)


class DeviceReconciler:
    def __init__(
        self,
        events: DeviceEventRepository,
        *,
        ttl_s: float | None = None,
        hysteresis_s: float = HYSTERESIS_S,
        clock: Callable[[], float] = time.time,
    ):
        self._events = events
        self._ttl = device_ttl_s() if ttl_s is None else ttl_s
        self._hold = hysteresis_s
        self._clock = clock
        self._devices: dict[str, _Device] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        """Write ``unknown`` for every device last recorded in a known state.

        Nobody watched while the server was down, so the timeline must not claim the last
        known state ran on until now.
        """
        now = self._clock()
        with self._lock:
            for device_id, state in self._events.latest_states().items():
                device = self._devices.setdefault(device_id, _Device())
                device.committed = state
                if state != UNKNOWN:
                    self._commit(device_id, device, UNKNOWN, now, reason="startup")

    def heartbeat(self, device_id: str, connection_id: str, state: str) -> None:
        if state not in REPORTED:
            raise ValueError(f"unknown device state {state!r}; expected one of {REPORTED}")
        now = self._clock()
        with self._lock:
            device = self._devices.setdefault(device_id, _Device())
            device.seen[connection_id] = (state, now)
            self._evaluate(device_id, device, now)

    def tick(self) -> None:
        """Re-evaluate every device: expire silent connections and settle held states."""
        now = self._clock()
        with self._lock:
            for device_id, device in self._devices.items():
                self._evaluate(device_id, device, now)

    def state(self, device_id: str) -> str:
        """The committed state; ``unknown`` for a device nobody has reported."""
        with self._lock:
            device = self._devices.get(device_id)
            return (device.committed if device else None) or UNKNOWN

    def _observed(self, device: _Device, now: float) -> tuple[str, float]:
        """``(state, since)`` right now: the live connections' best state, else unknown."""
        live = [state for state, at in device.seen.values() if now - at < self._ttl]
        if not live:
            last = max((at for _, at in device.seen.values()), default=now)
            return UNKNOWN, last + self._ttl  # unknown began when the TTL ran out
        known = [s for s in live if s != "disconnected"]
        return (min(known, key=REPORTED.index) if known else "disconnected"), now

    def _evaluate(self, device_id: str, device: _Device, now: float) -> None:
        if device.committed is None:
            device.committed = self._events.latest_state(device_id) or UNKNOWN
        state, since = self._observed(device, now)
        if state == device.committed:
            device.pending = None
            return
        if device.pending is None or device.pending[0] != state:
            device.pending = (state, since)
        if now - device.pending[1] >= self._hold:
            self._commit(device_id, device, *device.pending)
            device.pending = None

    def _commit(
        self, device_id: str, device: _Device, state: str, ts: float, *, reason: str | None = None
    ) -> None:
        self._events.record_state(device_id, state, ts, reason=reason)
        device.committed = state
