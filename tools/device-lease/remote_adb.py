"""R1 owner API. No SSH daemon, host firewall or device mutation is installed here.

The owner must hold the broker's exclusive daemon lock. `resolve` authenticates
the control principal/live run and returns a CURRENT trusted admission snapshot;
it must return None on control loss. No request-provided policy is accepted.
The ops adapter owns effective SSH/isolation policy and exact session identities.
Only public keys are accepted/stored. The cooperative local RPC cannot call this.
"""

import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import struct
import time
import uuid

from artemis.runtime.adb_gateway import Gateway
from artemis.runtime.host_mux import ByteBudget, PROCESS_BUDGET
from artemis.runtime.host_peer import SocketRemote
from artemis.runtime.host_protocol import CONTRACT, ProtocolError
from artemis.runtime.stream_owner import StreamOwner

IDENTITY = ("principal", "agent", "issue", "run", "lease")
SSH_SERVER_POLICY = """AllowTcpForwarding local
AllowStreamLocalForwarding no
MaxSessions 0
PermitTunnel no
AllowAgentForwarding no
X11Forwarding no
PermitTTY no
GatewayPorts no
"""


def public_key(value):
    """One canonical Ed25519 key; never accept authorized_keys options/comments."""
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("Invalid public key")
    kind, encoded = value.split(" ")
    blob = base64.b64decode(encoded, validate=True)
    name = b"ssh-ed25519"
    if kind != name.decode() or blob[:19] != struct.pack("!I", 11) + name + struct.pack("!I", 32):
        raise ValueError("Invalid public key")
    if len(blob) != 51 or base64.b64encode(blob).decode() != encoded:
        raise ValueError("Invalid public key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return fingerprint, value


def snapshot(value, identity, now):
    """Validate a trusted snapshot structurally; review/grant evidence is owner-supplied.

    profile binds the reviewed attestation profile/build/service/grant/verifier;
    baseline binds the independent pre-lease state digests. `isolation` binds
    the ops-reviewed SSH/UID/namespace/USB/device-network evidence. These are not
    holder assertions. Without a complete reviewed profile resolve MUST return None.
    """
    if not isinstance(identity, dict) or set(identity) != set(IDENTITY):
        raise ValueError("Invalid identity")
    if not isinstance(value, dict) or any(value.get(k) != identity[k] for k in IDENTITY):
        raise ValueError("Invalid binding")
    if any(not isinstance(identity[k], str) or not 0 < len(identity[k]) <= 200 for k in IDENTITY):
        raise ValueError("Invalid identity")
    if not (
        value.get("state") == "active"
        and value.get("live") is True
        and value.get("restricted") is False
        and value.get("granted") is True
    ):
        raise ValueError("Not admitted")
    if type(value.get("generation")) is not int or value["generation"] < 1:
        raise ValueError("Unknown authority")
    if any(
        not isinstance(value.get(k), str) or not value[k]
        for k in ("profile", "baseline", "isolation")
    ):
        raise ValueError("Admission evidence unavailable")
    expires = value.get("expires")
    if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= now:
        raise ValueError("Expired")
    cap = value.get("hard_deadline")
    if type(cap) not in (int, float) or not math.isfinite(cap) or expires > cap:
        raise ValueError("Hard cap exceeded")
    devices = value.get("devices")
    if not isinstance(devices, (tuple, list)) or not 0 < len(devices) <= CONTRACT.max_streams:
        raise ValueError("Invalid mapping")
    for device in devices:
        if not isinstance(device, (tuple, list)) or len(device) != 3:
            raise ValueError("Invalid mapping")
        if any(not isinstance(v, str) or not v or len(v) > 200 for v in device):
            raise ValueError("Invalid mapping")
        if device[0] == "500c592" or device[1] == "500c592":
            raise ValueError("Restricted device")
    if len({d[0] for d in devices}) != len(devices) or len({d[1] for d in devices}) != len(devices):
        raise ValueError("Ambiguous mapping")
    return json.loads(json.dumps(value, allow_nan=False))


class BoundedReader(asyncio.StreamReader):
    """Bound bytes before StreamReader buffers them; this is not an ADB parser."""

    def __init__(self):
        super().__init__(limit=8192)

    def feed_data(self, data):
        if len(self._buffer) + len(data) > CONTRACT.max_text + 4:
            self.set_exception(ProtocolError("Receive budget exceeded"))
            if self._transport is not None:
                self._transport.abort()
            return
        super().feed_data(data)


async def listen_loopback(callback, host, port, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.create_server(
        lambda: asyncio.StreamReaderProtocol(BoundedReader(), callback),
        host,
        port,
        backlog=CONTRACT.max_streams,
    )


async def connect_adb():
    loop = asyncio.get_running_loop()
    reader = BoundedReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.create_connection(lambda: protocol, "127.0.0.1", 5037)
    transport.set_write_buffer_limits(high=0, low=0)
    return SocketRemote(reader, asyncio.StreamWriter(transport, protocol, reader, loop))


class LeaseGateway(Gateway):
    """Lease-specific profile of the shared smart-socket gateway."""

    def __init__(self, shared, *, authorize=None):
        super().__init__(shared, r1=True, authorize=authorize)


class RemoteAccess:
    """Single event-loop owner; SQLite tombstones survive all lease incarnations.

    bootstrap=True is ONLY for an explicitly new, protected deployment store.
    An ordinary startup never creates missing history. Startup reconciliation
    remains closed until exact old SSH session/listener closure is confirmed.
    """

    def __init__(
        self,
        path,
        *,
        resolve,
        ops,
        quarantine,
        clock=time.time,
        listen=listen_loopback,
        connect=connect_adb,
        bootstrap=False,
    ):
        self.path = Path(path).absolute()
        self.resolve, self.ops, self.quarantine = resolve, ops, quarantine
        self.clock, self.listen, self.connect = clock, listen, connect
        self.incarnation = uuid.uuid4().hex
        self.lock = asyncio.Lock()
        self.tunnels = {}
        self.dead = set()
        self.closed = False
        self.monitor = None
        self.ready = False
        if bootstrap:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with self.db() as conn:
                conn.executescript("""
                    CREATE TABLE r1_keys (
                        id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                        identity TEXT NOT NULL, snapshot TEXT NOT NULL,
                        incarnation TEXT NOT NULL, port INTEGER, phase TEXT NOT NULL);
                    CREATE TABLE r1_sessions (
                        identity TEXT PRIMARY KEY, binding TEXT NOT NULL);
                    CREATE TABLE r1_owner (incarnation TEXT NOT NULL);
                """)
                conn.execute("INSERT INTO r1_owner VALUES (?)", (self.incarnation,))
        with self.db() as conn:
            # A new broker incarnation immediately invalidates every old binding.
            conn.execute("UPDATE r1_owner SET incarnation=?", (self.incarnation,))
            self.ready = (
                conn.execute("SELECT 1 FROM r1_keys WHERE phase!='closed'").fetchone() is None
            )

    @contextmanager
    def db(self):
        info = self.path.lstat()
        parent = self.path.parent.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise ValueError("Credential store must be private")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def row(self, binding):
        with self.db() as conn:
            return conn.execute("SELECT * FROM r1_keys WHERE id=?", (binding,)).fetchone()

    def current(self, identity):
        return snapshot(self.resolve(dict(identity)), identity, self.clock())

    def valid(self, binding):
        if self.closed or not self.ready or binding in self.dead:
            return False
        try:
            row = self.row(binding)
            with self.db() as conn:
                owner = conn.execute("SELECT incarnation FROM r1_owner").fetchone()[0]
            if (
                row is None
                or row["phase"] != "active"
                or owner != self.incarnation
                or row["incarnation"] != self.incarnation
            ):
                self.dead.add(binding)
                return False
            current = self.current(json.loads(row["identity"]))
            # Exact tuple: renew requires explicit fresh-key rotation, never silent extension.
            if current == json.loads(row["snapshot"]):
                tunnel = self.tunnels.get(binding)
                if tunnel is not None and asyncio.get_running_loop().time() < tunnel.deadline:
                    return True
        except Exception:
            pass
        self.dead.add(binding)
        return False

    async def issue(self, identity, credential):
        async with self.lock:
            if not self.ready or self.closed:
                return None
            binding = None
            try:
                current = self.current(identity)
                fingerprint, key = public_key(credential)
                serialized = json.dumps(identity, sort_keys=True)
                with self.db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    old = conn.execute(
                        "SELECT * FROM r1_keys WHERE fingerprint=?", (fingerprint,)
                    ).fetchone()
                    if old is not None:
                        if old["identity"] == serialized and self.valid(old["id"]):
                            return {"id": old["id"], "port": old["port"]}
                        return None
                    if conn.execute(
                        "SELECT 1 FROM r1_keys WHERE identity=? AND phase!='closed'", (serialized,)
                    ).fetchone():
                        return None  # rotate only after the old access is fenced
                    binding = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO r1_keys VALUES (?,?,?,?,?,NULL,'preparing')",
                        (
                            binding,
                            fingerprint,
                            serialized,
                            json.dumps(current, sort_keys=True),
                            self.incarnation,
                        ),
                    )
                # All ops/listener I/O is outside the SQLite writer transaction.
                port = await self.ops.reserve(binding)
                if type(port) is not int or not 1024 <= port <= 65535 or port == 5037:
                    raise ValueError("Invalid reserved destination")
                with self.db() as conn:
                    if conn.execute(
                        "SELECT 1 FROM r1_keys WHERE port=? AND phase!='closed'", (port,)
                    ).fetchone():
                        raise ValueError("Destination still owned")
                    conn.execute("UPDATE r1_keys SET port=? WHERE id=?", (port, binding))
                tunnel = LeaseTunnel(self, binding, current)
                self.tunnels[binding] = tunnel
                tunnel.listener = await self.listen(
                    lambda r, w: self.client(binding, r, w),
                    "127.0.0.1",
                    port,
                )
                line = f'restrict,port-forwarding,permitopen="127.0.0.1:{port}" {key}'
                if await self.ops.publish(binding, line) is not True:
                    raise ValueError("SSH publication unconfirmed")
                if self.current(identity) != current:
                    raise ValueError("Admission changed")
                with self.db() as conn:
                    conn.execute("UPDATE r1_keys SET phase='active' WHERE id=?", (binding,))
                tunnel.arm()
                if self.monitor is None:
                    self.monitor = asyncio.create_task(self._monitor())
                return {"id": binding, "port": port}
            except Exception:
                if binding is not None:
                    await self._fence(binding)
                return None

    async def authenticate(self, identity, credential, session):
        """Ops SSH-auth callback AFTER proof of private-key possession.

        identity is resolved by trusted control auth, never a network source.
        session is observed by the supervisor (pid/start/boot), not client data.
        Persist BEFORE auth success; the supervisor fails closed if this fails.
        """
        async with self.lock:
            try:
                fingerprint, _ = public_key(credential)
                with self.db() as conn:
                    row = conn.execute(
                        "SELECT * FROM r1_keys WHERE fingerprint=?", (fingerprint,)
                    ).fetchone()
                    if (
                        row is None
                        or not self.valid(row["id"])
                        or json.loads(row["identity"]) != identity
                    ):
                        return False
                    if (
                        set(session) != {"pid", "start", "boot", "binding"}
                        or type(session["pid"]) is not int
                        or session["pid"] <= 1
                        or session["binding"] != row["id"]
                        or any(
                            not isinstance(session[k], str) or not session[k]
                            for k in ("start", "boot")
                        )
                    ):
                        return False
                    if (
                        conn.execute(
                            "SELECT COUNT(*) FROM r1_sessions WHERE binding=?", (row["id"],)
                        ).fetchone()[0]
                        >= CONTRACT.max_streams
                    ):
                        return False
                    conn.execute(
                        "INSERT OR IGNORE INTO r1_sessions VALUES (?,?)",
                        (
                            json.dumps(session, sort_keys=True),
                            row["id"],
                        ),
                    )
                return True
            except Exception:
                return False

    async def client(self, binding, reader, writer):
        tunnel = self.tunnels.get(binding)
        if tunnel is None:
            writer.close()
            await writer.wait_closed()
            return
        await tunnel.client(reader, writer)

    async def fence(self, binding):
        async with self.lock:
            return await self._fence(binding)

    async def _fence(self, binding):
        self.dead.add(binding)  # event-loop dispatch barrier, BEFORE any await
        tunnel = self.tunnels.get(binding)
        row, sessions, durable = None, [], True
        try:
            row = self.row(binding)
            if row is None:
                raise ValueError("Missing session inventory")
            with self.db() as conn:
                conn.execute("UPDATE r1_keys SET phase='revoked' WHERE id=?", (binding,))
                sessions = [
                    json.loads(r[0])
                    for r in conn.execute(
                        "SELECT identity FROM r1_sessions WHERE binding=?", (binding,)
                    )
                ]
        except Exception:
            self.ready = False
            durable = False
        # Loss of history must never prevent closing observable local streams.
        if tunnel is not None:
            try:
                durable = (await asyncio.wait_for(tunnel.close(), 5) is True) and durable
            except Exception:
                durable = False
        ok = durable
        try:
            ok = (await asyncio.wait_for(self.ops.revoke(binding), 5) is True) and ok
            for session in sessions:
                # Adapter validates pid/start/boot before stopping; no executable-name kill.
                ok = (await asyncio.wait_for(self.ops.stop(session), 5) is True) and ok
            if ok and row is not None and row["port"] is not None:
                ok = await asyncio.wait_for(self.ops.retire(binding, row["port"]), 5) is True
        except Exception:
            ok = False
        if ok:
            with self.db() as conn:
                conn.execute("DELETE FROM r1_sessions WHERE binding=?", (binding,))
                conn.execute("UPDATE r1_keys SET phase='closed' WHERE id=?", (binding,))
        else:
            lease = (
                json.loads(row["identity"])["lease"]
                if row
                else tunnel.admitted["lease"]
                if tunnel
                else None
            )
            self.quarantine(lease)
        return ok

    async def maintain(self):
        try:
            with self.db() as conn:
                active = [r[0] for r in conn.execute("SELECT id FROM r1_keys WHERE phase='active'")]
        except Exception:
            self.ready = False
            active = list(self.tunnels)
        for binding in active:
            if not self.valid(binding):
                await self.fence(binding)

    async def _monitor(self):
        while not self.closed:
            await asyncio.sleep(0.05)
            await self.maintain()

    async def fence_lease(self, lease):
        """Owner event hook for release/revoke/policy/authority/control-loss barriers."""
        with self.db() as conn:
            bindings = [
                r["id"]
                for r in conn.execute("SELECT id,identity FROM r1_keys WHERE phase!='closed'")
                if json.loads(r["identity"])["lease"] == lease
            ]
        results = [await self.fence(binding) for binding in bindings]
        return all(results)

    async def reconcile(self):
        async with self.lock:
            self.ready = False
            with self.db() as conn:
                pending = [
                    r[0] for r in conn.execute("SELECT id FROM r1_keys WHERE phase!='closed'")
                ]
            outcomes = [await self._fence(binding) for binding in pending]
            self.ready = all(outcomes)
            return self.ready

    async def close(self):
        self.closed = True
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        await self.reconcile()
        self.ready = False


class LeaseTunnel(StreamOwner):
    def __init__(self, access, binding, admitted):
        super().__init__()
        self.access, self.binding, self.admitted = access, binding, admitted
        self.budget = ByteBudget(CONTRACT.host_budget - CONTRACT.control_reserve)
        self.timer = None
        self.deadline = float("inf")
        self.closure_ok = True
        if not PROCESS_BUDGET.reserve(CONTRACT.control_reserve):
            raise ProtocolError("Process budget exceeded")
        self.control_reserved = True

    def arm(self):
        loop = asyncio.get_running_loop()
        # Monotonic deadline never moves with a wall-clock rollback or renew.
        delay = max(0, self.admitted["expires"] - self.access.clock())
        self.deadline = loop.time() + delay
        self.timer = loop.call_later(
            delay, lambda: loop.create_task(self.access.fence(self.binding))
        )

    async def client(self, reader, writer):
        task = asyncio.current_task()
        remote = None
        reserved = False
        self.tasks.add(task)
        self.clients[writer] = None
        try:
            with self.access.db() as conn:
                authenticated = conn.execute(
                    "SELECT 1 FROM r1_sessions WHERE binding=?", (self.binding,)
                ).fetchone()
            if (
                not authenticated
                or not self.access.valid(self.binding)
                or len(self.clients) > CONTRACT.max_streams
            ):
                return
            # Conservative admission reserves a full stream window BEFORE connect/read.
            # The shared process control reserve is unavailable to data streams.
            if not self.budget.reserve(CONTRACT.stream_credit):
                return
            if not PROCESS_BUDGET.reserve(CONTRACT.stream_credit):
                self.budget.release(CONTRACT.stream_credit)
                return
            reserved = True
            if hasattr(writer, "transport"):
                writer.transport.set_write_buffer_limits(high=0, low=0)
            remote = await asyncio.wait_for(self.access.connect(), 5)
            self.client_streams[writer] = remote
            gateway = LeaseGateway(
                lambda: {d[1] for d in self.admitted["devices"]},
                authorize=lambda: self.access.valid(self.binding),
            )
            self.clients[writer] = gateway
            await gateway.relay(reader, writer, remote)
        except (ConnectionError, OSError, ValueError, TimeoutError):
            pass  # payloads and exception strings never enter logs
        finally:
            try:
                if remote is not None and hasattr(remote, "writer"):
                    remote.writer.close()
                    await asyncio.wait_for(remote.writer.wait_closed(), 5)
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 5)
            except (OSError, ConnectionError, TimeoutError):
                self.closure_ok = False
            finally:
                self.clients.pop(writer, None)
                self.client_streams.pop(writer, None)
                writer.close()
                self.tasks.discard(task)
                if reserved:
                    self.budget.release(CONTRACT.stream_credit)
                    PROCESS_BUDGET.release(CONTRACT.stream_credit)

    async def close(self):
        if self.timer is not None:
            self.timer.cancel()
        try:
            self.closure_ok = (await self.close_streams()) and self.closure_ok
            return self.closure_ok
        finally:
            if self.control_reserved:
                PROCESS_BUDGET.release(CONTRACT.control_reserve)
                self.control_reserved = False
