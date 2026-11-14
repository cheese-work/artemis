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
import subprocess
import sys
import threading

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
    def __init__(self, state_dir, broker, *, offer_worker=None):
        self.state_dir, self.broker = Path(state_dir), broker
        self.offer_worker = offer_worker
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
            "park": self.broker.park,
            "claim": self.broker.claim,
            "inventory": self.broker.inventory,
        }
        if not isinstance(request["op"], str) or request["op"] not in methods:
            raise Refused("invalid_request")
        method = methods[request["op"]]
        if not isinstance(request["args"], dict):
            raise Refused("invalid_request")
        if request["op"] in {"acquire", "claim"}:
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
        if self.offer_worker is not None:
            self.offer_worker.run_once()

    def server_close(self):
        if hasattr(self, "socket"):
            super().server_close()
        if self._owns_socket:
            self.socket_path.unlink(missing_ok=True)
            self._owns_socket = False
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


class ControlServer(socketserver.UnixStreamServer):
    """Leader release control plane on its own private socket (CHE-5297).

    Only release verification and quarantine recovery evidence; the holder
    socket never exposes them. Same-UID callers stay cooperative: the separate
    0700 directory is the boundary, the verifier string is not a credential.
    """

    def __init__(self, socket_path, broker):
        self.broker, self.socket_path = broker, Path(socket_path)
        self._owns_socket = False
        parent = self.socket_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("control directory must be owned by this user and mode 0700")
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                raise RuntimeError("control path is not a socket")
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.connect(str(self.socket_path))
                except ConnectionRefusedError:
                    self.socket_path.unlink()
                else:
                    raise RuntimeError("control socket already has an owner")
        super().__init__(str(self.socket_path), Handler, bind_and_activate=False)
        try:
            self.server_bind()
            self._owns_socket = True
            os.chmod(self.socket_path, 0o600)
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {"op", "args"}:
            raise Refused("invalid_request")
        methods = {
            "verify-release": self.broker.verify_release,
            "recover-quarantine": self.broker.recover_quarantine,
            "record-change": self.broker.record_change,
        }
        if not isinstance(request["op"], str) or request["op"] not in methods:
            raise Refused("invalid_request")
        if not isinstance(request["args"], dict):
            raise Refused("invalid_request")
        method = methods[request["op"]]
        try:
            inspect.signature(method).bind(**request["args"])
        except TypeError as exc:
            raise Refused("invalid_request") from exc
        return method(**request["args"])

    def server_close(self):
        super().server_close()
        if self._owns_socket:
            self.socket_path.unlink(missing_ok=True)
            self._owns_socket = False


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
    parser = argparse.ArgumentParser(description="DLP-1 local broker and owner recovery")
    parser.add_argument("--socket", type=Path, default=DEFAULT_STATE / "broker.sock")
    parser.add_argument("--control-socket", type=Path, help="leader control plane socket")
    sub = parser.add_subparsers(dest="op", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    serve.add_argument("--registry", type=Path)
    serve.add_argument("--offer-property-id")
    serve.add_argument("--multica-profile", help="dedicated ops-owned CLI profile")
    serve.add_argument(
        "--composition",
        type=Path,
        help="owner-only JSON naming every live admission and recovery input",
    )
    verify = sub.add_parser("verify-release", help="leader control plane only")
    verify.add_argument("--lease-id", required=True)
    verify.add_argument("--verifier", required=True)
    verify.add_argument("--evidence", required=True)
    verdict = verify.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--passed", dest="passed", action="store_true")
    verdict.add_argument("--failed", dest="passed", action="store_false")
    recover = sub.add_parser("recover-quarantine", help="leader control plane only")
    recover.add_argument("--lease-id", required=True)
    recover.add_argument("--recovery-owner", required=True)
    recover.add_argument("--evidence", required=True)
    record = sub.add_parser("record-change", help="owner control: journal intent before execution")
    record.add_argument("--lease-id", required=True)
    record.add_argument("--principal", required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--change-file", type=Path, required=True)
    for operation in (
        "write-readiness",
        "census-android-claims",
        "claim-android",
        "release-android",
        "list-android-claims",
    ):
        owner = sub.add_parser(operation, help="owner-only source operation under the shared guard")
        owner.add_argument("--composition", type=Path, required=True)
        owner.add_argument("--registry", type=Path, required=True)
        owner.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        if operation in {"write-readiness", "census-android-claims"}:
            owner.add_argument("--input", type=Path, required=True)
        if operation in {"claim-android", "release-android"}:
            owner.add_argument("--owner", required=True)
            owner.add_argument("--run-id", required=True)
        if operation == "claim-android":
            owner.add_argument("--device-id", required=True)
            owner.add_argument("--reference", required=True)
        if operation == "release-android":
            owner.add_argument("--claim-id", required=True)
    wait = sub.add_parser("wait", help="one fallback poll, condition wakeup and final comment")
    for flag in ("principal", "agent-id", "issue-id", "ticket", "offer-property-id"):
        wait.add_argument(
            f"--{flag}",
            required=True,
            dest="ticket_id" if flag == "ticket" else flag.replace("-", "_"),
        )
    wait.add_argument(
        "--wakeup-block-file", type=Path, help="actual run [WAKEUP] block, if present"
    )
    sub.add_parser("inventory", help="owner fleet readback: eligibility, reason, lease")
    for op in (
        "acquire",
        "renew",
        "release",
        "status",
        "heartbeat",
        "ticket-status",
        "cancel-ticket",
        "park",
        "claim",
    ):
        command = sub.add_parser(op)
        command.add_argument("--principal", required=True)
        if op in ("ticket-status", "cancel-ticket", "park", "claim"):
            command.add_argument("--ticket", "--ticket-id", dest="ticket_id", required=True)
            command.add_argument("--agent-id", required=True)
            command.add_argument("--issue-id", required=True)
            if op in {"park", "claim"}:
                command.add_argument("--run-id", required=True)
            if op == "claim":
                command.add_argument("--pid", type=int, required=True)
                command.add_argument("--idempotency-key", required=True)
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
    control_socket = args.pop("control_socket")
    from compose import CompositionError

    try:
        if op == "serve":
            registry = (
                DeviceRegistry.load(args["registry"]) if args["registry"] else DeviceRegistry()
            )
            composed = None
            if args["composition"]:
                from compose import compose

                # Refuse before creating state or binding any socket.
                composed = compose(
                    args["composition"], registry, db_path=args["state_dir"] / "leases.sqlite3"
                )
            state = args["state_dir"]
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Without --composition the broker stays closed: no readiness, readers,
            # guard or recovery adapter, so every acquire is admission_unavailable.
            broker = Broker(
                state / "leases.sqlite3", registry, **(composed["broker"] if composed else {})
            )
            if bool(args["multica_profile"]) != bool(args["offer_property_id"]):
                raise ValueError("offer property and ops profile must be configured together")
            from resume import MulticaClient, OfferWorker

            # Validate private directory/socket ownership before enabling the capability.
            with BrokerServer(state, broker) as server:
                control = (
                    ControlServer(composed["leader_control_socket"], broker) if composed else None
                )
                try:
                    if control is not None:
                        control_worker = threading.Thread(target=control.serve_forever)
                        control_worker.start()
                    if args["multica_profile"]:
                        client = MulticaClient(
                            profile=args["multica_profile"],
                            audit_path=state / "capabilities.jsonl",
                        )
                        server.offer_worker = OfferWorker(broker, client, args["offer_property_id"])
                    server.serve_forever()
                finally:
                    if control is not None:
                        control.shutdown()
                        control_worker.join()
                        control.server_close()
            return 0
        if op in {
            "write-readiness",
            "census-android-claims",
            "claim-android",
            "release-android",
            "list-android-claims",
        }:
            from compose import compose, write_readiness, _owner_file

            _owner_file(args["registry"])
            registry = DeviceRegistry.load(args["registry"])
            state = args["state_dir"]
            composed = compose(args["composition"], registry, db_path=state / "leases.sqlite3")
            # Initialize broker tables. No maintenance worker or device I/O here.
            Broker(state / "leases.sqlite3", registry, **composed["broker"]).inventory()
            ledger = composed["ledger"]
            if op == "write-readiness":
                result = write_readiness(composed, args["input"], registry)
            elif op == "census-android-claims":
                _owner_file(args["input"])
                payload = json.loads(args["input"].read_text())
                if not isinstance(payload, dict) or set(payload) != {
                    "generation",
                    "observed_at",
                    "complete",
                    "writers",
                    "claims",
                }:
                    raise ValueError("invalid census")
                result = ledger.census(**payload)
            elif op == "claim-android":
                result = {
                    "claim_id": ledger.claim(
                        args["device_id"],
                        owner=args["owner"],
                        run_id=args["run_id"],
                        reference=args["reference"],
                    )
                }
            elif op == "release-android":
                ledger.release(args["claim_id"], owner=args["owner"], run_id=args["run_id"])
                result = {"released": True}
            else:
                result = ledger.snapshot()
            print(json.dumps(result))
            return 0
        if op == "record-change":
            from compose import _owner_file

            path = args.pop("change_file")
            _owner_file(path)
            args["change"] = json.loads(path.read_text())
        if op in {"verify-release", "recover-quarantine", "record-change"}:
            if control_socket is None:
                raise ValueError("leader operations need --control-socket")
            response = rpc(control_socket, {"op": op, "args": args})
            print(json.dumps(response))
            return 1 if "code" in response else 0
        if op == "wait":
            from resume import MulticaClient, fallback_wait

            class TicketClient:
                def ticket_status(self, **identity):
                    result = rpc(socket_path, {"op": "ticket-status", "args": identity})
                    if "code" in result:
                        raise Refused(result["code"])
                    return result

            block_file = args.pop("wakeup_block_file")
            response = fallback_wait(
                TicketClient(),
                MulticaClient(),
                args.pop("offer_property_id"),
                **args,
                wakeup_block=block_file.read_text() if block_file else "",
            )
            print(json.dumps(response))
            return 0
        if op in {"acquire", "claim"}:
            args["binding"] = process_identity(args.pop("pid"))
        response = rpc(socket_path, {"op": op, "args": args})
        print(json.dumps(response))
        return 1 if "code" in response else 0
    except KeyboardInterrupt:
        return 0
    except CompositionError as exc:
        # Field names only; never input paths, values or credentials.
        print(f"device-lease: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, Refused, subprocess.SubprocessError):
        print("device-lease: broker unavailable or invalid configuration", file=sys.stderr)
        return 2
