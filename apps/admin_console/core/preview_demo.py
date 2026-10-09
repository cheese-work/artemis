"""Device-free demo board fixture for the isolated preview (CHE-1373).

Opt-in (``ARTEMIS_PREVIEW_DEMO=1``, see ``docs/preview-fixtures.md``): on top of
the nine base runs it seeds 20 labelled fake devices, runs for the busy ones,
recent failures, one interrupted queue item and playable evidence, all stamped
relative to boot time. Nothing here touches a device, process or network.

Writable notes and the uncertain-match device need the annotation store and the
device identity record that later CHE-1332 layers add. Those layers register a
function in ``SEED_HOOKS``; it runs last with the :class:`DemoSeed` context.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
from typing import Any
import uuid

from apps.admin_console.core.ownership import OwnerScope

CLIP = Path(__file__).with_name("preview_demo_clip.mp4")
STATES = ("idle", "private", "busy", "disconnected", "unknown")


@dataclass(frozen=True)
class DemoDevice:
    number: int
    name: str
    state: str  # one of STATES
    serials: tuple[str, ...]  # more than one: the same phone on USB and Wi-Fi
    owner: str | None = None  # email; set only for a private device

    @property
    def label(self) -> str:
        return f"Demo {self.number:02d} {self.name}"


@dataclass(frozen=True)
class DemoSeed:
    """What a seeding hook receives; hooks return extra queue items or nothing."""

    root: Path
    owners: tuple[str, str, str]  # qa-a, qa-b, admin emails
    now: float
    devices: tuple[DemoDevice, ...]
    storage: Any
    catalog: Any
    session_ids: dict[str, str] = field(default_factory=dict)


SeedHook = Callable[[DemoSeed], list[dict] | None]
SEED_HOOKS: list[SeedHook] = []

_devices: tuple[DemoDevice, ...] = ()
_busy: dict[
    str, tuple[str, str, float, str]
] = {}  # serial -> (session id, goal, acquired_at, owner)


def demo_devices(qa_a: str, qa_b: str) -> tuple[DemoDevice, ...]:
    shared = ("Pixel 8", "Pixel 7a", "Galaxy S23", "Galaxy S22", "OnePlus 11", "Moto G Power")
    private = (
        ("Pixel 6", qa_a), ("Pixel 6a", qa_a), ("Galaxy A54", qa_a),
        ("Pixel 5", qa_b), ("Galaxy A34", qa_b),
    )  # fmt: skip
    busy = ("Pixel 8 Pro", "Pixel Fold", "Galaxy Z Flip", "Xiaomi 13")
    offline = ("Pixel 4a", "Galaxy S21", "Nokia G60")
    unknown = ("Pixel 3a", "Redmi Note 12")
    specs = [(n, "idle", None) for n in shared]
    specs += [(n, "private", o) for n, o in private]
    specs += [(n, "busy", None) for n in busy]
    specs += [(n, "disconnected", None) for n in offline]
    specs += [(n, "unknown", None) for n in unknown]
    devices = []
    for number, (name, state, owner) in enumerate(specs, start=1):
        serials = (f"DEMO{number:04d}",)
        if number == 1:
            serials = (f"DEMOUSB{number:02d}", "192.168.50.21:5555")
        elif state == "private":  # bridge-style address of a browser phone
            serials = (f"127.0.0.1:{5500 + number}",)
        devices.append(DemoDevice(number, name, state, serials, owner))
    return tuple(devices)


def _session_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"artemis-preview-demo/{key}"))


def seed_demo_board(root: Path, owners: tuple[str, str, str], now: float, storage, catalog):
    """Seed the demo runs and return their queue items."""
    from artemis.data_engine.models import SessionMetadata
    from artemis.runtime.lifecycle import ensure_lifecycle_schema

    global _devices
    devices = demo_devices(owners[0], owners[1])
    queue: list[dict] = []
    ids: dict[str, str] = {}

    def add(key, owner, goal, status, started, ended=None, device=None) -> str:
        session_id = ids[key] = _session_id(key)
        storage.create_session(
            SessionMetadata(
                session_id=session_id,
                initial_goal=goal,
                start_time=now - started,
                end_time=None if ended is None else now - ended,
                status=status,
                device_info={"profile": "synthetic-preview", "device_id": device},
            )
        )
        if not catalog.set_meta(session_id, requested_by=owner):
            raise ValueError("Demo fixture ownership could not be recorded.")
        return session_id

    _busy.clear()
    for index, device in enumerate(d for d in devices if d.state == "busy"):
        serial, owner = device.serials[0], owners[(0, 1, 2, 0)[index]]
        goal = f"Synthetic demo: run on {device.label}"
        session_id = add(f"busy/{index}", owner, goal, "running", 600 + index * 60, None, serial)
        _busy[serial] = (session_id, goal, now - 600 - index * 60, owner)
        queue.append(_item(session_id, goal, "running", owner, serial))
    broken = [d for d in devices if d.state in ("disconnected", "unknown")]
    for index, device in enumerate(broken[:4]):
        goal = f"Synthetic demo: failed run on {device.label}"
        add(f"failed/{index}", owners[index % 3], goal, "failed", 7200 + index * 900,
            7000 + index * 900, device.serials[0])  # fmt: skip
    goal = "Synthetic demo: interrupted by a device that went offline"
    session_id = add(
        "interrupted", owners[0], goal, "interrupted", 1800, 1700, broken[0].serials[0]
    )
    with sqlite3.connect(storage.db_path) as conn:
        if ensure_lifecycle_schema(conn):
            conn.execute(
                "UPDATE sessions SET interrupt_reason = 'device_offline' WHERE session_id = ?",
                (session_id,),
            )
    queue.append(_item(session_id, goal, "interrupted", owners[0], broken[0].serials[0]))
    for index in range(3):
        goal = f"Synthetic demo: completed run with a recording ({index + 1})"
        session_id = add(f"evidence/{index}", owners[index], goal, "completed",
                         3600 + index * 300, 3570 + index * 300)  # fmt: skip
        (storage.base_trace_dir / session_id).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CLIP, storage.base_trace_dir / session_id / "recording.mp4")

    _devices = devices
    seed = DemoSeed(root, owners, now, devices, storage, catalog, ids)
    for hook in SEED_HOOKS:
        queue += hook(seed) or []
    return queue


def _item(session_id: str, goal: str, status: str, owner: str, device_id: str) -> dict:
    return {
        "session_id": session_id,
        "goal": goal,
        "status": status,
        "requested_by": owner,
        "device_id": device_id,
    }


def visible_device_rows(scope: OwnerScope) -> list[dict[str, Any]]:
    """``/api/devices`` rows (one per connection).

    A private device shows to its owner and admin; a busy row shows the run only to its owner and admin.
    """
    from artemis.runtime.device_pool import DeviceStatus

    rows = []
    for device in _devices:
        if device.state == "private" and not scope.may_act_on(device.owner):
            continue
        for serial in device.serials:
            active = _busy.get(serial)
            # Busy stays visible to all; the run behind it follows run visibility.
            run = active if active and scope.may_act_on(active[3]) else None
            rows.append(
                DeviceStatus(
                    serial=serial,
                    state={"disconnected": "offline", "unknown": "unknown"}.get(
                        device.state, "device"
                    ),
                    model=device.label,
                    product=f"demo-{device.state}",
                    device_kind="phone",
                    is_busy=active is not None,
                    active_task_desc=run[1] if run else None,
                    active_session_id=run[0] if run else None,
                    acquired_at=datetime.fromtimestamp(active[2]).isoformat() if active else None,
                ).to_dict()
            )
    return rows
