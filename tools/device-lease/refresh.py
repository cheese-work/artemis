"""Owner timer producer: observe the existing ADB server; retain owned claims.

This is cooperative host composition, not isolation from the same Unix user.
Initial claim reconciliation remains an explicit owner census, never inferred
from an empty process list. A failed refresh invalidates the census immediately.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from compose import _owner_file, write_readiness


def _environment(unit):
    if not isinstance(unit, str) or not unit.endswith(".service") or unit.startswith("-"):
        raise ValueError("invalid writer unit")
    pid = int(
        subprocess.check_output(
            ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
            text=True,
            timeout=5,
        )
    )
    if pid <= 0:
        raise ValueError("writer is not running")
    return dict(
        item.decode().split("=", 1)
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if b"=" in item
    )


def _writers(config, composed, db_path):
    writers, isolated = config["writers"], config["isolated_writers"]
    if not isinstance(writers, list) or not writers or not isinstance(isolated, list):
        raise ValueError("complete writer configuration required")
    seen = set()
    for row in writers + isolated:
        if not isinstance(row, dict) or row["unit"] in seen:
            raise ValueError("invalid writer configuration")
        seen.add(row["unit"])
        env = _environment(row["unit"])
        if row in writers:
            if env.get("ARTEMIS_DEVICE_ADMISSION_GUARD") != str(composed["guard"].path) or env.get(
                "ARTEMIS_DEVICE_CLAIMS_DB"
            ) != str(db_path):
                raise ValueError("unguarded live writer")
            # SDK derives its lock directory from tempfile, not ARTEMIS_APP_DIR.
            temp = next((env[k] for k in ("TMPDIR", "TEMP", "TMP") if env.get(k)), "/tmp")
            if Path(temp) / "artemis/device-locks" != composed["ledger"].lock_dir:
                raise ValueError("writer lock directory differs")
            sdk = Path(row["sdk_lock"])
            if sdk.stat().st_uid != os.getuid() or sdk.stat().st_mode & 0o002:
                raise ValueError("untrusted writer SDK")
            code = sdk.read_text()
            if "check_sdk_available(" not in code or "ARTEMIS_DEVICE_ADMISSION_GUARD" not in code:
                raise ValueError("writer SDK lacks admission guard")
        else:
            # Explicitly disabled staging stays disabled; a changed wrapper fails closed.
            wrapper = Path(row["adb_path"])
            if wrapper.stat().st_uid != os.getuid() or wrapper.stat().st_mode & 0o002:
                raise ValueError("untrusted isolation wrapper")
            if (
                env.get("ARTEMIS_ADB_PATH") != str(wrapper)
                or env.get("ADBUTILS_ADB_PATH") != str(wrapper)
                or hashlib.sha256(wrapper.read_bytes()).hexdigest() != row["sha256"]
            ):
                raise ValueError("isolated writer configuration changed")


def devices():
    """Only host:devices-l on shared loopback 5037. Never launch or reset ADB."""

    def exact(connection, size):
        result = bytearray()
        while len(result) < size:
            part = connection.recv(size - len(result))
            if not part:
                raise ValueError("short ADB response")
            result.extend(part)
        return bytes(result)

    with socket.create_connection(("127.0.0.1", 5037), timeout=3) as connection:
        connection.sendall(b"000ehost:devices-l")
        if exact(connection, 4) != b"OKAY":
            raise ValueError("ADB enumeration refused")
        size = int(exact(connection, 4), 16)
        if size > 1024 * 1024:
            raise ValueError("ADB enumeration too large")
        payload = exact(connection, size).decode()
    result = {}
    for line in payload.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] in result:
            raise ValueError("invalid ADB enumeration")
        result[fields[0]] = fields[1]
    return result


def refresh(composed, host_config, registry, db_path):
    """One guarded observation and refresh; never erase a known owned claim."""
    ledger = composed["ledger"]
    with composed["guard"]():
        snapshot = ledger.snapshot()
        try:
            if snapshot["source"] is None:
                raise ValueError("initial owner census required")
            _owner_file(host_config)
            config = json.loads(Path(host_config).read_text())
            _writers(config, composed, db_path)
            grants = config["devices"]
            expiry, reference = config["expires_at"], config["reference"]
            if (
                not isinstance(grants, list)
                or not grants
                or len(set(grants)) != len(grants)
                or type(expiry) not in (int, float)
                or not math.isfinite(expiry)
                or not isinstance(reference, str)
                or not reference.strip()
            ):
                raise ValueError("invalid owner grants")
            for serial in grants:
                device = registry.resolve(serial)
                if (
                    device is None
                    or device.device_id != serial
                    or device.pool != "rig"
                    or device.restricted
                    or not registry.eligible(serial, "device")
                ):
                    raise ValueError("grant outside confirmed unrestricted rig")
            observed = devices()
            now = time.time()
            generation = snapshot["source"]["generation"]
            payload = {
                "generation": generation,
                "devices": [
                    {
                        "device_id": serial,
                        "endpoint": serial,
                        "state": "device",
                        "observed_at": now,
                        "grant": {"reference": reference, "observed_at": now, "expires_at": expiry},
                    }
                    for serial in grants
                    if observed.get(serial) == "device" and expiry > now
                ],
            }
            # Reuse owner operations; the outer flock spans source reads and commits.
            with tempfile.NamedTemporaryFile(
                mode="w", dir=db_path.parent, suffix=".json"
            ) as stream:
                json.dump(payload, stream, allow_nan=False)
                stream.flush()
                write_readiness(composed, stream.name, registry)
            result = ledger.census(
                generation=generation,
                observed_at=now,
                complete=True,
                writers=["android", "artemis"],
                claims=snapshot["claims"],
            )
            return {**result, "ready_devices": len(payload["devices"])}
        except Exception as exc:
            try:
                ledger.census(
                    generation=1,
                    observed_at=time.time(),
                    complete=False,
                    writers=["android", "artemis"],
                    claims=snapshot["claims"],
                )
            except ValueError:
                pass  # census deliberately commits complete=0 before raising
            raise ValueError("host refresh unavailable; census invalidated") from exc
