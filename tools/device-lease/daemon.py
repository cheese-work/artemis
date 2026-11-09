"""Private Unix-socket transport for the cooperative local DLP-1 authority.

Same-UID callers remain cooperative; principal/run labels are not credentials.
Remote authentication and isolation belong to slice 5. No device access here.
"""

import argparse
import fcntl
import inspect
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import sys

from broker import Broker, DeviceRegistry, Refused, process_identity

MAX_FRAME = 65536
DEFAULT_STATE = Path.home() / ".local/state/artemis-device-lease"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        try:
            data = self.rfile.readline(MAX_FRAME + 1)
            if len(data) > MAX_FRAME or not data.endswith(b"\n"):
                raise Refused("invalid_request")
            response = self.server.dispatch(json.loads(data))
        except Refused as exc:
            response = {"code": exc.code, **exc.details}
        except (ValueError, UnicodeError, TimeoutError):
            response = {"code": "invalid_request"}
        except Exception:
            # Log no request values, SQL, paths or exception text to clients.
            print("device-lease: request failed", file=sys.stderr)
            response = {"code": "internal_error"}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass  # committed results remain replayable with the same key


class BrokerServer(socketserver.UnixStreamServer):
    # ponytail: one bounded request at a time; add workers if measured load requires it.
    def __init__(self, state_dir, broker):
        self.state_dir, self.broker = Path(state_dir), broker
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("state directory must be owned by this user and mode 0700")
        self.socket_path = self.state_dir / "broker.sock"
        self.lock_fd = os.open(
            str(broker.db_path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.lock_fd)
            raise RuntimeError("broker database already has an owner") from exc
        self._owns_socket = False
        try:
            # A second live daemon cannot remove a socket while it owns this DB lock.
            if self.socket_path.exists():
                if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                    raise RuntimeError("socket path is not a socket")
                # Refuse a live endpoint even if it belongs to another DB.
                with socket.socket(socket.AF_UNIX) as probe:
                    try:
                        probe.connect(str(self.socket_path))
                    except ConnectionRefusedError:
                        self.socket_path.unlink()
                    else:
                        raise RuntimeError("broker socket already has an owner")
            super().__init__(str(self.socket_path), Handler, bind_and_activate=False)
            self.server_bind()
            self._owns_socket = True
            self.server_activate()
            os.chmod(self.socket_path, 0o600)
            broker.restart()
        except BaseException:
            self.server_close()
            raise

    def dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {"op", "args"}:
            raise Refused("invalid_request")
        methods = {
            "acquire": self.broker.acquire,
            "renew": self.broker.renew,
            "release": self.broker.release,
            "status": self.broker.status,
            "ticket-status": self.broker.ticket_status,
            "cancel-ticket": self.broker.cancel_ticket,
            "heartbeat": self.broker.heartbeat,
        }
        if not isinstance(request["op"], str) or request["op"] not in methods:
            raise Refused("invalid_request")
        method = methods[request["op"]]
        if not isinstance(request["args"], dict):
            raise Refused("invalid_request")
        if request["op"] == "acquire":
            binding = request["args"].get("binding")
            if not isinstance(binding, dict) or binding.get("mode") != "local":
                raise Refused("invalid_request")
        try:
            inspect.signature(method).bind(**request["args"])
        except TypeError as exc:
            raise Refused("invalid_request") from exc
        return method(**request["args"])

    def service_actions(self):
        # The owner loop stays foreground. It also maintains leases while clients are idle.
        self.broker.maintain()

    def server_close(self):
        if hasattr(self, "socket"):
            super().server_close()
        if self._owns_socket:
            self.socket_path.unlink(missing_ok=True)
            self._owns_socket = False
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def rpc(socket_path, request):
    """Clients touch only the socket, never the database."""
    encoded = json.dumps(request).encode() + b"\n"
    if len(encoded) > MAX_FRAME:
        raise ValueError("request is too large")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(15)
        client.connect(str(socket_path))
        client.sendall(encoded)
        response = client.makefile("rb").readline(MAX_FRAME + 1)
    if len(response) > MAX_FRAME or not response.endswith(b"\n"):
        raise ValueError("invalid broker response")
    return json.loads(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description="DLP-1 local broker (no ADB access)")
    parser.add_argument("--socket", type=Path, default=DEFAULT_STATE / "broker.sock")
    sub = parser.add_subparsers(dest="op", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    serve.add_argument("--registry", type=Path)
    for op in (
        "acquire",
        "renew",
        "release",
        "status",
        "heartbeat",
        "ticket-status",
        "cancel-ticket",
    ):
        command = sub.add_parser(op)
        command.add_argument("--principal", required=True)
        if op in ("ticket-status", "cancel-ticket"):
            command.add_argument("--ticket-id", required=True)
            command.add_argument("--agent-id", required=True)
            command.add_argument("--issue-id", required=True)
            continue
        command.add_argument("--run-id", required=True)
        if op == "acquire":
            command.add_argument("--pool", default="rig")
            command.add_argument("--device-id")
            command.add_argument("--count", type=int, default=1)
            command.add_argument("--purpose", required=True)
            command.add_argument("--agent-id", required=True)
            command.add_argument("--issue-id", required=True)
            command.add_argument(
                "--pid",
                type=int,
                required=True,
                help="live agent run PID (not this short-lived CLI process)",
            )
        else:
            command.add_argument("--lease-id", required=True)
        if op in ("acquire", "renew"):
            command.add_argument("--ttl", type=int, default=1800)
            command.add_argument("--idempotency-key", required=True)
    args = vars(parser.parse_args(argv))
    op, socket_path = args.pop("op"), args.pop("socket")
    try:
        if op == "serve":
            state = args["state_dir"]
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
            registry = (
                DeviceRegistry.load(args["registry"]) if args["registry"] else DeviceRegistry()
            )
            # Closed until ops composes trusted grants/readiness, both admission sources
            # with their shared guard, and a deadline-bounded recovery adapter.
            broker = Broker(state / "leases.sqlite3", registry)
            with BrokerServer(state, broker) as server:
                server.serve_forever()
            return 0
        if op == "acquire":
            args["binding"] = process_identity(args.pop("pid"))
        response = rpc(socket_path, {"op": op, "args": args})
        print(json.dumps(response))
        return 1 if "code" in response else 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError):
        print("device-lease: broker unavailable or invalid configuration", file=sys.stderr)
        return 2
