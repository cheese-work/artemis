"""Host admission and the maintenance barrier (CHE-1128, behind ARTEMIS_HOST_AGENT).

The host agent is authoritative for its own state (``active``, ``draining``,
``maintenance``, ``update_required``) and reports it by heartbeat. The
dispatcher asks :meth:`HostAdmission.try_reserve` once per otherwise-eligible
queue row; a host that is not ``active``, has no free run slot, or presents a
device id another computer also presents gets no new start.

This is deliberately separate from the server-wide deploy drain
(``state.draining``, ``active_run_count``): that count includes waiting rows and
only closes submission, so it cannot fence dispatch for one host.

One lock covers state, capacity and device claims, so a barrier request and a
slot reservation are totally ordered: a start is either already reserved (and
counted in the barrier acknowledgement) or refused.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
import os
import threading
from typing import Any

ENV_FLAG = "ARTEMIS_HOST_AGENT"
# 32 streams = 4 shared pre-selection + 28 device streams, 4 per run (accepted plan A3).
DEFAULT_MAX_RUNS = 7


def enabled() -> bool:
    return os.getenv(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


class HostState(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    MAINTENANCE = "maintenance"
    UPDATE_REQUIRED = "update_required"
    OFFLINE = "offline"


# Everything except ACTIVE and OFFLINE is a barrier the agent raised itself.
_BARRIERS = frozenset({HostState.DRAINING, HostState.MAINTENANCE, HostState.UPDATE_REQUIRED})


class WaitReason(StrEnum):
    HOST_OFFLINE = "host_offline"
    HOST_DRAINING = "host_draining"
    HOST_MAINTENANCE = "host_maintenance"
    HOST_UPDATE_REQUIRED = "host_update_required"
    HOST_FULL = "host_full"
    DEVICE_NOT_SHARED = "device_not_shared"
    DEVICE_NEEDS_ATTENTION = "device_needs_attention"


_STATE_REASON = {
    HostState.OFFLINE: WaitReason.HOST_OFFLINE,
    HostState.DRAINING: WaitReason.HOST_DRAINING,
    HostState.MAINTENANCE: WaitReason.HOST_MAINTENANCE,
    HostState.UPDATE_REQUIRED: WaitReason.HOST_UPDATE_REQUIRED,
}


class RunPhase(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    CLEANING_UP = "cleaning_up"


@dataclass(frozen=True, slots=True)
class BarrierAck:
    """What is still in flight on the host once dispatch to it is fenced."""

    host_id: str
    state: HostState
    starting: int
    running: int
    cleaning_up: int

    @property
    def quiescent(self) -> bool:
        return not (self.starting or self.running or self.cleaning_up)


@dataclass(slots=True)
class _Host:
    state: HostState = HostState.OFFLINE  # unknown until the agent reports
    max_runs: int = DEFAULT_MAX_RUNS
    devices: set[str] = field(default_factory=set)


class HostAdmission:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, _Host] = {}
        self._runs: dict[str, tuple[str, RunPhase]] = {}  # session id -> (host id, phase)
        self._different: set[str] = set()  # device ids an admin declared distinct phones

    def reset(self) -> None:
        with self._lock:
            self._hosts.clear()
            self._runs.clear()
            self._different.clear()

    # -- state, reported by the agent -------------------------------------

    def _host(self, host_id: str) -> _Host:
        return self._hosts.setdefault(host_id, _Host())

    def heartbeat(self, host_id: str, state: HostState, max_runs: int | None = None) -> None:
        with self._lock:
            host = self._host(host_id)
            host.state = HostState(state)
            if max_runs is not None:
                host.max_runs = max(0, int(max_runs))

    def disconnect(self, host_id: str) -> None:
        """Link lost: offline until the agent re-reports; reserved runs stay counted."""
        self.heartbeat(host_id, HostState.OFFLINE)

    def cancel_update(self, host_id: str) -> None:
        """Clear a barrier; an offline host stays offline until it reports."""
        with self._lock:
            host = self._host(host_id)
            if host.state in _BARRIERS:
                host.state = HostState.ACTIVE

    def request_barrier(self, host_id: str, state: HostState = HostState.MAINTENANCE) -> BarrierAck:
        """Fence dispatch to the host and report what is still running there."""
        if state not in _BARRIERS:
            raise ValueError(f"{state} is not a barrier state")
        with self._lock:
            self._host(host_id).state = state
            return self._ack(host_id)

    def state_of(self, host_id: str) -> HostState:
        with self._lock:
            return self._host(host_id).state

    # -- devices ------------------------------------------------------------

    def share_device(self, host_id: str, device_id: str) -> None:
        with self._lock:
            self._host(host_id).devices.add(device_id)

    def unshare_device(self, host_id: str, device_id: str) -> None:
        """New starts stop at once; a run already reserved is left to finish."""
        with self._lock:
            self._host(host_id).devices.discard(device_id)

    def _claimants(self, device_id: str) -> list[str]:
        return [hid for hid, host in self._hosts.items() if device_id in host.devices]

    def needs_attention(self, device_id: str) -> list[str]:
        """Hosts whose rows show "Needs attention": one id seen from two computers."""
        with self._lock:
            hosts = self._claimants(device_id)
            return hosts if len(hosts) > 1 and device_id not in self._different else []

    def use_this_computer(self, device_id: str, host_id: str) -> None:
        """Admin: the phone is on this computer; every other claim is dropped."""
        with self._lock:
            for hid, host in self._hosts.items():
                if hid != host_id:
                    host.devices.discard(device_id)
            self._host(host_id).devices.add(device_id)

    def different_phones(self, device_id: str) -> None:
        """Admin: same id, different phones; lock keys already differ by host."""
        with self._lock:
            self._different.add(device_id)

    # -- admission ------------------------------------------------------------

    def try_reserve(self, host_id: str, session_id: str, device_id: str) -> WaitReason | None:
        """Take a run slot, or return why the row must keep waiting.

        Idempotent per session, so a retried dispatch never double-counts.
        """
        with self._lock:
            host = self._host(host_id)
            if host.state is not HostState.ACTIVE:
                return _STATE_REASON[host.state]
            if device_id not in host.devices:
                return WaitReason.DEVICE_NOT_SHARED
            claimants = self._claimants(device_id)
            if len(claimants) > 1 and device_id not in self._different:
                return WaitReason.DEVICE_NEEDS_ATTENTION
            if session_id not in self._runs:
                if self._count(host_id) >= host.max_runs:
                    return WaitReason.HOST_FULL
                self._runs[session_id] = (host_id, RunPhase.STARTING)
            return None

    def admit(self, item: dict[str, Any]) -> WaitReason | None:
        """The dispatcher hook: local rows and a disabled flag always pass."""
        host_id = item.get("host_id")
        if not host_id or not enabled():
            return None
        return self.try_reserve(
            str(host_id), str(item.get("session_id")), str(item.get("device_serial"))
        )

    def mark(self, session_id: str, phase: RunPhase) -> None:
        with self._lock:
            run = self._runs.get(session_id)
            if run is not None:
                self._runs[session_id] = (run[0], phase)

    def release(self, session_id: str) -> None:
        with self._lock:
            self._runs.pop(session_id, None)

    # -- counts -----------------------------------------------------------------

    def _count(self, host_id: str, phase: RunPhase | None = None) -> int:
        return sum(
            1 for hid, p in self._runs.values() if hid == host_id and (phase is None or p == phase)
        )

    def _ack(self, host_id: str) -> BarrierAck:
        return BarrierAck(
            host_id=host_id,
            state=self._host(host_id).state,
            starting=self._count(host_id, RunPhase.STARTING),
            running=self._count(host_id, RunPhase.RUNNING),
            cleaning_up=self._count(host_id, RunPhase.CLEANING_UP),
        )

    def snapshot(self, host_id: str, queue_items: Iterable[Any]) -> dict[str, Any]:
        """Starting, running and cleaning-up runs, counted apart from waiting rows."""
        with self._lock:
            ack = self._ack(host_id)
            max_runs = self._host(host_id).max_runs
        waiting = sum(
            1
            for item in queue_items
            if isinstance(item, dict)
            and item.get("status") == "pending"
            and item.get("host_id") == host_id
        )
        return {
            "host_id": host_id,
            "state": ack.state.value,
            "max_runs": max_runs,
            "waiting": waiting,
            "starting": ack.starting,
            "running": ack.running,
            "cleaning_up": ack.cleaning_up,
        }


host_admission = HostAdmission()
