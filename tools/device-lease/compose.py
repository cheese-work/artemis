"""Owner composition for the live `serve` path (CHE-5297). Standard library only.

A trusted, owner-only JSON file names every admission and recovery input. Any
missing or invalid input refuses startup; there are no empty readers, no default
guard and no TTL-only release. File I/O runs before SQLite's writer lock and
inside the shared claim guard, as the broker requires.
"""

import fcntl
import importlib
import json
import os
from pathlib import Path
import stat
import threading
import time

MAX_SKEW = 5


class CompositionError(ValueError):
    """Names the first missing or invalid composition field; never a secret value."""


class FileGuard:
    """One exclusion shared by broker decisions and both claim writers.

    `flock` on the configured path; reentrant per thread inside this process so
    nested broker transactions cannot self-deadlock. Callable as a factory.
    Artemis lock writers take the same path through ARTEMIS_DEVICE_ADMISSION_GUARD.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._depth = 0
        self._fd = None

    def __call__(self):
        return self

    def __enter__(self):
        self._lock.acquire()
        try:
            if self._depth == 0:
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._depth -= 1
            if self._depth == 0:
                fd, self._fd = self._fd, None
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            self._lock.release()
        return False


def _fresh(path, key, max_age, clock=time.time):
    """Read `{"observed_at": epoch, key: value}`; stale or future snapshots refuse."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"observed_at", key}:
        raise ValueError("invalid snapshot")
    observed = payload["observed_at"]
    if type(observed) not in (int, float) or not -MAX_SKEW <= clock() - observed <= max_age:
        raise ValueError("stale snapshot")
    return payload[key]


def admitted_reader(path, max_age, registry=None, clock=time.time):
    """Per-device readiness AND explicit grants; inventory is never permission.

    Invalid source records close admission. Valid but stale, revoked or restricted
    devices are excluded individually. Caller holds the shared admission guard.
    """
    import math

    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid readiness time")
        return value

    def read():
        _owner_file(path)
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or set(payload) != {"generation", "devices"}
                or type(payload["generation"]) is not int or payload["generation"] < 1
                or not isinstance(payload["devices"], list) or registry is None):
            raise ValueError("invalid readiness source")
        now, result, seen = number(clock()), {}, set()
        for row in payload["devices"]:
            if (not isinstance(row, dict) or set(row) != {
                    "device_id", "endpoint", "state", "observed_at", "grant"}):
                raise ValueError("invalid readiness record")
            if any(not isinstance(row[k], str) or not row[k].strip()
                   for k in ("device_id", "endpoint", "state")):
                raise ValueError("invalid readiness identity")
            if row["device_id"] in seen:
                raise ValueError("duplicate readiness record")
            seen.add(row["device_id"])
            grant = row["grant"]
            if (not isinstance(grant, dict) or set(grant) != {
                    "reference", "observed_at", "expires_at"}
                    or not isinstance(grant["reference"], str) or not grant["reference"].strip()):
                raise ValueError("invalid grant record")
            observed, granted, expiry = (number(row["observed_at"]),
                                        number(grant["observed_at"]), number(grant["expires_at"]))
            device = registry.resolve(row["device_id"])
            if (device is not None and device.device_id == row["device_id"]
                    and registry.resolve(row["endpoint"]) == device
                    and device.pool != "spare" and not device.restricted
                    and registry.eligible(row["endpoint"], row["state"])
                    and -MAX_SKEW <= now - observed <= max_age
                    and -MAX_SKEW <= now - granted <= max_age
                    and granted < expiry and now < expiry):
                result[device.device_id] = row["endpoint"]
        return result

    return read


def android_claims_reader(path, max_age):
    """Android issue claims mirrored by the claim writer under the shared guard."""

    def read():
        claims = _fresh(path, "claims", max_age)
        if not isinstance(claims, list) or any(not isinstance(c, str) for c in claims):
            raise ValueError("invalid claims snapshot")
        return claims

    return read


def artemis_locks_reader(lock_dir):
    """Every Artemis execution-lock owner file, live or not (fail closed).

    Reads DeviceExecutionLock metadata without importing the SDK. Unreadable or
    partial records raise, closing admission; a stale record keeps its device
    blocked until Artemis's own stale-lock cleanup removes it.
    """
    lock_dir = Path(lock_dir)

    def read():
        if not lock_dir.is_dir():
            raise OSError("artemis lock directory missing")
        paths = sorted(lock_dir.glob("artemis-device-*.lock"))
        legacy = lock_dir / "artemis-global-device.lock"
        if legacy.exists():
            paths.append(legacy)
        devices = []
        for path in paths:
            owner = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(owner, dict) or not isinstance(owner.get("device_id"), str):
                raise ValueError("invalid artemis lock record")
            devices.append(owner["device_id"])
        return devices

    return read


def write_claims_snapshot(path, guard, claims, clock=time.time):
    """Removed unsafe snapshot replacement. Use ClaimLedger's owned claim/release."""
    raise ValueError("unverified claims snapshots are disabled; use the claim ledger")


def _owner_file(path):
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise CompositionError(
            "composition file must be a regular file owned by this user, not group/world writable"
        )


def _field(config, name, kind):
    value = config.get(name) if isinstance(config, dict) else None
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise CompositionError(f"composition incomplete: {name}")
    return value


def _snapshot(config, name):
    section = _field(config, name, dict)
    try:
        path = _field(section, "path", str)
        age = _field(section, "max_age_seconds", int)
    except CompositionError as exc:
        raise CompositionError(f"composition incomplete: {name}") from exc
    if type(age) is not int or not 1 <= age <= 3600:
        raise CompositionError(f"composition incomplete: {name}")
    return path, age


def _recovery(config):
    section = _field(config, "recovery", dict)
    try:
        spec = _field(section, "adapter", str)
        owner = _field(section, "owner", str)
        ops_issue = _field(section, "ops_issue", str)
        module, _, factory = spec.partition(":")
        if not module or not factory:
            raise CompositionError("adapter")
        adapter = getattr(importlib.import_module(module), factory)()
    except (CompositionError, ImportError, AttributeError, TypeError) as exc:
        raise CompositionError("composition incomplete: recovery") from exc
    # recover_pending passes the remaining deadline; both callbacks are mandatory.
    if not callable(getattr(adapter, "fence", None)) or not callable(
        getattr(adapter, "undo", None)
    ):
        raise CompositionError("composition incomplete: recovery")
    return adapter, owner, ops_issue


def compose(config_path, registry):
    """Return Broker keyword arguments plus the leader control socket path.

    `registry` is accepted so later checks can validate inventory against inputs;
    identity resolution itself stays in the broker.
    """
    _owner_file(config_path)
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CompositionError("composition file is not valid JSON") from exc
    admitted_path, admitted_age = _snapshot(config, "admitted")
    locks = _field(config, "artemis_locks_dir", str)
    claims_path, claims_age = _snapshot(config, "android_claims")
    guard = FileGuard(_field(config, "guard_path", str))
    recovery_io, owner, ops_issue = _recovery(config)
    control = Path(_field(config, "leader_control_socket", str))
    return {
        "broker": {
            "admitted": {},
            "admission_snapshot": admitted_reader(admitted_path, admitted_age, registry),
            "admission_sources": {
                "artemis": artemis_locks_reader(locks),
                "android": android_claims_reader(claims_path, claims_age),
            },
            "admission_guard": guard,
            "recovery_io": recovery_io,
            "recovery_owner": owner,
            "ops_issue": ops_issue,
        },
        "guard": guard,
        "android_claims_path": Path(claims_path),
        "leader_control_socket": control,
    }
