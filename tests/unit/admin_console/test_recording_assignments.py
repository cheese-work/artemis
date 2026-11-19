"""Tests for the durable host recording assignment store."""

from pathlib import Path

import pytest

from apps.admin_console.services.recording_assignments import RecordingAssignmentStore


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
