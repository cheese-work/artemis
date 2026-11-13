"""Concrete Linux owner recovery. No actions happen until an expired/released lease.

The owner records complete work manifests and undo intents through broker_changes
BEFORE execution. Missing manifests, unknown work or any failed readback quarantine.
Raw local ADB has no remotely revocable credential: remote transports must wrap this
adapter in RemoteRecovery. Neither successful cleanup nor TTL releases a device.
"""

import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import time

from broker import process_identity
from recovery import validate_change

SETTINGS = {
    "brightness": ("system", "screen_brightness"),
    "screen_brightness": ("system", "screen_brightness"),
    "stay_on_while_plugged_in": ("global", "stay_on_while_plugged_in"),
}


class OwnedRecovery:
    def __init__(self, db_path, *, adb="adb", multica="multica", monotonic=time.monotonic):
        self.db_path, self.adb, self.multica, self.monotonic = (
            Path(db_path),
            adb,
            multica,
            monotonic,
        )
        self._fenced = set()

    def _changes(self, group):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT change FROM broker_changes WHERE lease_id=? ORDER BY seq",
                (group["lease_id"],),
            ).fetchall()
        changes = [json.loads(r[0]) for r in rows]
        for change in changes:
            validate_change(change)
        return changes

    @staticmethod
    def _identity(pid):
        try:
            return process_identity(pid)
        except (FileNotFoundError, ProcessLookupError):
            return None

    def _descendants(self, identity):
        if not identity or identity.get("mode") != "local":
            raise ValueError("local work binding required")
        expected = {k: identity[k] for k in ("mode", "pid", "process_start", "boot_id")}
        current = self._identity(identity["pid"])
        if current != expected:
            raise ValueError("old root unavailable; orphaned work cannot be ruled out")
        parents = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                if fields[0] not in {"Z", "X"}:
                    parents[int(path.parent.name)] = int(fields[1])
            except (FileNotFoundError, ProcessLookupError):
                continue
        found, frontier = set(), {identity["pid"]}
        while frontier:
            frontier = {pid for pid, parent in parents.items() if parent in frontier} - found
            found.update(frontier)
        return found

    def _daemon_pid(self, timeout):
        result = subprocess.run(
            [self.multica, "daemon", "status", "--output", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        payload = json.loads(result.stdout)
        if payload.get("status") != "running" or type(payload.get("pid")) is not int:
            raise ValueError("daemon identity unavailable")
        return payload["pid"]

    def _stop(self, identity, timeout):
        # pidfd pins the process, closing the identity-check/signal PID-reuse race.
        deadline = self.monotonic() + timeout
        fd = os.pidfd_open(identity["pid"])
        try:
            if self._identity(identity["pid"]) != identity:
                return False
            daemon = self._daemon_pid(max(0.001, deadline - self.monotonic()))
            if identity["pid"] in {1, os.getpid(), daemon} or self.monotonic() >= deadline:
                return False
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            waiter = select.poll()
            waiter.register(fd, select.POLLIN)
            return bool(waiter.poll(max(0, int((deadline - self.monotonic()) * 1000))))
        except ProcessLookupError:
            return True
        finally:
            os.close(fd)

    def _adb(self, group, device_id, args, timeout):
        endpoints = {d["device_id"]: d["endpoint"] for d in group["devices"]}
        endpoint = endpoints[device_id]
        if not isinstance(endpoint, str) or not endpoint or endpoint.startswith("-"):
            raise ValueError("invalid recovery endpoint")
        result = subprocess.run(
            [self.adb, "-s", endpoint, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return result.stdout

    def _forwards(self, group, device, direction, timeout):
        data = self._adb(group, device, [direction, "--list"], timeout)
        endpoint = next(d["endpoint"] for d in group["devices"] if d["device_id"] == device)
        result = {}
        for line in data.splitlines():
            fields = line.split()
            if len(fields) != 3:
                raise ValueError("invalid forward census")
            serial, local, remote = fields
            if direction == "reverse" or serial in {device, endpoint}:
                if local in result:
                    raise ValueError("ambiguous forward")
                result[local] = remote
        return result

    def fence(self, group, *, timeout):
        deadline = self.monotonic() + timeout

        def remaining():
            value = deadline - self.monotonic()
            if value <= 0:
                raise TimeoutError()
            return value

        try:
            remaining()
            changes = self._changes(group)
            work = [c for c in changes if c["kind"] == "work"]
            devices = {d["device_id"] for d in group["devices"]}
            if (
                group["binding"].get("mode") != "local"
                or {w["device_id"] for w in work} != devices
                or len(work) != len(devices)
            ):
                return False
            identities = {}
            for item in work:
                for identity in item["processes"]:
                    if identity["pid"] in identities and identities[identity["pid"]] != identity:
                        return False
                    identities[identity["pid"]] = identity
            daemon = self._daemon_pid(remaining())
            if set(identities) & {1, os.getpid(), daemon}:
                return False
            if self._descendants(group["binding"]) - identities.keys():
                return False
            # Validate EVERY identity and descendant before the first signal.
            for pid, identity in identities.items():
                current = self._identity(pid)
                if current is not None and current != identity:
                    return False
                if current is not None and self._descendants(identity) - identities.keys():
                    return False
            for identity in identities.values():
                if (
                    self._identity(identity["pid"]) is not None
                    and self._stop(identity, remaining()) is not True
                ):
                    return False
            if any(self._identity(pid) is not None for pid in identities):
                return False
            if self._descendants(group["binding"]):
                return False
            for item in work:
                device = item["device_id"]
                for direction in ("forward", "reverse"):
                    allowed = {
                        f["local"]: f["remote"]
                        for f in item["forwards"]
                        if f["direction"] == direction
                    }
                    actual = self._forwards(group, device, direction, remaining())
                    if any(allowed.get(local) != remote for local, remote in actual.items()):
                        return False
                    for local in actual:
                        self._adb(group, device, [direction, "--remove", local], remaining())
                    if self._forwards(group, device, direction, remaining()):
                        return False
            remaining()
            self._fenced.add(group["lease_id"])
            return True
        except Exception:
            return False

    def undo(self, group, change, *, timeout):
        deadline = self.monotonic() + timeout

        def adb(args):
            left = deadline - self.monotonic()
            if left <= 0:
                raise TimeoutError()
            return self._adb(group, change["device_id"], args, left).strip()

        try:
            validate_change(change)
            if (
                timeout <= 0
                or change not in self._changes(group)
                or change["device_id"] not in {d["device_id"] for d in group["devices"]}
            ):
                return False
            if change["kind"] == "work":
                return group["lease_id"] in self._fenced  # Only after confirmed fencing.
            if change["kind"] == "setting":
                namespace, name = SETTINGS[change["name"]]  # Fixed allowlist; no shell fragments.
                before, after = change["before"], change["after"]
                current = adb(["shell", "settings", "get", namespace, name])
                if current == (before if before is not None else "null"):
                    return True
                if current != (after if after is not None else "null"):
                    return False
                if before is None:
                    adb(["shell", "settings", "delete", namespace, name])
                elif before.isdecimal():
                    adb(["shell", "settings", "put", namespace, name, before])
                else:
                    return False
                return adb(["shell", "settings", "get", namespace, name]) == (
                    before if before is not None else "null"
                )
            if change["kind"] == "app":
                import re

                package = change["package"]
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package):
                    return False
                marker = "package:" + package

                def packages():
                    lines = adb(["shell", "pm", "list", "packages", package]).splitlines()
                    if any(
                        not re.fullmatch(
                            r"package:[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", line
                        )
                        for line in lines
                    ):
                        raise ValueError("invalid package census")
                    return lines

                if marker not in packages():
                    return True
                if adb(["uninstall", package]) != "Success":
                    return False
                return marker not in packages()
            # A legacy HOME intent lacks comparable state. Never claim a successful no-op.
            return False
        except Exception:
            return False
