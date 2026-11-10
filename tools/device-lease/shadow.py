"""Offline SmartQA DLP-1 projection. No allocating API or broker write access.

Run explicitly after broker requests, in another process. The evaluator receives
immutable JSON bytes, not a client, connection, path, or admission capability.
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat

from broker import Broker, Refused
from registry import Device, DeviceRegistry


def smartqa_decide(value):
    """Compute the extension's proposed decision using only captured inputs.

    This is a pure projection, not a call to the legacy CHE-2099 allocator or
    the HTTP backend. Lease identifiers for new grants are deliberately absent.
    """
    data = json.loads(value)
    op, args, snapshot = data["operation"], data["request"], data["snapshot"]
    now, groups = snapshot["now"], snapshot["groups"]
    registry = DeviceRegistry(Device(**row) for row in snapshot["inventory"])
    try:
        if op == "recovery":
            expected, target = args["expected"], args["target"]
            if target == "quarantine" and expected in {"fencing", "cleaning", "verifying"}:
                if not args["recovery_owner"] or not args["ops_issue"]:
                    raise Refused("recovery_owner_required")
                Broker._text(args["recovery_owner"], args["ops_issue"])
            elif (expected, target) not in {("fencing", "cleaning"), ("cleaning", "verifying")}:
                raise Refused("invalid_transition")
        elif op in {"acquire", "renew", "release", "status"}:
            names = ["principal", "run_id"]
            names += ["purpose", "pool", "idempotency_key"] if op == "acquire" else ["lease_id"]
            if op == "renew":
                names += ["idempotency_key"]
            Broker._text(*(args[name] for name in names))
            if op in {"acquire", "renew"}:
                Broker._ttl(args["ttl"], 1800 if args.get("pool") == "root" else 3600)
            if op == "acquire":
                if type(args["count"]) is not int or args["count"] < 1:
                    raise Refused("invalid_request")
                if args["device_id"] is not None:
                    Broker._text(args["device_id"])
        else:
            raise ValueError("unsupported snapshot operation")

        for group in groups:
            if group["state"] == "active" and group["expires_at"] <= now:
                group["state"] = "fencing"
        group = None
        if op != "acquire":
            group = next((g for g in groups if g["lease_id"] == args["lease_id"]), None)
            if group is None:
                raise Refused("lease_unknown")
            if op != "recovery" and (group["holder"], group["run_id"]) != (
                args["principal"],
                args["run_id"],
            ):
                raise Refused("lease_not_yours")

        if op in {"acquire", "renew"}:
            target = registry.resolve(args.get("device_id"))
            fingerprint = json.dumps(
                [
                    "acquire",
                    args["pool"],
                    target.device_id if target else args["device_id"],
                    args["count"],
                    args["ttl"],
                    args["purpose"],
                ]
                if op == "acquire"
                else ["renew", args["lease_id"], args["ttl"]]
            )
            replay = next(
                (
                    r
                    for r in snapshot["requests"]
                    if (r["principal"], r["run_id"], r["key"])
                    == (args["principal"], args["run_id"], args["idempotency_key"])
                ),
                None,
            )
            if replay:
                if replay["fingerprint"] != fingerprint:
                    raise Refused("idempotency_conflict")
                return json.loads(replay["response"])

        if op == "acquire":
            if snapshot["draining"]:
                raise Refused("draining")
            aliases = [args["device_id"]] if args["device_id"] else snapshot["admitted"]
            candidates = {}
            for alias in aliases:
                device = registry.resolve(alias)
                if device is None:
                    continue
                endpoint = snapshot["admitted"].get(device.device_id)
                if (
                    device.pool == args["pool"]
                    and endpoint
                    and registry.resolve(endpoint) == device
                    and registry.eligible(endpoint, "device")
                ):
                    candidates[device.device_id] = device
            if args["count"] > len(candidates):
                raise Refused("impossible_batch")
            if any(
                (g["holder"], g["run_id"]) == (args["principal"], args["run_id"])
                and g["state"] != "released"
                for g in groups
            ):
                raise Refused("run_already_holds")
            holders = [
                g
                for g in groups
                if g["state"] != "released"
                and any(d["device_id"] in candidates for d in g["devices"])
            ]
            occupied = {d["device_id"] for g in holders for d in g["devices"]}
            free = [d for d in candidates if d not in occupied]
            if len(free) < args["count"]:
                raise Refused("device_busy", holder=min(holders, key=lambda g: g["expires_at"]))
            budget = next(
                (
                    r
                    for r in snapshot["runs"]
                    if (r["principal"], r["run_id"]) == (args["principal"], args["run_id"])
                ),
                None,
            )
            started = budget["started_at"] if budget else now
            deadline = started + (3600 if args["pool"] == "root" else 7200)
            if budget:
                deadline = min(deadline, budget["hard_deadline"])
            if now >= deadline:
                raise Refused("hard_cap")
            return dict(
                holder=args["principal"],
                run_id=args["run_id"],
                purpose=args["purpose"],
                started_at=started,
                expires_at=min(now + args["ttl"], deadline),
                ttl=args["ttl"],
                hard_deadline=deadline,
                state="active",
                recovery_owner=None,
                ops_issue=None,
                devices=[
                    dict(
                        device_id=d,
                        endpoint=snapshot["admitted"][d],
                        restricted=candidates[d].restricted,
                        space_epoch=None,
                    )
                    for d in sorted(free[: args["count"]])
                ],
            )
        if op == "renew":
            if now >= group["expires_at"]:
                raise Refused("lease_expired")
            if group["state"] != "active":
                raise Refused("lease_not_active")
            if snapshot["draining"]:
                raise Refused("draining")
            if group["expires_at"] - now > group["ttl"] / 3:
                raise Refused("renew_too_early")
            devices = [registry.resolve(d["device_id"]) for d in group["devices"]]
            if None in devices:
                raise Refused("device_unknown")
            Broker._ttl(args["ttl"], 1800 if any(d.pool == "root" for d in devices) else 3600)
            if snapshot["conflicting_waiter"]:
                raise Refused("conflicting_waiter")
            group.update(ttl=args["ttl"], expires_at=min(now + args["ttl"], group["hard_deadline"]))
        elif op == "release" and group["state"] == "active":
            group["state"] = "fencing"
        elif op == "recovery":
            if group["state"] != args["expected"]:
                raise Refused("state_conflict")
            group.update(
                state=args["target"],
                recovery_owner=args["recovery_owner"],
                ops_issue=args["ops_issue"],
            )
        return group
    except Refused as exc:
        return {"code": exc.code, **exc.details}


def compare(db_path, output, *, decide=smartqa_decide, after=0, limit=100):
    """Append JSONL comparisons; return the last sequence for explicit resume.

    Close the read-only connection before running any shadow code or file I/O.
    A stalled evaluator can delay only this offline invocation. It has no route
    back into a broker request. Exceptions are recorded without private text.
    """
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid comparison range")
    database, output = Path(db_path).resolve(), Path(output)
    if str(output.resolve()) in {
        str(database) + suffix for suffix in ("", "-wal", "-shm", "-journal")
    }:
        raise ValueError("comparison output must be separate from the broker database")
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT sequence, entry FROM broker_decisions WHERE sequence > ? ORDER BY sequence LIMIT ?",
            (after, limit),
        ).fetchall()
    conn.close()
    if not rows:
        return after
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(output, flags, 0o600), "a", encoding="utf-8") as stream:
        info, source = os.fstat(stream.fileno()), database.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) == (source.st_dev, source.st_ino)
        ):
            raise ValueError("comparison output must be a private, separate regular file")
        for sequence, encoded in rows:
            entry = json.loads(encoded)
            inputs = {k: entry[k] for k in ("operation", "request", "snapshot")}
            value = json.dumps(inputs).encode("utf-8")
            row = dict(
                sequence=sequence,
                version=entry["version"],
                time_ict=entry["time_ict"],
                broker_input=inputs,
                shadow_input=json.loads(value),
                broker=entry["broker"],
            )
            try:
                row["shadow"] = decide(value)
                if not isinstance(row["shadow"], dict):
                    raise ValueError("invalid shadow result")
                json.dumps(row["shadow"], allow_nan=False)

                def comparable(result):
                    return {
                        k: v
                        for k, v in result.items()
                        if not (entry["operation"] == "acquire" and k == "lease_id")
                    }

                row["comparison"] = (
                    "match"
                    if comparable(row["broker"]) == comparable(row["shadow"])
                    else "different"
                )
            except Exception as exc:
                row.pop("shadow", None)
                row["comparison"] = "timeout" if isinstance(exc, TimeoutError) else "error"
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            after = sequence
    return after


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    print(compare(args.database, args.output, after=args.after, limit=args.limit))
