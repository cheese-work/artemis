"""Tests for the durable host recording assignment store."""

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from apps.admin_console.services.recording_assignments import (
    RecordingAssignment,
    RecordingAssignmentStore,
)


@pytest.fixture
def store(tmp_path: Path) -> RecordingAssignmentStore:
    return RecordingAssignmentStore(db_path=tmp_path / "hosts.db", clock=lambda: 100.0)


def test_assign_is_idempotent_by_session(store: RecordingAssignmentStore) -> None:
    assignment = store.assign("host-a", "run-a", 3)

    assert assignment.recording_id
    assert assignment.host_id == "host-a"
    assert assignment.session_id == "run-a"
    assert assignment.run_generation == 3
    assert assignment.desired_state == "record"
    assert (assignment.revision, assignment.acked_revision) == (1, 0)
    assert assignment.deadline == 2200.0
    assert assignment.created_at == assignment.updated_at == 100.0

    store.clock = lambda: 200.0
    assert store.assign("host-b", "run-a", 4) == assignment
    assert store.reconcile("host-a", 200.0) == [assignment]
    assert store.reconcile("host-b", 200.0) == []


def test_request_stop_revises_only_once(store: RecordingAssignmentStore) -> None:
    assignment = store.assign("host-a", "run-a", 3)
    store.clock = lambda: 200.0
    store.request_stop("run-a")

    (stopped,) = store.reconcile("host-a", 200.0)
    assert stopped.desired_state == "stop"
    assert (stopped.revision, stopped.acked_revision) == (2, 0)
    assert stopped.recording_id == assignment.recording_id
    assert stopped.created_at == assignment.created_at
    assert stopped.deadline == assignment.deadline
    assert stopped.updated_at == 200.0

    store.clock = lambda: 300.0
    store.request_stop("run-a")
    assert store.reconcile("host-a", 300.0) == [stopped]
    assert store.assign("host-a", "run-a", 4) == stopped


def test_reconcile_includes_active_and_unacknowledged_stops_in_creation_order(
    store: RecordingAssignmentStore,
) -> None:
    pending = store.assign("host-a", "run-z", 3)
    store.clock = lambda: 101.0
    active = store.assign("host-a", "run-a", 3)
    assert pending.recording_id != active.recording_id
    assert store.acknowledge("host-a", active.recording_id, 1)
    store.clock = lambda: 102.0
    completed = store.assign("host-a", "run-b", 3)
    store.clock = lambda: 103.0
    store.request_stop(pending.session_id)
    store.request_stop(completed.session_id)
    assert store.acknowledge("host-a", completed.recording_id, 2)

    snapshot = store.reconcile("host-a", 103.0)
    assert [(row.recording_id, row.desired_state) for row in snapshot] == [
        (pending.recording_id, "stop"),
        (active.recording_id, "record"),
    ]


def test_recording_expires_at_deadline_but_unacknowledged_stop_does_not(
    store: RecordingAssignmentStore,
) -> None:
    assignment = store.assign("host-a", "run-a", 3)

    assert store.reconcile("host-a", assignment.deadline - 0.1) == [assignment]
    assert store.reconcile("host-a", assignment.deadline) == []
    assert store.reconcile("host-a", assignment.deadline + 1) == []

    store.request_stop("run-a")
    (pending,) = store.reconcile("host-a", assignment.deadline + 1)
    assert pending.desired_state == "stop"


def test_acknowledge_requires_matching_host_and_advances_only_valid_revisions(
    store: RecordingAssignmentStore,
) -> None:
    assignment = store.assign("host-a", "run-a", 3)
    store.request_stop("run-a")
    (pending,) = store.reconcile("host-a", 100.0)
    store.clock = lambda: 200.0

    assert not store.acknowledge("host-b", assignment.recording_id, 2)
    assert not store.acknowledge("host-a", assignment.recording_id, 3)
    assert store.reconcile("host-a", 200.0) == [pending]

    assert store.acknowledge("host-a", assignment.recording_id, 1)
    (partly_acked,) = store.reconcile("host-a", 200.0)
    assert (partly_acked.revision, partly_acked.acked_revision) == (2, 1)
    store.clock = lambda: 300.0
    assert not store.acknowledge("host-a", assignment.recording_id, 0)
    assert not store.acknowledge("host-a", assignment.recording_id, 1)
    assert store.reconcile("host-a", 300.0) == [partly_acked]

    assert store.acknowledge("host-a", assignment.recording_id, 2)
    assert store.reconcile("host-a", 300.0) == []


def test_assignments_and_acknowledgments_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "hosts.db"
    store = RecordingAssignmentStore(db_path=db_path, clock=lambda: 100.0)
    active = store.assign("host-a", "run-a", 3)
    stopped = store.assign("host-a", "run-b", 3)
    assert store.acknowledge("host-a", active.recording_id, 1)
    store.request_stop(stopped.session_id)
    assert store.acknowledge("host-a", stopped.recording_id, 2)

    reopened = RecordingAssignmentStore(db_path=db_path, clock=lambda: 200.0)
    (persisted_active,) = reopened.reconcile("host-a", 200.0)
    assert persisted_active.recording_id == active.recording_id
    assert persisted_active.acked_revision == 1
    persisted_stop = reopened.assign("host-a", stopped.session_id, 4)
    assert persisted_stop.recording_id == stopped.recording_id
    assert persisted_stop.run_generation == 3
    assert persisted_stop.desired_state == "stop"
    assert (persisted_stop.revision, persisted_stop.acked_revision) == (2, 2)


def test_reconcile_is_host_scoped_for_records_and_stops(store: RecordingAssignmentStore) -> None:
    host_a = store.assign("host-a", "run-a", 3)
    host_b_active = store.assign("host-b", "run-b", 5)
    host_b_stopped = store.assign("host-b", "run-c", 5)
    store.request_stop(host_b_stopped.session_id)

    assert store.reconcile("host-a", 100.0) == [host_a]
    assert {row.recording_id for row in store.reconcile("host-b", 100.0)} == {
        host_b_active.recording_id,
        host_b_stopped.recording_id,
    }
    assert store.reconcile("host-c", 100.0) == []


def test_ready_columns_migrate_existing_assignments(tmp_path: Path) -> None:
    db_path = tmp_path / "hosts.db"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            CREATE TABLE host_recording_assignments (
                recording_id TEXT NOT NULL PRIMARY KEY,
                host_id TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                run_generation INTEGER NOT NULL,
                desired_state TEXT NOT NULL CHECK (desired_state IN ('record', 'stop')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                acked_revision INTEGER NOT NULL CHECK (acked_revision BETWEEN 0 AND revision),
                deadline REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO host_recording_assignments VALUES "
            "('existing', 'host-a', 'run-a', 3, 'record', 1, 1, 2200, 100, 101)"
        )

    store = RecordingAssignmentStore(db_path, clock=lambda: 110.0)
    (row,) = store.reconcile("host-a", 110.0)
    assert (row.recording_id, row.run_generation, row.acked_revision) == ("existing", 3, 1)
    assert (row.created_at, row.updated_at, row.deadline) == (100.0, 101.0, 2200.0)
    assert (row.ready_at, row.host_started_at, row.clock_offset) == (None, None, None)

    assert store.mark_ready("host-a", row.recording_id, 1, 105.25, 5.0)
    reopened = RecordingAssignmentStore(db_path)
    (ready,) = reopened.reconcile("host-a", 110.0)
    assert (ready.ready_at, ready.host_started_at, ready.clock_offset) == (110.0, 105.25, 5.0)


def test_mark_ready_requires_matching_active_assignment_and_only_sets_once(
    store: RecordingAssignmentStore,
) -> None:
    active = store.assign("host-a", "run-a", 1)
    stopped = store.assign("host-a", "run-b", 2)
    assert store.request_stop(stopped.session_id)
    store.clock = lambda: 110.0

    assert not store.mark_ready("host-b", active.recording_id, 1, 105.0, 5.0)
    assert not store.mark_ready("host-a", "unknown", 1, 105.0, 5.0)
    assert not store.mark_ready("host-a", active.recording_id, 2, 105.0, 5.0)
    assert not store.mark_ready("host-a", stopped.recording_id, 1, 105.0, 5.0)
    assert not store.mark_ready("host-a", stopped.recording_id, 2, 105.0, 5.0)
    rows = {row.session_id: row for row in store.reconcile("host-a", 110.0)}
    assert rows[active.session_id] == active
    assert rows[stopped.session_id].ready_at is None

    assert store.mark_ready("host-a", active.recording_id, 1, 105.25, 5.0)
    ready = store.assign("host-a", active.session_id, 1)
    assert (ready.ready_at, ready.host_started_at, ready.clock_offset) == (110.0, 105.25, 5.0)
    assert ready.acked_revision == 0
    store.clock = lambda: 120.0
    assert not store.mark_ready("host-a", active.recording_id, 1, 121.0, -1.0)
    assert store.assign("host-a", active.session_id, 1) == ready


@pytest.mark.parametrize("signal", ["ready", "timeout", "stop", "missing"])
def test_wait_ready_polls_shared_db_without_real_sleep(tmp_path: Path, signal: str) -> None:
    db_path = tmp_path / "hosts.db"
    elapsed = 0.0
    sleeps = []
    writer = RecordingAssignmentStore(db_path, clock=lambda: 100.0 + elapsed)
    assignment = writer.assign("host-a", "run-a", 1) if signal != "missing" else None

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed = round(elapsed + seconds, 8)
        if elapsed == 0.4 and assignment is not None:
            if signal == "ready":
                assert writer.mark_ready("host-a", assignment.recording_id, 1, 95.0, 5.0)
            elif signal == "stop":
                assert writer.request_stop(assignment.session_id)

    reader = RecordingAssignmentStore(db_path, monotonic=lambda: elapsed, sleep=sleep)
    ready = reader.wait_ready("run-a")

    assert elapsed == {"ready": 0.4, "timeout": 10.0, "stop": 0.4, "missing": 0.0}[signal]
    assert sleeps == pytest.approx(
        [0.2] * {"ready": 2, "timeout": 50, "stop": 2, "missing": 0}[signal]
    )
    if signal == "ready":
        assert ready == writer.assign("host-a", "run-a", 1)
        assert ready is not None and ready.ready_at == 100.4
        assert writer.request_stop("run-a")
        assert reader.wait_ready("run-a") is None
        assert sleeps == [0.2, 0.2]
    else:
        assert ready is None


@pytest.mark.parametrize(
    ("host_started_at", "clock_offset", "expected"),
    [(1000.0, 5.0, 1005.0), (1000.0, None, None), (None, 5.0, None), (None, None, None)],
)
def test_server_started_at_is_host_start_plus_clock_offset(
    host_started_at: float | None, clock_offset: float | None, expected: float | None
) -> None:
    assignment = RecordingAssignment(
        recording_id="rec",
        host_id="host-a",
        session_id="run-a",
        run_generation=1,
        desired_state="record",
        revision=1,
        acked_revision=0,
        deadline=0.0,
        created_at=0.0,
        updated_at=0.0,
        host_started_at=host_started_at,
        clock_offset=clock_offset,
    )

    assert assignment.server_started_at == expected
