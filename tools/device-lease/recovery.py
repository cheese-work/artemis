"""Trusted recovery composition. Adapters are owner-supplied; no device commands here."""

import json
import sqlite3


def validate_change(change):
    from broker import Refused

    if not isinstance(change, dict) or not isinstance(change.get("device_id"), str):
        raise Refused("invalid_change")
    fields = {
        "setting": {"kind", "device_id", "name", "before", "after"},
        "app": {"kind", "device_id", "package", "installed_by_run", "allowed_remove"},
        "home": {"kind", "device_id", "allowed"},
        "work": {"kind", "device_id", "complete", "processes", "forwards"},
    }
    kind = change.get("kind")
    if not isinstance(kind, str) or kind not in fields or set(change) != fields[kind]:
        raise Refused("invalid_change")
    if kind == "work":
        import re

        if (
            change["complete"] is not True
            or not isinstance(change["processes"], list)
            or not isinstance(change["forwards"], list)
            or len(change["processes"]) > 100
            or len(change["forwards"]) > 100
        ):
            raise Refused("invalid_change")
        seen = set()
        for identity in change["processes"]:
            if (
                not isinstance(identity, dict)
                or set(identity) != {"mode", "pid", "process_start", "boot_id"}
                or identity["mode"] != "local"
                or type(identity["pid"]) is not int
                or identity["pid"] < 2
                or any(
                    not isinstance(identity[k], str) or not identity[k].strip()
                    for k in ("process_start", "boot_id")
                )
                or identity["pid"] in seen
            ):
                raise Refused("invalid_change")
            seen.add(identity["pid"])
        seen = set()
        for forward in change["forwards"]:
            if (
                not isinstance(forward, dict)
                or set(forward) != {"direction", "local", "remote"}
                or forward["direction"] not in {"forward", "reverse"}
                or any(
                    not isinstance(forward[k], str)
                    or not re.fullmatch(r"tcp:[1-9][0-9]{0,4}", forward[k])
                    or int(forward[k][4:]) > 65535
                    for k in ("local", "remote")
                )
                or (forward["direction"], forward["local"]) in seen
            ):
                raise Refused("invalid_change")
            seen.add((forward["direction"], forward["local"]))
    if kind == "app" and (
        change["installed_by_run"] is not True or change["allowed_remove"] is not True
    ):
        raise Refused("cleanup_forbidden")
    if kind == "home" and change["allowed"] is not True:
        raise Refused("cleanup_forbidden")
    if kind in {"setting", "app"}:
        name = change["name"] if kind == "setting" else change["package"]
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise Refused("invalid_change")
    if kind == "setting" and any(
        v is not None and not isinstance(v, str) for v in (change["before"], change["after"])
    ):
        raise Refused("invalid_change")


def recover_pending(broker):
    """Fence first, undo recorded intents only, then HOLD for leader verification.

    fence(group, timeout=seconds) must deny connections, revoke credentials, close
    ALL streams, stop ONLY tracked old-run work/reverse forwards, and confirm it.
    undo(group, change, timeout=seconds) must be idempotent and compare current vs
    recorded values before restoring. No broad reset, uninstall or HOME fallback.
    Adapters must honor the supplied remaining deadline; uncertainty returns False.
    """
    broker._text(broker.recovery_owner, broker.ops_issue)
    with broker.transaction(allow_audit_failure=True) as (conn, now):
        pending = [
            broker._view(conn, r[0])
            for r in conn.execute(
                "SELECT lease_id FROM broker_groups WHERE state IN ('fencing','cleaning')"
            ).fetchall()
        ]
    for group in pending:
        lease_id = group["lease_id"]
        with sqlite3.connect(broker.db_path) as conn:
            deadline = conn.execute(
                "SELECT deadline_mono FROM broker_recovery_deadlines WHERE lease_id=?", (lease_id,)
            ).fetchone()
            changes = [
                json.loads(r[0])
                for r in conn.execute(
                    "SELECT change FROM broker_changes WHERE lease_id=? ORDER BY seq DESC",
                    (lease_id,),
                )
            ]
        state = group["state"]
        try:

            def remaining():
                return deadline[0] - broker.monotonic() if deadline else 0

            # Interrupted cleanup has uncertain external side effects. Recover manually.
            if (
                broker.recovery_io.fence(group, timeout=max(0.001, remaining())) is not True
                or remaining() <= 0
            ):
                raise RuntimeError("fence unconfirmed")
            if state != "fencing":
                raise RuntimeError("interrupted cleanup uncertain")
            broker.recovery(lease_id, state, "cleaning")
            state = "cleaning"
            for change in changes:
                validate_change(change)
                if (
                    remaining() <= 0
                    or broker.recovery_io.undo(group, change, timeout=remaining()) is not True
                ):
                    raise RuntimeError("cleanup unconfirmed")
            # R1's independent device-state comparison runs after all recorded undo.
            # Legacy local adapters have no raw-remote attestation contract.
            verify_state = getattr(broker.recovery_io, "verify_state", None)
            if group["binding"].get("mode") == "remote" and not callable(verify_state):
                raise RuntimeError("remote release attestation unavailable")
            if verify_state is not None and (
                remaining() <= 0 or verify_state(group, timeout=remaining()) is not True
            ):
                raise RuntimeError("release attestation unconfirmed")
            if remaining() <= 0:
                raise RuntimeError("cleanup overdue")
            broker.recovery(lease_id, state, "verifying")
        except Exception:
            broker.recovery(
                lease_id,
                state,
                "quarantine",
                recovery_owner=broker.recovery_owner,
                ops_issue=broker.ops_issue,
            )
        broker.finish_reconcile(lease_id)
