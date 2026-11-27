"""Concrete Linux owner recovery. No actions happen until an expired/released lease.

The owner records complete work manifests and undo intents through broker_changes
BEFORE execution. Missing manifests, unknown work or any failed readback quarantine.
This adapter accepts local bindings only. Neither successful cleanup nor TTL
releases a device.
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
    def __init__(
        self, db_path, *, registry=None, adb="adb", multica="multica", monotonic=time.monotonic
    ):
        self.db_path, self.adb, self.multica, self.monotonic = (
            Path(db_path),
            adb,
            multica,
            monotonic,
        )
        self._fenced = set()
        self.registry = registry

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

    def _descendants(self, identity, *, allow_missing=False):
        if not identity or identity.get("mode") != "local":
            raise ValueError("local work binding required")
        expected = {k: identity[k] for k in ("mode", "pid", "process_start", "boot_id")}
        current = self._identity(identity["pid"])
        if current is None and allow_missing:
            return set()  # Only after this fence validated and stopped the root.
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
            if not line.strip():
                continue  # adb prints a newline even when the list is empty.
            fields = line.split()
            if len(fields) != 3:
                raise ValueError("invalid forward census")
            serial, local, remote = fields
            resolved = self.registry.resolve(serial) if self.registry is not None else None
            if direction == "forward" and resolved is None and serial not in {device, endpoint}:
                raise ValueError("unknown forward identity")
            if (
                direction == "reverse"
                or serial in {device, endpoint}
                or (resolved is not None and resolved.device_id == device)
            ):
                if local in result:
                    raise ValueError("ambiguous forward")
                result[local] = remote
        return result

    def fence_startup(self, group, *, timeout):
        """Fence a startup-origin AVD without ADB or userdata mutations."""
        from startup import LinuxStartupIO, _journal, pinned_device

        deadline = self.monotonic() + timeout

        def remaining():
            value = deadline - self.monotonic()
            if value <= 0:
                raise TimeoutError()
            return value

        proof = group["startup"]
        device = pinned_device(self.registry, proof)
        if [(item["device_id"], item["endpoint"]) for item in group["devices"]] != [
            (device.device_id, f"emulator-{device.console_port}")
        ]:
            return False
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT generation,grant_ref,state,journal FROM broker_launches WHERE lease_id=?",
                (group["lease_id"],),
            ).fetchone()
        changes = self._changes(group)
        identities = []
        if row is not None:
            generation, grant, state, journal = row
            if (generation, grant) != (
                proof["generation"],
                proof["grant"]["reference"],
            ) or state not in {"paused", "executing"}:
                return False
            journal = _journal(json.loads(journal))
            identities = [journal["child"], journal["worker"]]
            if not changes or changes[0] != dict(
                kind="work",
                device_id=device.device_id,
                complete=True,
                processes=[journal["worker"], journal["child"]],
                forwards=[],
            ):
                return False

        def resource_users():
            remaining()
            sockets = LinuxStartupIO.socket_owners()
            return (
                LinuxStartupIO.resource_users(device)
                | sockets.get(device.console_port, set())
                | sockets.get(device.adb_port, set())
            )

        owned = {identity["pid"] for identity in identities}
        if resource_users() - owned:
            return False
        if owned:
            if owned & {1, os.getpid(), self._daemon_pid(remaining()), group["binding"]["pid"]}:
                return False
            for identity in identities:
                current = self._identity(identity["pid"])
                if current is not None and (
                    current != identity or self._descendants(identity) - owned
                ):
                    return False
            for identity in identities:
                if self._identity(identity["pid"]) is not None and not self._stop(
                    identity, remaining()
                ):
                    return False
            if any(self._identity(pid) is not None for pid in owned):
                return False
        return (
            not resource_users()
            and group["access_phase"] == "starting"
            and len(changes) == bool(identities)
            and remaining() > 0
        )

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
            # The broker journals a root-only manifest at grant; an owner-recorded one
            # for the same device is later in the journal and supersedes it.
            work = list({c["device_id"]: c for c in changes if c["kind"] == "work"}.values())
            devices = {d["device_id"] for d in group["devices"]}
            if (
                group["binding"].get("mode") not in {"local", "managed"}
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
            root = {
                "mode": "local",
                **{k: group["binding"][k] for k in ("pid", "process_start", "boot_id")},
            }
            if identities.get(root["pid"]) != root:
                return False
            daemon = self._daemon_pid(remaining())
            if set(identities) & {1, os.getpid(), daemon}:
                return False
            if self._descendants(root) - identities.keys():
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
            if self._descendants(root, allow_missing=True):
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
