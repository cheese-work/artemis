"""Promote a startup lease only when its managed SDK peer owns the execution lock."""

from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from broker import Refused
from device_lease.claims import _source, check_lock_conflicts
from startup import _journal, phase_grant_matches, pinned_device, verified_identity


def reviewed(grant, principal):
    testcase = grant.get("testcase")
    if not isinstance(testcase, dict) or set(testcase) != {"author", "revision", "reviews"}:
        return False
    revision, author, reviews = (testcase[key] for key in ("revision", "author", "reviews"))
    if (
        not isinstance(revision, str)
        or not re.fullmatch(r"[0-9a-f]{64}", revision)
        or not isinstance(author, str)
        or not author.strip()
        or not isinstance(reviews, list)
        or len(reviews) != 2
    ):
        return False
    reviewers, references = set(), set()
    for review in reviews:
        if (
            not isinstance(review, dict)
            or set(review) != {"reviewer", "revision", "verdict", "reference"}
            or review["revision"] != revision
            or review["verdict"] != "PASS"
            or any(
                not isinstance(review[key], str) or not review[key].strip()
                for key in ("reviewer", "reference")
            )
            or review["reviewer"] in {author, principal}
        ):
            return False
        reviewers.add(review["reviewer"])
        references.add(review["reference"])
    qualification = grant.get("qualification")
    return len(reviewers) == len(references) == 2 and (
        (isinstance(qualification, str) and bool(qualification.strip()))
        if grant["phase"] == "work"
        else qualification is None
    )


def _install(path, owner):
    fd, temporary = tempfile.mkstemp(prefix=".lease-lock-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(owner, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink()


def _persisted(conn, lease_id):
    row = conn.execute("SELECT * FROM broker_avd_work WHERE lease_id=?", (lease_id,)).fetchone()
    if row is None:
        return None
    return (
        Path(row["lock_path"]),
        json.loads(row["owner"]),
        json.loads(row["peer"]),
        json.loads(row["grant"]),
    )


def _live_lock(broker, conn, group, stored, now):
    path, owner, peer, grant = stored
    proof = group["startup"]
    device_id = proof["device"]["device_id"]
    phase = grant["phase"]
    current = broker._admission.avd_grants.get(phase)
    if (
        broker._admission.unavailable
        or current != grant
        or now >= grant["expires_at"]
        or json.loads(path.read_text()) != owner
        or broker.managed_runs.execution_peer(group["binding"], owner["pid"]) != peer
    ):
        raise ValueError("execution admission no longer live")
    source, aliases = _source(conn, broker.admission_guard.path, clock=lambda: now)
    if (
        path.parent != Path(source[6])
        or conn.execute("SELECT 1 FROM android_claims WHERE device_id=?", (device_id,)).fetchone()
    ):
        raise ValueError("execution ownership changed")
    check_lock_conflicts(device_id, aliases, path.parent, own_path=path)


def check_active(broker, conn, lease_id, now):
    stored = _persisted(conn, lease_id)
    if stored is None:
        raise ValueError("work lock missing")
    _live_lock(broker, conn, broker._view(conn, lease_id), stored, now)


def acquire(
    broker,
    peer_pid,
    /,
    *,
    lease_id,
    device_id,
    issue_id,
    generation,
    phase,
    agent_id,
    run_id,
    binding_id,
    guard,
    database,
    lock_dir,
    owner,
):
    broker._text(
        lease_id, device_id, issue_id, agent_id, run_id, binding_id, guard, database, lock_dir
    )
    if type(generation) is not int or phase not in ("qualification", "work"):
        raise Refused("invalid_request")
    if (
        not isinstance(owner, dict)
        or set(owner)
        - {
            "pid",
            "token",
            "device_id",
            "process_created_at",
            "acquired_at",
            "description",
            "session_id",
            "ingress",
            "lock_scope",
        }
        or type(owner.get("pid")) is not int
        or owner["pid"] != peer_pid
        or not isinstance(owner.get("token"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", owner["token"])
        or type(owner.get("process_created_at")) not in (float, int)
        or not math.isfinite(owner["process_created_at"])
        or owner["process_created_at"] <= 0
        or any(
            not isinstance(owner.get(key), str)
            for key in ("device_id", "description", "acquired_at")
        )
        or any(
            not isinstance(owner[key], str)
            for key in ("session_id", "ingress", "lock_scope")
            if key in owner
        )
    ):
        raise Refused("invalid_request")
    scope = owner.get("lock_scope")
    if scope not in (None, "tcp:127.0.0.1:5037"):
        raise Refused("device_not_yours")
    if (
        broker.startup_io is None
        or broker.managed_runs is None
        or not hasattr(broker.admission_guard, "path")
        or Path(guard).resolve() != broker.admission_guard.path.resolve()
        or Path(database).resolve() != broker.db_path.resolve()
    ):
        raise Refused("admission_unavailable")

    def inputs(conn, now):
        broker._require_authority(conn)
        group = broker._owned(conn, lease_id, agent_id, run_id)
        if group["state"] != "active":
            raise Refused("lease_not_active")
        proof, binding = group["startup"], group["binding"]
        if (
            proof is None
            or group["access_phase"] not in {"starting", "work"}
            or binding["mode"] != "managed"
            or (
                binding["issue_id"],
                binding["binding_id"],
                proof["generation"],
                proof["device"]["device_id"],
            )
            != (issue_id, binding_id, generation, device_id)
        ):
            raise Refused("access_phase_denied")
        if broker.draining or broker._admission.unavailable or not broker._admission.audit_ok:
            raise Refused("admission_unavailable")
        device = pinned_device(broker.registry, proof)
        endpoint = f"emulator-{device.console_port}"
        if owner["device_id"] != endpoint or group["devices"][0]["endpoint"] != endpoint:
            raise Refused("device_not_yours")
        source, aliases = _source(conn, guard, clock=lambda: now)
        directory = Path(source[6])
        if directory != Path(lock_dir).resolve() or aliases.get(endpoint) != device_id:
            raise Refused("admission_unavailable")
        lock_id = f"tcp_127_0_0_1_5037__{endpoint}" if scope else endpoint
        path = directory / f"artemis-device-{lock_id}.lock"
        grant = broker._admission.avd_grants.get(phase)
        if not phase_grant_matches(grant, phase, lease_id, device, proof, now) or not reviewed(
            grant, agent_id
        ):
            raise Refused("avd_phase_not_admitted")
        if group["access_phase"] == "work":
            stored = _persisted(conn, lease_id)
            if (
                stored is None
                or stored[0] != path
                or stored[3] != grant
                or (stored[1]["pid"], stored[1]["token"]) != (peer_pid, owner["token"])
            ):
                raise Refused("execution_lock_busy")
            return group, device, path, grant, None
        if device_id in broker._admission.blocked or os.path.lexists(path):
            raise Refused("execution_lock_busy")
        identity_grant = broker._admission.avd_grants.get("identity")
        if not phase_grant_matches(identity_grant, "identity", lease_id, device, proof, now):
            raise Refused("avd_phase_not_admitted")
        launch = conn.execute(
            "SELECT state,journal,identity FROM broker_launches WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if launch is None or launch["state"] != "executing" or launch["identity"] is None:
            raise Refused("startup_identity_unverified")
        return group, device, path, grant, (identity_grant, launch["journal"], launch["identity"])

    with broker.admission_guard():
        installed = False
        try:
            with broker.transaction() as (conn, now):
                group, device, path, grant, identity_input = inputs(conn, now)
            peer = broker.managed_runs.execution_peer(group["binding"], peer_pid)
            if identity_input is None:
                return {"acquired": True}
            _, journal_text, _ = identity_input
            journal = _journal(json.loads(journal_text))
            identity = broker.startup_io.observe(device, journal)
            verified_identity(device, journal, identity)
            if broker.managed_runs.execution_peer(group["binding"], peer_pid) != peer:
                raise Refused("run_not_live")
            with broker.transaction() as (conn, now):
                if inputs(conn, now) != (group, device, path, grant, identity_input):
                    raise Refused("admission_changed")
                _install(path, owner)
                installed = True
                broker.boundary("work:lock_installed")
                conn.execute(
                    "INSERT INTO broker_avd_work VALUES (?,?,?,?,?)",
                    (lease_id, json.dumps(grant), str(path), json.dumps(owner), json.dumps(peer)),
                )
                conn.execute("UPDATE broker_access SET phase='work' WHERE lease_id=?", (lease_id,))
                conn.execute(
                    "UPDATE broker_launches SET identity=? WHERE lease_id=?",
                    (json.dumps(dict(identity, observed_at=now)), lease_id),
                )
                broker._event(
                    conn, "work_admitted", now, group, phase=phase, grant_ref=grant["reference"]
                )
            return {"acquired": True}
        except (OSError, ValueError, sqlite3.Error, RuntimeError, KeyError) as exc:
            raise Refused("execution_handoff_unavailable") from exc
        finally:
            if installed:
                with closing(sqlite3.connect(broker.db_path)) as readback:
                    committed = readback.execute(
                        "SELECT 1 FROM broker_avd_work WHERE lease_id=?", (lease_id,)
                    ).fetchone()
                if not committed and json.loads(path.read_text()) == owner:
                    path.unlink()
