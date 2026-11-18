"""Trusted startup orchestration. No allocation and no ordinary work permission.

The shared claim guard covers the whole bounded launch. SQLite records an attempt
before spawn and both paused identities before execution. A planned/paused attempt
is deliberately NOT retried after uncertainty: recovery must reconcile it first.
"""

import hashlib
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from broker import Refused, process_identity
from refresh import devices


# Hashing pinned images under the guard can outlast a short process handshake.
# EOF still aborts immediately if the broker exits before releasing the gate.
PAUSE_TIMEOUT_SECONDS = 120


class StartupMismatch(ValueError):
    """Positive evidence that the owned process or guest identity disagrees."""


class StartupNotReady(ValueError):
    """The same launch may be observed again without consuming another grant."""


def _group(broker, conn, now, lease_id, principal, run_id):
    broker._require_authority(conn)
    broker._expire(conn, now)
    group = broker._owned(conn, lease_id, principal, run_id)
    if group["state"] != "active":
        raise Refused("lease_not_active")
    if group["access_phase"] != "starting":
        raise Refused("access_phase_denied")
    if broker.draining or broker._admission.unavailable or not broker._admission.audit_ok:
        raise Refused("admission_unavailable")
    device_id = group["devices"][0]["device_id"]
    if device_id in broker._admission.blocked:
        raise Refused("device_busy")
    binding = group["binding"]
    proof = broker._startup_proof(
        conn, now, principal, binding["agent_id"], binding["issue_id"], run_id, binding, device_id
    )
    if proof != group["startup"]:
        raise Refused("startup_not_admitted")
    return group, broker.registry.resolve(device_id)


def process_record(identity):
    if (
        not isinstance(identity, dict)
        or set(identity) != {"mode", "pid", "process_start", "boot_id"}
        or identity["mode"] != "local"
        or type(identity["pid"]) is not int
        or identity["pid"] < 2
        or any(
            not isinstance(identity[k], str) or not identity[k]
            for k in ("process_start", "boot_id")
        )
    ):
        raise ValueError("invalid process identity")
    return identity


def _journal(value):
    if not isinstance(value, dict) or set(value) != {"worker", "child"}:
        raise ValueError("invalid paused process journal")
    for identity in value.values():
        process_record(identity)
    if (
        value["child"]["pid"] == value["worker"]["pid"]
        or value["child"]["boot_id"] != value["worker"]["boot_id"]
    ):
        raise ValueError("invalid paused process association")
    return json.loads(json.dumps(value))


def launch(broker, *, lease_id, principal, run_id):
    if broker.startup_io is None:
        raise Refused("startup_launch_unavailable")
    broker._text(lease_id, principal, run_id)
    with broker.admission_guard():
        with broker.transaction() as (conn, now):
            group, device = _group(broker, conn, now, lease_id, principal, run_id)
            previous = conn.execute(
                "SELECT * FROM broker_launches WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if previous:
                if previous["state"] != "executing":
                    raise Refused("startup_launch_uncertain")
                return dict(
                    lease_id=lease_id, state="executing", journal=json.loads(previous["journal"])
                )
        try:
            broker.startup_io.preflight(device)
        except Exception as exc:
            raise Refused("startup_preflight_failed") from exc
        with broker.transaction() as (conn, now):
            group, device = _group(broker, conn, now, lease_id, principal, run_id)
            proof = group["startup"]
            if conn.execute(
                "SELECT 1 FROM broker_launches WHERE generation=? AND grant_ref=?",
                (proof["generation"], proof["grant"]["reference"]),
            ).fetchone():
                raise Refused("startup_grant_consumed")
            conn.execute(
                "INSERT INTO broker_launches VALUES (?,?,?,'planned',NULL,NULL)",
                (lease_id, proof["generation"], proof["grant"]["reference"]),
            )
        broker.boundary("launch:planned")
        handle = broker.startup_io.prepare(device)
        executing = False
        try:
            journal = _journal(handle)
            # Injection point models a journal write failure while the child is paused.
            broker.boundary("launch:prepared")
            with broker.transaction() as (conn, now):
                _group(broker, conn, now, lease_id, principal, run_id)
                conn.execute(
                    "UPDATE broker_launches SET state='paused',journal=? WHERE lease_id=?",
                    (json.dumps(journal), lease_id),
                )
                conn.execute(
                    "INSERT INTO broker_changes(lease_id,change) VALUES (?,?)",
                    (
                        lease_id,
                        json.dumps(
                            dict(
                                kind="work",
                                device_id=device.device_id,
                                complete=True,
                                processes=list(journal.values()),
                                forwards=[],
                            )
                        ),
                    ),
                )
            broker.boundary("launch:journalled")
            # Recheck shared ADB/ports/data and current authority after journalling.
            broker.startup_io.preflight(device, journal)
            with broker.transaction() as (conn, now):
                _group(broker, conn, now, lease_id, principal, run_id)
                conn.execute(
                    "UPDATE broker_launches SET state='executing' WHERE lease_id=?", (lease_id,)
                )
            executing = True  # Commit precedes gate release; a lost response cannot retry it.
            broker.boundary("launch:executing")
            broker.startup_io.resume(handle)
        finally:
            if not executing:
                broker.startup_io.abort(handle)
        return dict(lease_id=lease_id, state="executing", journal=journal)


def verify(broker, *, lease_id, principal, run_id):
    if broker.startup_io is None:
        raise Refused("startup_launch_unavailable")
    broker._text(lease_id, principal, run_id)
    with broker.admission_guard():
        with broker.transaction() as (conn, now):
            _, device = _group(broker, conn, now, lease_id, principal, run_id)
            row = conn.execute(
                "SELECT * FROM broker_launches WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if row is None or row["state"] != "executing":
                raise Refused("startup_not_launched")
            journal = _journal(json.loads(row["journal"]))
        # The trusted adapter must bind processes and observe a real device before
        # any scoped guest probe. Never accept observations through an RPC argument.
        try:
            identity = broker.startup_io.observe(device, journal)
            expected = dict(
                **journal,
                parent_pid=journal["worker"]["pid"],
                avd_name=device.avd_name,
                data_path=device.avd_data_path,
                userdata=device.avd_data_path + "/userdata-qemu.img",
                console_port=device.console_port,
                adb_port=device.adb_port,
                serial=f"emulator-{device.console_port}",
                api=device.api,
                abi=device.abi,
                socket_pids=[journal["child"]["pid"]],
                userdata_pids=[journal["child"]["pid"]],
                device_state="device",
                boot_complete="1",
            )
            readiness = {"device_state", "boot_complete"}
            if {k: v for k, v in identity.items() if k not in readiness} != {
                k: v for k, v in expected.items() if k not in readiness
            }:
                raise StartupMismatch("process/guest binding mismatch")
            if identity.get("device_state") != "device" or identity.get("boot_complete") != "1":
                raise StartupNotReady("guest boot not complete")
        except StartupMismatch as exc:
            with broker.transaction() as (conn, now):
                broker._owned(conn, lease_id, principal, run_id)
                conn.execute(
                    "UPDATE broker_groups SET state='fencing' WHERE lease_id=? AND state='active'",
                    (lease_id,),
                )
            raise Refused("startup_identity_mismatch") from exc
        except StartupNotReady as exc:
            raise Refused("startup_not_ready") from exc
        except Exception as exc:
            # Unknown/failed observations grant no access, but are not positive
            # mismatch evidence. Preserve occupancy and the original deadlines.
            raise Refused("startup_observation_unavailable") from exc
        with broker.transaction() as (conn, now):
            _group(broker, conn, now, lease_id, principal, run_id)
            conn.execute(
                "UPDATE broker_launches SET identity=? WHERE lease_id=?",
                (json.dumps(identity), lease_id),
            )
        return dict(lease_id=lease_id, identity=identity)


def _exact(connection, size):
    result = bytearray()
    while len(result) < size:
        part = connection.recv(size - len(result))
        if not part:
            raise ValueError("short shared ADB response")
        result.extend(part)
    return bytes(result)


def guest_identity(serial):
    """Raw smart sockets cannot auto-start ADB; there is no general command API."""
    result = {}
    for name, prop in (
        ("avd_name", "ro.boot.qemu.avd_name"),
        ("api", "ro.build.version.sdk"),
        ("abi", "ro.product.cpu.abi"),
        ("boot_complete", "sys.boot_completed"),
    ):
        with socket.create_connection(("127.0.0.1", 5037), timeout=3) as connection:
            for service in (f"host:transport:{serial}", f"shell:getprop {prop}"):
                data = service.encode("ascii")
                connection.sendall(f"{len(data):04x}".encode() + data)
                if _exact(connection, 4) != b"OKAY":
                    raise ValueError("scoped identity probe refused")
            data = bytearray()
            while True:
                part = connection.recv(1024)
                if not part:
                    break
                data.extend(part)
                if len(data) > 4096:
                    raise ValueError("oversized identity probe")
            result[name] = data.decode("utf-8").strip()
    result["api"] = int(result["api"])
    return result


def _ini(path):
    return dict(
        line.split("=", 1)
        for line in Path(path).read_text().splitlines()
        if "=" in line and not line.startswith(("#", ";"))
    )


class LinuxStartupIO:
    """Linux owner adapter, opt-in through composition after separate admission.

    host_preflight supplies fresh ops-owned listener identity, not a holder claim.
    Process inspection fails closed on inaccessible /proc entries. No adoption,
    signals, port fallback, userdata reset or ADB lifecycle commands exist here.
    """

    def __init__(self, host_preflight):
        self.host_preflight = host_preflight
        self._paused = {}
        self._running = {}

    @staticmethod
    def socket_owners():
        inodes = {}
        for protocol in ("tcp", "tcp6"):
            for line in Path(f"/proc/net/{protocol}").read_text().splitlines()[1:]:
                fields = line.split()
                # Kernel TIME_WAIT records have no live socket or owning fd.
                # Keep bound/listening sockets with an unseen owner as -1 below.
                if fields[9] == "0":
                    continue
                # Include bound non-listeners, too: any occupant blocks launch.
                port = int(fields[1].rsplit(":", 1)[1], 16)
                inodes.setdefault(fields[9], set()).add(port)
        inode_pids = {}
        for directory in Path("/proc").glob("[0-9]*"):
            try:
                for fd in (directory / "fd").iterdir():
                    target = os.readlink(fd)
                    if target.startswith("socket:["):
                        inode_pids.setdefault(target[8:-1], set()).add(int(directory.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                raise ValueError("startup requires full host /proc visibility") from exc
        # Unattributed sockets still occupy the port; never treat them as free.
        result = {}
        for inode, ports in inodes.items():
            for port in ports:
                result.setdefault(port, set()).update(inode_pids.get(inode, {-1}))
        return result

    @staticmethod
    def resource_users(device, *, userdata_only=False):
        found = set()
        for directory in Path("/proc").glob("[0-9]*"):
            try:
                for fd in (directory / "fd").iterdir():
                    path = os.readlink(fd)
                    if path == device.avd_data_path + "/userdata-qemu.img" or (
                        not userdata_only and path.startswith(device.avd_data_path + "/")
                    ):
                        found.add(int(directory.name))
                if not userdata_only:
                    args = (directory / "cmdline").read_bytes().split(b"\0")
                    if device.avd_data_path.encode() in args or any(
                        args[n : n + 2] == [b"-avd", device.avd_name.encode()]
                        for n in range(len(args))
                    ):
                        found.add(int(directory.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                raise ValueError("startup requires full host /proc visibility") from exc
        return found

    @staticmethod
    def pins(device):
        data = Path(device.avd_data_path)
        if str(data.resolve(strict=True)) != str(data) or not data.is_dir():
            raise StartupMismatch("AVD directory replaced")
        if _ini(device.avd_definition_path).get("path") != str(data):
            raise StartupMismatch("AVD definition mismatch")
        config = data / "config.ini"
        image_path = _ini(config).get("image.sysdir.1")
        if not image_path:
            raise StartupMismatch("system image path missing")
        image_dir = Path(image_path)
        if not image_dir.is_absolute():
            image_dir = Path(device.emulator_path).parents[1] / image_dir
        for path, digest in (
            (Path(device.emulator_path), device.emulator_sha256),
            (config, device.config_sha256),
            (image_dir / "system.img", device.image_sha256),
        ):
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise StartupMismatch("pinned launch artifact changed")
        userdata = data / "userdata-qemu.img"
        if str(userdata.resolve(strict=True)) != str(userdata) or not userdata.is_file():
            raise StartupMismatch("existing canonical userdata required")

    def listener(self):
        expected = self.host_preflight()
        if process_identity(expected["pid"]) != expected or self.socket_owners().get(5037) != {
            expected["pid"]
        }:
            raise ValueError("shared ADB listener absent or uncertain")

    def preflight(self, device, journal=None):
        self.listener()
        self.pins(device)
        allowed = set()
        if journal is not None:
            for row in _journal(journal).values():
                if process_identity(row["pid"]) != row:
                    raise ValueError("paused process replaced")
                allowed.add(row["pid"])
        owners = self.socket_owners()
        if (
            any(owners.get(port) for port in (device.console_port, device.adb_port))
            or self.resource_users(device) - allowed
        ):
            raise ValueError("AVD or fixed ports already owned")

    def prepare(self, device):
        args = [
            device.emulator_path,
            "-avd",
            device.avd_name,
            "-datadir",
            device.avd_data_path,
            "-data",
            device.avd_data_path + "/userdata-qemu.img",
            "-ports",
            f"{device.console_port},{device.adb_port}",
            "-no-snapshot",
            "-no-snapshot-load",
        ]
        # Adapt EmulatorManager's Popen primitive; no first-emulator selection or
        # background boot tracker. A separate, single-threaded worker forks safely.
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
            env=dict(os.environ, ANDROID_AVD_HOME=str(Path(device.avd_definition_path).parent)),
        )
        try:
            if not select.select([proc.stdout], [], [], 3)[0]:
                raise ValueError("paused worker handshake timeout")
            journal = _journal(json.loads(proc.stdout.readline(8193)))
            if journal["worker"] != process_identity(proc.pid) or journal[
                "child"
            ] != process_identity(journal["child"]["pid"]):
                raise ValueError("paused worker identity mismatch")
            self._paused[proc.pid] = proc
            return journal
        except BaseException:
            proc.stdin.close()  # EOF fails closed; no execution and no signal.
            proc.wait(timeout=5)
            proc.stdout.close()
            raise

    def resume(self, journal):
        proc = self._paused.pop(journal["worker"]["pid"])
        try:
            if (
                process_identity(proc.pid) != journal["worker"]
                or process_identity(journal["child"]["pid"]) != journal["child"]
            ):
                raise ValueError("paused identity replaced")
            proc.stdin.write("G")
            proc.stdin.flush()
            self._running[proc.pid] = proc
        finally:
            proc.stdin.close()
            proc.stdout.close()
        # Worker owns/reaps its emulator child; broker recovery owns its journal.

    def abort(self, journal):
        proc = self._paused.pop(journal["worker"]["pid"])
        proc.stdin.close()
        proc.wait(timeout=5)
        proc.stdout.close()

    def process_evidence(self, device, journal):
        self.listener()
        self.pins(device)
        try:
            if any(process_identity(row["pid"]) != row for row in journal.values()):
                raise StartupMismatch("owned process replaced")
        except (FileNotFoundError, ProcessLookupError) as exc:
            raise StartupMismatch("owned process exited") from exc
        child = Path(f"/proc/{journal['child']['pid']}")
        parent = int((child / "stat").read_text().rsplit(")", 1)[1].split()[1])
        args = (child / "cmdline").read_bytes().split(b"\0")
        for flag, value in (
            ("-avd", device.avd_name),
            ("-datadir", device.avd_data_path),
            ("-data", device.avd_data_path + "/userdata-qemu.img"),
            ("-ports", f"{device.console_port},{device.adb_port}"),
        ):
            if not any(
                args[n : n + 2] == [flag.encode(), value.encode()] for n in range(len(args))
            ):
                raise StartupMismatch("emulator command line mismatch")
        owners = self.socket_owners()
        expected = {journal["child"]["pid"]}
        users = self.resource_users(device, userdata_only=True)
        all_users = self.resource_users(device)
        ports = [owners.get(port, set()) for port in (device.console_port, device.adb_port)]
        if (
            parent != journal["worker"]["pid"]
            or users - expected
            or any(pids - expected - {-1} for pids in ports)
            or all_users - (expected | {journal["worker"]["pid"]})
        ):
            raise StartupMismatch("emulator data/socket ownership mismatch")
        if any(-1 in pids for pids in ports):
            raise ValueError("emulator socket ownership uncertain")
        if users != expected or any(pids != expected for pids in ports):
            raise StartupNotReady("emulator has not opened userdata or fixed sockets")
        return dict(
            **journal,
            parent_pid=parent,
            data_path=device.avd_data_path,
            userdata=device.avd_data_path + "/userdata-qemu.img",
            console_port=device.console_port,
            adb_port=device.adb_port,
            socket_pids=sorted(expected),
            userdata_pids=sorted(users),
        )

    def observe(self, device, journal):
        evidence = self.process_evidence(device, journal)
        serial = f"emulator-{device.console_port}"
        if devices().get(serial) != "device":
            raise StartupNotReady("exact serial not observed as device")
        guest = guest_identity(serial)
        # Recheck after probes, too, so a replacement during I/O cannot be cached.
        if self.process_evidence(device, journal) != evidence:
            raise StartupMismatch("process changed during identity probes")
        return dict(**evidence, **guest, serial=serial, device_state="device")


def _worker(args):
    """One owned parent and an exec-preserved child, both blocked before journal."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)
        os.close(0)  # Only the worker may consume the broker's gate.
        if (
            select.select([read_fd], [], [], PAUSE_TIMEOUT_SECONDS)[0]
            and os.read(read_fd, 1) == b"G"
        ):
            os.close(read_fd)
            null_fd = os.open(os.devnull, os.O_RDWR)
            os.dup2(null_fd, 0)
            # open() may return fd 0 with CLOEXEC; dup2(0, 0) keeps that flag.
            os.set_inheritable(0, True)
            os.dup2(null_fd, 1)
            if null_fd > 2:
                os.close(null_fd)
            env = dict(
                os.environ, ANDROID_ADB_SERVER_PORT="5037", ADB_SERVER_SOCKET="tcp:127.0.0.1:5037"
            )
            os.execve(args[0], args, env)
        os._exit(1)
    os.close(read_fd)
    try:
        print(
            json.dumps(dict(worker=process_identity(os.getpid()), child=process_identity(pid))),
            flush=True,
        )
        if select.select([sys.stdin], [], [], PAUSE_TIMEOUT_SECONDS)[0] and os.read(0, 1) == b"G":
            os.write(write_fd, b"G")
    finally:
        os.close(write_fd)
        os.waitpid(pid, 0)


if __name__ == "__main__" and sys.argv[1:2] == ["--worker"]:
    _worker(sys.argv[2:])
