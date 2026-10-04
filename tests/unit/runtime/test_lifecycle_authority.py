"""LifecycleAuthority: transition table, precedence, outbox dedupe, interleavings (CHE-1089)."""

from itertools import product
import sqlite3
import threading
import uuid

import pytest

from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store
from artemis.runtime.lifecycle import (
    TERMINAL_STATUSES,
    InterruptReason,
    LifecycleAuthority,
    can_transition,
    resolve_outcome,
)

NON_TERMINAL = ("queued", "running", "paused")
ALL_STATUSES = NON_TERMINAL + tuple(sorted(TERMINAL_STATUSES))


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "sessions.db"
    StorageManager(path, tmp_path)
    return path


@pytest.fixture
def traces(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    return tmp_path / "traces"


def _add_session(db_path, status="running", pid=None) -> str:
    session_id = str(uuid.uuid4())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status, device_info, pid)"
            " VALUES (?, 'goal', 1.0, ?, '{}', ?)",
            (session_id, status, pid),
        )
    return session_id


def _row(db_path, session_id) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        )


def _outbox(db_path, session_id=None) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM lifecycle_outbox").fetchall()
    return [dict(r) for r in rows if session_id in (None, r["session_id"])]


# -- transition table ------------------------------------------------------


@pytest.mark.parametrize(("current", "new"), list(product(ALL_STATUSES, ALL_STATUSES)))
def test_transition_table(current, new):
    expected = current not in TERMINAL_STATUSES and new != current
    assert can_transition(current, new) is expected


def test_legacy_success_alias_is_completed():
    assert can_transition("success", "failed") is False
    assert can_transition("running", "success") is True


@pytest.mark.parametrize(
    ("requested", "reason", "loss", "expected"),
    [
        ("failed", None, None, "failed"),
        ("failed", None, InterruptReason.HOST_DISCONNECTED, "interrupted"),
        ("completed", None, InterruptReason.HOST_DISCONNECTED, "completed"),
        ("cancelled", None, InterruptReason.HOST_DISCONNECTED, "cancelled"),
        ("interrupted", InterruptReason.DEVICE_OFFLINE, None, "interrupted"),
    ],
)
def test_resolve_outcome_precedence(requested, reason, loss, expected):
    assert resolve_outcome(requested, reason, loss)[0] == expected


def test_explicit_interrupt_reason_beats_a_noted_loss_reason():
    status, reason = resolve_outcome(
        "interrupted", InterruptReason.DEVICE_OFFLINE, InterruptReason.HOST_DISCONNECTED
    )
    assert (status, reason) == ("interrupted", InterruptReason.DEVICE_OFFLINE)


def test_finish_rejects_non_terminal_status_and_mismatched_reason(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    with pytest.raises(ValueError):
        authority.finish(sid, "running")
    with pytest.raises(ValueError):
        authority.finish(sid, "interrupted")
    with pytest.raises(ValueError):
        authority.finish(sid, "failed", reason="device_offline")
    with pytest.raises(ValueError):
        authority.interrupt(sid, "cosmic_rays")
    assert _row(db_path, sid)["status"] == "running"


# -- single outcome --------------------------------------------------------


def test_first_committed_outcome_is_final(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)

    first = authority.finish(sid, "completed")
    second = authority.finish(sid, "cancelled")
    third = authority.interrupt(sid, InterruptReason.HOST_DISCONNECTED)

    assert (first.status, first.committed) == ("completed", True)
    assert (second.status, second.committed) == ("completed", False)
    assert (third.status, third.committed) == ("completed", False)
    assert _row(db_path, sid)["status"] == "completed"
    assert _row(db_path, sid)["interrupt_reason"] is None


def test_unknown_session_commits_nothing(db_path):
    outcome = LifecycleAuthority(db_path).finish("missing", "failed")
    assert (outcome.status, outcome.committed) == (None, False)
    assert _outbox(db_path) == []


def test_worker_exit_cause_is_recorded_beside_the_outcome(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    authority.interrupt(sid, InterruptReason.DEVICE_OFFLINE)

    outcome = authority.settle_worker_exit(sid, returncode=1, manual_stop=False)

    row = _row(db_path, sid)
    assert outcome.status == "interrupted"
    assert row["status"] == "interrupted"
    assert row["interrupt_reason"] == "device_offline"
    assert row["exit_cause"] == "exit:1"


@pytest.mark.parametrize(
    ("returncode", "manual", "expected"),
    [(0, False, "completed"), (1, False, "failed"), (0, True, "cancelled"), (1, True, "cancelled")],
)
def test_settle_worker_exit_fallback(db_path, returncode, manual, expected):
    sid = _add_session(db_path)
    outcome = LifecycleAuthority(db_path).settle_worker_exit(sid, returncode, manual)
    assert outcome.status == expected


def test_settle_worker_exit_preserves_authoritative_result(db_path):
    sid = _add_session(db_path, status="completed")
    outcome = LifecycleAuthority(db_path).settle_worker_exit(sid, 1, True)
    assert (outcome.status, outcome.committed) == ("completed", False)


# -- loss arbitration ------------------------------------------------------


def test_worker_failure_after_a_noted_loss_is_interrupted(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    assert authority.note_loss(sid, InterruptReason.HOST_DISCONNECTED) is True

    outcome = authority.settle_worker_exit(sid, 1, False)

    assert (outcome.status, outcome.interrupt_reason) == (
        "interrupted",
        InterruptReason.HOST_DISCONNECTED,
    )


@pytest.mark.parametrize(
    ("status", "expected"), [("completed", "completed"), ("cancelled", "cancelled")]
)
def test_completion_or_cancel_beats_a_noted_loss(db_path, status, expected):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    authority.note_loss(sid, InterruptReason.BRIDGE_CLOSED)
    assert authority.finish(sid, status).status == expected
    assert _row(db_path, sid)["interrupt_reason"] is None


def test_loss_after_a_committed_outcome_is_ignored(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    authority.finish(sid, "completed")
    assert authority.note_loss(sid, InterruptReason.HOST_DISCONNECTED) is False
    assert _row(db_path, sid)["pending_loss_reason"] is None


def test_server_restart_interrupts_only_sessions_whose_worker_is_gone(db_path):
    dead = _add_session(db_path, pid=111)
    alive = _add_session(db_path, pid=222)
    queued = _add_session(db_path, status="queued")
    authority = LifecycleAuthority(db_path)

    interrupted = authority.interrupt_running_after_restart(lambda pid: pid == 222)

    assert interrupted == [dead]
    assert _row(db_path, dead)["status"] == "interrupted"
    assert _row(db_path, dead)["interrupt_reason"] == "server_restarted"
    assert _row(db_path, alive)["status"] == "running"
    assert _row(db_path, queued)["status"] == "queued"


def test_sweep_fails_vanished_workers_but_skips_owned_sessions(db_path):
    vanished = _add_session(db_path, pid=1)
    owned = _add_session(db_path, pid=2)
    authority = LifecycleAuthority(db_path)

    assert authority.fail_vanished_workers(lambda pid: False, lambda sid: sid == owned) == [
        vanished
    ]
    assert _row(db_path, vanished)["status"] == "failed"
    assert _row(db_path, vanished)["exit_cause"] == "worker_vanished"
    assert _row(db_path, owned)["status"] == "running"


# -- outbox ----------------------------------------------------------------


def test_one_outbox_row_per_first_committed_outcome(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    authority.finish(sid, "failed")
    authority.finish(sid, "completed")
    authority.interrupt(sid, InterruptReason.AUTH_EXPIRED)
    authority.settle_worker_exit(sid, 0, True)

    rows = _outbox(db_path, sid)
    assert [(r["dedupe_id"], r["status"]) for r in rows] == [(f"{sid}:outcome", "failed")]


# -- controlled-pause interleavings ----------------------------------------


def _race(db_path, first, second):
    """Hold ``first`` inside its open transaction, then launch ``second``.

    Returns the outcomes in commit order ``(first, second)``.
    """
    reached, release = threading.Event(), threading.Event()

    def pause(point):
        if point == "after_read":
            reached.set()
            assert release.wait(10)

    results: dict[str, object] = {}

    def run_first():
        results["first"] = first(LifecycleAuthority(db_path, pause=pause))

    def run_second():
        results["second"] = second(LifecycleAuthority(db_path))

    t1 = threading.Thread(target=run_first)
    t1.start()
    assert reached.wait(10)
    t2 = threading.Thread(target=run_second)
    t2.start()
    # The second writer queues on the sqlite write lock held by the first.
    t2.join(0.3)
    assert t2.is_alive()
    release.set()
    t1.join(10)
    t2.join(10)
    return results["first"], results["second"]


@pytest.mark.parametrize(
    ("first_call", "second_call", "winner"),
    [
        (lambda a, s: a.finish(s, "completed"), lambda a, s: a.finish(s, "cancelled"), "completed"),
        (lambda a, s: a.finish(s, "cancelled"), lambda a, s: a.finish(s, "completed"), "cancelled"),
        (
            lambda a, s: a.settle_worker_exit(s, 1, False),
            lambda a, s: a.interrupt(s, InterruptReason.HOST_DISCONNECTED),
            "failed",
        ),
        (
            lambda a, s: a.interrupt(s, InterruptReason.HOST_DISCONNECTED),
            lambda a, s: a.settle_worker_exit(s, 1, False),
            "interrupted",
        ),
        (
            lambda a, s: a.finish(s, "completed"),
            lambda a, s: a.interrupt(s, InterruptReason.SERVER_RESTARTED),
            "completed",
        ),
    ],
)
def test_interleaving_exactly_one_outcome_and_one_event(db_path, first_call, second_call, winner):
    sid = _add_session(db_path)
    first, second = _race(db_path, lambda a: first_call(a, sid), lambda a: second_call(a, sid))

    assert (first.committed, second.committed) == (True, False)
    assert first.status == second.status == winner
    assert _row(db_path, sid)["status"] == winner
    assert len(_outbox(db_path, sid)) == 1
    assert len(LifecycleAuthority(db_path).pending_events(sid)) == 1


def test_noted_loss_races_with_worker_exit(db_path):
    sid = _add_session(db_path)
    first, second = _race(
        db_path,
        lambda a: a.settle_worker_exit(sid, 1, False),
        lambda a: a.note_loss(sid, InterruptReason.HOST_DISCONNECTED),
    )
    # The worker exit committed first, so the later loss is ignored.
    assert first.status == "failed"
    assert second is False


# -- projection ------------------------------------------------------------


def test_status_json_projection_follows_the_committed_row(db_path, traces):
    sid = _add_session(db_path)
    trace_store.init_trace(sid, "goal", "flash")
    authority = LifecycleAuthority(db_path)

    authority.interrupt(sid, InterruptReason.DEVICE_OFFLINE, error="phone unplugged")
    authority.finish(sid, "completed")

    status = trace_store.read_status(sid)
    assert status["status"] == "interrupted"
    assert status["interrupt_reason"] == "device_offline"
    assert status["error"] == "phone unplugged"
    assert status["end_time"] == _row(db_path, sid)["end_time"]


def test_projection_is_skipped_for_sessions_without_a_status_file(db_path, traces):
    sid = _add_session(db_path)
    assert LifecycleAuthority(db_path).finish(sid, "failed").committed is True
    assert trace_store.read_status(sid) is None


def test_deleting_a_session_clears_its_outbox_row_so_a_reused_id_announces_again(db_path, tmp_path):
    sid = _add_session(db_path)
    LifecycleAuthority(db_path).finish(sid, "completed")
    storage = StorageManager(db_path, tmp_path)

    storage.delete_session(uuid.UUID(sid))

    assert _outbox(db_path) == []

    LifecycleAuthority(db_path).finish(_add_session(db_path), "failed")
    assert len(_outbox(db_path)) == 1
    storage.clear_all_data()
    assert _outbox(db_path) == []


# -- trace-only outcomes (traces that have no sessions row) -------------------


def test_finish_without_a_session_row_publishes_to_status_json_once(tmp_path, traces):
    authority = LifecycleAuthority(tmp_path / "missing.db")
    sid = str(uuid.uuid4())
    trace_store.init_trace(sid, "goal", "flash")

    first = authority.finish(sid, "cancelled", error="stopped", result={"r": 1}, device_serial="d")
    second = authority.finish(sid, "failed", error="late")

    assert (first.status, first.committed) == ("cancelled", True)
    assert (second.status, second.committed) == ("cancelled", False)
    status = trace_store.read_status(sid)
    assert (status["status"], status["error"], status["result"]) == (
        "cancelled",
        "stopped",
        {"r": 1},
    )
    assert status["device_serial"] == "d"
    assert not (tmp_path / "missing.db").exists()


def test_projection_carries_caller_metadata_only_when_its_status_won(db_path, traces):
    sid = _add_session(db_path)
    trace_store.init_trace(sid, "goal", "flash")
    authority = LifecycleAuthority(db_path)
    authority.finish(sid, "cancelled", error="stopped by user")

    authority.finish(sid, "failed", error="worker crashed")

    status = trace_store.read_status(sid)
    assert (status["status"], status["error"]) == ("cancelled", "stopped by user")


# -- recoverable outbox ------------------------------------------------------


def test_pending_events_are_not_acknowledged_until_acked(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    authority.interrupt(sid, InterruptReason.BRIDGE_CLOSED)

    first = authority.pending_events()
    again = authority.pending_events()  # a crash before delivery: still pending

    assert [e["dedupe_id"] for e in first] == [f"{sid}:outcome"]
    assert again == first
    assert authority.acknowledge([e["dedupe_id"] for e in first]) == 1
    assert authority.pending_events() == []


def test_ack_is_idempotent_and_a_redelivery_keeps_its_dedupe_id(db_path):
    sid = _add_session(db_path)
    authority = LifecycleAuthority(db_path)
    authority.finish(sid, "completed")

    delivered = authority.pending_events(sid)
    # a crash after delivery but before the ack: the same event comes back
    redelivered = authority.pending_events(sid)
    assert redelivered[0]["dedupe_id"] == delivered[0]["dedupe_id"]

    assert authority.acknowledge([delivered[0]["dedupe_id"]]) == 1
    assert authority.acknowledge([delivered[0]["dedupe_id"]]) == 0
    assert authority.pending_events(sid) == []
    assert len(_outbox(db_path, sid)) == 1
