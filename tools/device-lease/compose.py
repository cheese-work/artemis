"""Owner composition for the live `serve` path (CHE-5297). Standard library only.

A trusted, owner-only JSON file names every admission and recovery input. Any
missing or invalid input refuses startup; there are no empty readers, no default
guard and no TTL-only release. File I/O runs before SQLite's writer lock and
inside the shared claim guard, as the broker requires.
"""

import fcntl
import json
import math
from dataclasses import asdict
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

    def __init__(self, path, *, create=True):
        self.path = Path(path)
        self._flags = (os.O_CREAT | os.O_RDWR if create else os.O_RDONLY) | os.O_NOFOLLOW
        self._lock = threading.RLock()
        self._depth = 0
        self._fd = None

    def __call__(self):
        return self

    def __enter__(self):
        self._lock.acquire()
        try:
            if self._depth == 0:
                fd = os.open(self.path, self._flags, 0o600)
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
        if (
            not isinstance(payload, dict)
            or set(payload) != {"generation", "devices"}
            or type(payload["generation"]) is not int
            or payload["generation"] < 1
            or not isinstance(payload["devices"], list)
            or registry is None
        ):
            raise ValueError("invalid readiness source")
        now, result, seen = number(clock()), {}, set()
        for row in payload["devices"]:
            if not isinstance(row, dict) or set(row) != {
                "device_id",
                "endpoint",
                "state",
                "observed_at",
                "grant",
            }:
                raise ValueError("invalid readiness record")
            if any(
                not isinstance(row[k], str) or not row[k].strip()
                for k in ("device_id", "endpoint", "state")
            ):
                raise ValueError("invalid readiness identity")
            if row["device_id"] in seen:
                raise ValueError("duplicate readiness record")
            seen.add(row["device_id"])
            grant = row["grant"]
            if (
                not isinstance(grant, dict)
                or set(grant) != {"reference", "observed_at", "expires_at"}
                or not isinstance(grant["reference"], str)
                or not grant["reference"].strip()
            ):
                raise ValueError("invalid grant record")
            observed, granted, expiry = (
                number(row["observed_at"]),
                number(grant["observed_at"]),
                number(grant["expires_at"]),
            )
            device = registry.resolve(row["device_id"])
            endpoint_device = registry.resolve(row["endpoint"])
            if row["endpoint"].startswith("emulator-") or any(
                item is not None and item.kind == "avd" for item in (device, endpoint_device)
            ):
                raise ValueError("AVD readiness requires lifecycle evidence and a phase grant")
            if (
                device is not None
                and device.device_id == row["device_id"]
                and endpoint_device == device
                and device.pool != "spare"
                and not device.restricted
                and registry.eligible(row["endpoint"], row["state"])
                and -MAX_SKEW <= now - observed <= max_age
                and -MAX_SKEW <= now - granted <= max_age
                and granted < expiry
                and now < expiry
            ):
                result[device.device_id] = row["endpoint"]
        return result

    return read


def artemis_locks_reader(lock_dir):
    """Every Artemis execution-lock owner file, live or not (fail closed).

    Reads DeviceExecutionLock metadata without importing the SDK. Unreadable or
    partial records raise, closing admission; a stale record keeps its device
    blocked until Artemis's own stale-lock cleanup removes it.
    """
    from device_lease.claims import sdk_lock_alias

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
            devices.append(sdk_lock_alias(owner["device_id"], owner.get("webusb_claim_alias")))
        return devices

    return read


def startup_reader(path, max_age, registry, leader_id, clock=time.time):
    """Owner-only scoped grant and independent release; never ordinary readiness.

    The complete writer census is separately required through ClaimLedger.read.
    The pinned inventory identity must agree in full; a serial alone proves nothing.
    """

    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid startup grant time")
        return value

    def read():
        _owner_file(path)
        row = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(row, dict) or set(row) - {"claim_handoff"} != {
            "generation",
            "device",
            "principal",
            "agent_id",
            "issue_id",
            "run_id",
            "grant",
            "release",
        }:
            raise ValueError("invalid startup evidence")
        if type(row["generation"]) is not int or row["generation"] < 1:
            raise ValueError("invalid startup generation")
        if any(
            not isinstance(row[k], str) or not row[k].strip()
            for k in ("principal", "agent_id", "issue_id", "run_id")
        ):
            raise ValueError("invalid startup owner")
        if "claim_handoff" in row:
            consent = row["claim_handoff"]
            if (
                not isinstance(consent, dict)
                or set(consent) != {"claim_id", "owner", "run_id", "census_generation", "reference"}
                or type(consent["census_generation"]) is not int
                or consent["census_generation"] < 1
                or any(
                    not isinstance(consent[key], str) or not consent[key].strip()
                    for key in ("claim_id", "owner", "run_id", "reference")
                )
            ):
                raise ValueError("invalid claim handoff")
        device = registry.resolve(row["device"].get("device_id"))
        if (
            device is None
            or device.kind != "avd"
            or device.restricted
            or json.loads(json.dumps(asdict(device))) != row["device"]
        ):
            raise ValueError("startup identity mismatch")
        release, grant = row["release"], row["grant"]
        if (
            not isinstance(release, dict)
            or set(release) != {"verifier", "holder", "evidence", "passed"}
            or release["verifier"] != leader_id
            or release["verifier"] in {release["holder"], row["principal"], row["agent_id"]}
            or release["passed"] is not True
            or any(
                not isinstance(release[k], str) or not release[k].strip()
                for k in ("holder", "evidence")
            )
            or not isinstance(grant, dict)
            or set(grant) != {"reference", "observed_at", "expires_at"}
            or not isinstance(grant["reference"], str)
            or not grant["reference"].strip()
        ):
            raise ValueError("startup release or grant unavailable")
        observed, expiry, now = (
            number(grant["observed_at"]),
            number(grant["expires_at"]),
            number(clock()),
        )
        if not (-MAX_SKEW <= now - observed <= max_age and observed < expiry and now < expiry):
            raise ValueError("startup grant expired or stale")
        return row

    return read


def shared_adb_reader(path, max_age, clock=time.time):
    """Fresh ops-owned host preflight; the launch adapter rechecks actual ownership."""

    def read():
        _owner_file(path)
        row = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(row, dict) or set(row) != {
            "observed_at",
            "host",
            "port",
            "listener",
            "evidence",
        }:
            raise ValueError("invalid shared ADB host preflight")
        observed = row["observed_at"]
        if (
            type(observed) not in (int, float)
            or not math.isfinite(observed)
            or not -MAX_SKEW <= clock() - observed <= max_age
            or row["host"] != "127.0.0.1"
            or type(row["port"]) is not int
            or row["port"] != 5037
            or not isinstance(row["evidence"], str)
            or not row["evidence"].strip()
        ):
            raise ValueError("shared ADB preflight stale or unavailable")
        from startup import process_record

        listener = row["listener"]
        return process_record(listener)

    return read


def avd_grant_reader(path, registry, max_age, clock=time.time):
    """Owner grants for a specific committed lease, with no grant across phases."""

    def read():
        _owner_file(path)
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("invalid AVD grants source")
        now, seen, result = clock(), set(), {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) - {"testcase", "qualification"}
                != {
                    "phase",
                    "device_id",
                    "endpoint",
                    "lease_id",
                    "generation",
                    "reference",
                    "observed_at",
                    "expires_at",
                }
                or any(
                    not isinstance(row[key], str) or not row[key].strip()
                    for key in ("phase", "device_id", "endpoint", "lease_id", "reference")
                )
            ):
                raise ValueError("invalid AVD phase grant")
            device = registry.resolve(row["device_id"])
            phase = row["phase"]
            if (
                phase not in {"identity", "qualification", "work"}
                or phase in seen
                or device is None
                or device.kind != "avd"
                or device.restricted
                or device.device_id != row["device_id"]
                or row["endpoint"] != f"emulator-{device.console_port}"
                or type(row["generation"]) is not int
                or row["generation"] < 1
                or any(
                    type(row[key]) not in (int, float) or not math.isfinite(row[key])
                    for key in ("observed_at", "expires_at")
                )
                or row["observed_at"] >= row["expires_at"]
                or (phase == "identity" and set(row) & {"testcase", "qualification"})
            ):
                raise ValueError("invalid AVD grant scope")
            seen.add(phase)
            if -MAX_SKEW <= now - row["observed_at"] <= max_age and now < row["expires_at"]:
                result[phase] = row
        return result

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


def _recovery(config, db_path, registry):
    from owned_recovery import OwnedRecovery

    section = _field(config, "recovery", dict)
    try:
        spec = _field(section, "adapter", str)
        owner = _field(section, "owner", str)
        ops_issue = _field(section, "ops_issue", str)
        if spec != "owned":
            raise CompositionError("adapter")
    except CompositionError as exc:
        raise CompositionError("composition incomplete: recovery") from exc
    return OwnedRecovery(db_path, registry=registry), owner, ops_issue


def compose(config_path, registry, *, db_path=None, initialize=True):
    """One store for the daemon, claims source and concrete recovery adapter.

    Validation precedes state creation. Legacy caller-written claims and imported
    callback factories cannot enable the live path.
    """
    _owner_file(config_path)
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CompositionError("composition file is not valid JSON") from exc
    managed_runs = None
    if "managed_bindings_dir" in config:
        from managed_runs import ManagedRuns

        try:
            managed_runs = ManagedRuns(_field(config, "managed_bindings_dir", str))
        except (OSError, ValueError, TypeError) as exc:
            raise CompositionError("composition incomplete: managed_bindings_dir") from exc
    admitted_path, admitted_age = _snapshot(config, "admitted")
    locks = _field(config, "artemis_locks_dir", str)
    claims = _field(config, "android_claims", dict)
    if (
        set(claims) != {"source", "max_age_seconds"}
        or claims.get("source") != "ledger"
        or type(claims.get("max_age_seconds")) is not int
        or not 1 <= claims["max_age_seconds"] <= 3600
    ):
        raise CompositionError("composition incomplete: android_claims")
    guard = FileGuard(_field(config, "guard_path", str), create=initialize)
    # Validate recovery even when db_path is absent; no dynamic import fallback.
    recovery_io, owner, ops_issue = _recovery(config, db_path or ".", registry)
    control = Path(_field(config, "leader_control_socket", str))
    leader_id = _field(config, "leader_id", str)
    startup_snapshot = None
    startup_io = None
    avd_grants = dict
    if "startup" in config:
        startup_path, startup_age = _snapshot(config, "startup")
        if managed_runs is None:
            raise CompositionError("startup requires managed_bindings_dir")
        startup_snapshot = startup_reader(startup_path, startup_age, registry, leader_id)
        if "worker" in config["startup"]:
            worker = _field(config["startup"], "worker", dict)
            if worker.get("adapter") != "linux-paused":
                raise CompositionError("startup worker adapter must be linux-paused")
            if os.geteuid() != 0:
                raise CompositionError("linux-paused requires root with full host /proc visibility")
            host_path, host_age = _snapshot(worker, "host_preflight")
            from startup import LinuxStartupIO

            startup_io = LinuxStartupIO(shared_adb_reader(host_path, host_age))
    if "avd_grants" in config:
        if startup_snapshot is None or not any(
            device.kind == "avd" for device in registry.devices()
        ):
            raise CompositionError("avd_grants requires startup and a registered AVD")
        grants_path, grants_age = _snapshot(config, "avd_grants")
        avd_grants = avd_grant_reader(grants_path, registry, grants_age)
    if db_path is None:
        raise CompositionError("composition requires the broker database")
    db_path = Path(db_path)
    if initialize:
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = db_path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CompositionError("broker state directory must be private and owner-only")
    from device_lease.claims import ClaimLedger

    lifecycle = {key: config[key] for key in ("startup", "avd_grants") if key in config}
    if lifecycle:
        lifecycle["managed_bindings_dir"] = config["managed_bindings_dir"]
        lifecycle["leader_id"] = leader_id
    ledger = ClaimLedger(
        db_path,
        guard,
        registry,
        locks,
        max_age=claims["max_age_seconds"],
        lifecycle_config=lifecycle,
        initialize=initialize,
    )
    return {
        "broker": {
            "managed_runs": managed_runs,
            "startup_snapshot": startup_snapshot,
            "startup_io": startup_io,
            "avd_grants": avd_grants,
            "admitted": {},
            "admission_snapshot": admitted_reader(admitted_path, admitted_age, registry),
            "admission_sources": {"artemis": artemis_locks_reader(locks), "android": ledger.read},
            "admission_guard": guard,
            "recovery_io": recovery_io,
            "recovery_owner": owner,
            "ops_issue": ops_issue,
            "leader_id": leader_id,
        },
        "guard": guard,
        "ledger": ledger,
        "admitted_path": Path(admitted_path),
        "admitted_age": admitted_age,
        "leader_control_socket": control,
    }


def write_readiness(composed, input_path, registry):
    """Trusted owner producer. Validate observations and atomically publish a generation."""
    reader = admitted_reader(input_path, composed["admitted_age"], registry)
    destination, guard = composed["admitted_path"], composed["guard"]
    with guard():
        reader()  # Validates source ownership, schema, grants and timestamps.
        payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
        if destination.exists():
            _owner_file(destination)
            prior = json.loads(destination.read_text(encoding="utf-8"))
            if payload["generation"] < prior["generation"]:
                raise ValueError("stale readiness generation")
        temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    return {"generation": payload["generation"]}
