from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.services import failure_ledger, qa_results, task_queue_service
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime.lifecycle import LifecycleAuthority


@pytest.fixture
def outcome_sweep(library, monkeypatch):
    monkeypatch.setattr(session_repo, "db_path", library.db)
    monkeypatch.setattr(task_queue_service.DeviceExecutionLock, "get_active_owners", lambda: {})
    monkeypatch.setattr(state, "queue_items", [])
    monkeypatch.setattr(state, "active_runs", set())
    monkeypatch.setattr(state, "active_connections", {})
    monkeypatch.setattr(state, "current_process", None)
    monkeypatch.setattr(state, "active_session_id", None)
    TaskQueueService._forget_delivery_memory()
    yield library
    TaskQueueService._forget_delivery_memory()


def _failed_turn(library, sid, error):
    library.seed("Check the app", status="running", sid=sid)
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE run_meta SET requested_by='qa@example.com' WHERE session_id=?", (sid,)
        )
    assert LifecycleAuthority(library.db).finish(sid, "failed", error=error).committed
    library.write(
        sid,
        "status.json",
        json.dumps({"trace_id": sid, "status": "failed", "error": error}),
    )
    return sid


def test_sweep_classifies_each_turn_and_collect_race_does_not_duplicate(outcome_sweep, monkeypatch):
    first = _failed_turn(
        outcome_sweep, "turn-one", "Android secure keyguard is locked. Unlock the device manually."
    )
    second = _failed_turn(outcome_sweep, "turn-two", "LLM call timed out")
    barrier = threading.Barrier(2)

    def sweep():
        barrier.wait()
        TaskQueueService._reap_vanished_workers()

    def collect():
        barrier.wait()
        failure_ledger.collect(first)

    with ThreadPoolExecutor(max_workers=2) as executor:
        sweep_future = executor.submit(sweep)
        collect_future = executor.submit(collect)
        sweep_future.result()
        collect_future.result()

    TaskQueueService._reap_vanished_workers()

    first_result = qa_results.read_result(outcome_sweep.db, first)
    second_result = qa_results.read_result(outcome_sweep.db, second)
    assert first_result["result"]["category"] == "qa_environment"
    assert second_result["result"]["category"] == "provider"
    assert first_result["version"] == second_result["version"] == 1
    with sqlite3.connect(outcome_sweep.db) as connection:
        for sid in (first, second):
            count = connection.execute(
                "SELECT COUNT(*) FROM failure_ledger "
                "WHERE session_id=? AND step_number=0 AND signal='run_step'",
                (sid,),
            ).fetchone()[0]
            assert count == 1


def test_sweep_classifies_using_lifecycle_database_when_catalog_is_uninitialized(
    outcome_sweep, monkeypatch
):
    from apps.admin_console.database import connection
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    sid = _failed_turn(outcome_sweep, "catalog-db-unset", "LLM call timed out")
    monkeypatch.setattr(connection, "DB_PATH", outcome_sweep.db)
    monkeypatch.setattr(run_catalog_repo, "db_path", None)

    TaskQueueService._reap_vanished_workers()

    result = qa_results.read_result(outcome_sweep.db, sid)
    assert result is not None
    assert result["result"]["category"] == "provider"
    with sqlite3.connect(outcome_sweep.db) as connection:
        classified_at, attempts = connection.execute(
            "SELECT classified_at, classify_attempts FROM lifecycle_outbox"
        ).fetchone()
    assert classified_at is not None
    assert attempts == 0


def test_catalog_not_ready_does_not_block_deliveries_and_counts_classification_attempts(
    outcome_sweep, monkeypatch
):
    from apps.admin_console.database.repositories.run_catalog_repository import CatalogNotReady

    first = _failed_turn(outcome_sweep, "turn-catalog-one", "first failure")
    second = _failed_turn(outcome_sweep, "turn-catalog-two", "second failure")
    calls = []

    def broadcast(_lifecycle, event):
        calls.append(("broadcast", event["session_id"]))
        return True

    def notify(_lifecycle, _task_item, sid, _goal, _status, _event_id=None):
        calls.append(("notify", str(sid)))
        return True

    def classify(sid=None, *, db_path=None):
        calls.append(("classify", sid))
        raise CatalogNotReady()

    monkeypatch.setattr(TaskQueueService, "_broadcast_outcome", staticmethod(broadcast))
    monkeypatch.setattr(TaskQueueService, "_notify_session_end", staticmethod(notify))
    monkeypatch.setattr(failure_ledger, "collect", classify)

    TaskQueueService._reap_vanished_workers()

    assert calls == [
        ("broadcast", first),
        ("notify", first),
        ("broadcast", second),
        ("notify", second),
        ("classify", first),
        ("classify", second),
    ]
    with sqlite3.connect(outcome_sweep.db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(lifecycle_outbox)")}
        assert {"classified_at", "classify_attempts"} <= columns
        rows = connection.execute(
            "SELECT session_id, broadcast_at, notified_at, classified_at, classify_attempts "
            "FROM lifecycle_outbox ORDER BY created_at"
        ).fetchall()
    assert [(row[0], row[1] is not None, row[2] is not None, row[3], row[4]) for row in rows] == [
        (first, True, True, None, 1),
        (second, True, True, None, 1),
    ]


def test_admission_failure_without_sessions_row_still_gets_a_result(outcome_sweep):
    store = ChatStore(outcome_sweep.db)
    turn_id = "admission-failed-turn"
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO chat_sessions (session_id, created_at) VALUES (?, ?)",
            ("chat-root", 1.0),
        )
        connection.execute(
            "INSERT INTO chat_turns (turn_id, session_id, number, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (turn_id, "chat-root", 1, "queued", 1.0),
        )
        connection.execute(
            "INSERT INTO chat_messages (session_id, turn_id, role, content, client_id, created_at) "
            "VALUES (?, ?, 'human', ?, ?, ?)",
            ("chat-root", turn_id, "Check the app", "admission", 1.0),
        )

    store.fail_admission(turn_id, "The original phone binding is unavailable.")
    with sqlite3.connect(outcome_sweep.db) as connection:
        assert (
            connection.execute("SELECT 1 FROM sessions WHERE session_id=?", (turn_id,)).fetchone()
            is None
        )

    TaskQueueService._reap_vanished_workers()

    result = qa_results.read_result(outcome_sweep.db, turn_id)
    assert result is not None
    assert result["label_state"] == "pending"
    assert result["result"]["category"] == "unknown"
