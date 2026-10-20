from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest

from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.services import qa_results
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime.lifecycle import LifecycleAuthority


DAY = 86400


def _turn(library, name, started, *, status="failed", owner="qa@example.com", reason=None):
    library.seed(sid=name, status=status)
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE sessions SET start_time=?, end_time=?, interrupt_reason=? WHERE session_id=?",
            (started, started + 1, reason, name),
        )
        connection.execute("UPDATE run_meta SET requested_by=? WHERE session_id=?", (owner, name))
    return name


def _result(library, turn_id, *, category="provider", state="final", key="blocked.provider"):
    result = {
        "catalog_key": key,
        "verdict": "pass" if category is None else "blocked",
        "category": category,
        "reason": "translated text must not decide Gate A",
        "source": "fixture",
        "evidence": {"turn_id": turn_id, "step": None, "screenshot_url": None},
    }
    return qa_results.record_result(
        library.db, turn_id, result, label_state=state, source="fixture"
    )


def test_activation_is_durable_and_suppresses_only_pre_activation_classification(
    library, monkeypatch
):
    now = time.time()
    first = qa_results.activate(library.db, ["admin@example.com"], now=now)
    assert qa_results.activate(library.db, ["other@example.com"], now=now + DAY) == first
    assert first == {"activated_at": now, "admin_emails": ["admin@example.com"]}
    monkeypatch.setattr(session_repo, "db_path", library.db)
    old = _turn(library, "before-activation", now - DAY)
    TaskQueueService._classify_terminal_outcome(
        {"session_id": old, "status": "failed", "created_at": now - 1}
    )
    assert qa_results.read_result(library.db, old) is None
    current = _turn(library, "at-activation", now)
    TaskQueueService._classify_terminal_outcome(
        {"session_id": current, "status": "failed", "created_at": now}
    )
    assert qa_results.read_result(library.db, current) is not None


def test_backfill_is_bounded_idempotent_and_includes_rows_without_outbox(library):
    now = time.time()
    qa_results.activate(library.db, [], now=now)
    for name, started, status in [
        ("too-old", now - 300, "failed"),
        ("first", now - 100, "failed"),
        ("second", now - 50, "completed"),
        ("live", now - 40, "running"),
        ("future", now + 100, "failed"),
    ]:
        _turn(library, name, started, status=status)
    options = {"since": now - 200, "until": now, "limit": 1}
    assert qa_results.backfill(library.db, library.traces, **options)["turn_ids"] == ["first"]
    assert qa_results.backfill(library.db, library.traces, **options)["turn_ids"] == ["second"]
    assert qa_results.backfill(library.db, library.traces, **options)["turn_ids"] == []
    with sqlite3.connect(library.db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM qa_results").fetchone()[0] == 2


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 1001}, {"since": 3, "until": 2}])
def test_backfill_rejects_unbounded_or_inverted_requests(library, options):
    with pytest.raises(ValueError):
        qa_results.backfill(library.db, library.traces, **options)


def test_replay_rearms_only_abandoned_classifier_and_preserves_other_consumers(library):
    now = time.time()
    lifecycle = LifecycleAuthority(library.db, clock=lambda: now)
    for name, abandoned in [("one", "notify,classify"), ("two", "classify"), ("other", "notify")]:
        library.seed(sid=name, status="running")
        lifecycle.finish(name, "failed")
        with sqlite3.connect(library.db) as connection:
            connection.execute(
                "UPDATE lifecycle_outbox SET delivered_at=?, broadcast_at=?, notified_at=?, "
                "classified_at=?, classify_attempts=5, abandoned=? WHERE session_id=?",
                (now, now, now, now, abandoned, name),
            )
    assert qa_results.replay_abandoned(library.db, since=now - 1, until=now + 1, limit=1) == 1
    with sqlite3.connect(library.db) as connection:
        row = connection.execute(
            "SELECT delivered_at, broadcast_at, notified_at, classified_at, classify_attempts, "
            "abandoned FROM lifecycle_outbox WHERE session_id='one'"
        ).fetchone()
    assert row == (None, now, now, None, 0, "notify")
    assert qa_results.replay_abandoned(library.db, since=now - 1, until=now + 1) == 1
    assert qa_results.replay_abandoned(library.db, since=now - 1, until=now + 1) == 0


def test_classifier_failures_are_abandoned_at_retry_cap(library, monkeypatch):
    monkeypatch.setattr(session_repo, "db_path", library.db)
    library.seed(sid="retry", status="running")
    lifecycle = session_repo.lifecycle
    lifecycle.finish("retry", "failed")
    event = lifecycle.pending_events()[0]
    lifecycle.mark_delivered(event["dedupe_id"], "broadcast")
    lifecycle.mark_delivered(event["dedupe_id"], "notify")

    def fail(_event):
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(TaskQueueService, "_classify_terminal_outcome", staticmethod(fail))
    TaskQueueService._forget_delivery_memory()
    try:
        TaskQueueService._classification_owed[event["dedupe_id"]] = event
        for _attempt in range(TaskQueueService._MAX_DELIVERY_ATTEMPTS):
            TaskQueueService._classify_owed_outcomes()
        with sqlite3.connect(library.db) as connection:
            row = connection.execute(
                "SELECT classify_attempts, abandoned, delivered_at FROM lifecycle_outbox"
            ).fetchone()
        assert row[:2] == (5, "classify")
        assert row[2] is not None
        assert TaskQueueService._classification_owed == {}
    finally:
        TaskQueueService._forget_delivery_memory()


@pytest.mark.asyncio
async def test_admin_relabel_appends_atomic_version_and_audit(library, admin, qa):
    turn_id = _turn(library, "relabel", time.time())
    original = _result(library, turn_id)
    url = f"/api/system/qa-results/{turn_id}/relabel"
    payload = {"category": "qa_environment", "reason": "Confirmed locked device"}
    assert (await qa.post(url, json=payload)).status_code == 403
    response = await admin.post(url, json=payload)
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["result"]["category"] == "qa_environment"
    assert (await admin.post(url, json={**payload, "category": "invented"})).status_code == 422
    assert (await admin.post(url, json={**payload, "actor": "forged"})).status_code == 422
    with sqlite3.connect(library.db) as connection:
        rows = connection.execute(
            "SELECT version, result_json FROM qa_results ORDER BY version"
        ).fetchall()
        audit = connection.execute(
            "SELECT actor, reason, old_version, new_version FROM qa_result_audit"
        ).fetchall()
        assert json.loads(rows[0][1]) == original["result"]
        assert audit == [("admin@example.com", payload["reason"], 1, 2)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM qa_result_audit")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE qa_result_audit SET actor='forged'")
    refreshed = qa_results.result_for_turn(library.db, library.traces, turn_id, "failed", 1, 1)
    assert refreshed["category"] == "qa_environment"
    assert refreshed["updated"] is True


def test_concurrent_relabels_keep_contiguous_versions_and_audit(library):
    turn_id = _turn(library, "parallel-labels", time.time())
    _result(library, turn_id)

    def relabel(category):
        return qa_results.relabel_result(
            library.db, turn_id, category=category, actor="admin@example.com", reason="Review"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(relabel, ["provider", "unknown"]))
    with sqlite3.connect(library.db) as connection:
        assert connection.execute(
            "SELECT old_version, new_version FROM qa_result_audit ORDER BY new_version"
        ).fetchall() == [(1, 2), (2, 3)]
    refreshed = qa_results.result_for_turn(library.db, library.traces, turn_id, "failed", 1, 1)
    assert refreshed["source"] == "admin"


def test_gate_a_counts_turns_latest_machine_labels_and_snapshot_not_translations(library):
    activated = time.time() - 2 * DAY
    qa_results.activate(library.db, ["admin@example.com"], now=activated)
    for index in range(5):
        turn_id = _turn(library, f"known-{index}", activated + 10)
        _result(library, turn_id, category=None if index == 0 else "provider")
    for name, state in [
        ("unknown", "final"),
        ("insufficient", "final"),
        ("legacy", "legacy"),
        ("pending", "pending"),
    ]:
        _turn(library, name, activated + 10)
        _result(library, name, category="unknown", state=state, key="blocked.evidence")
    for name, status, owner, started in [
        ("cancelled", "cancelled", "qa@example.com", activated + 10),
        ("live", "running", "qa@example.com", activated + 10),
        ("queued", "queued", "qa@example.com", activated + 10),
        ("paused", "paused", "qa@example.com", activated + 10),
        ("admin", "failed", "admin@example.com", activated + 10),
        ("before", "failed", "qa@example.com", activated - 1),
        ("after", "failed", "qa@example.com", activated + 7 * DAY),
    ]:
        _turn(library, name, started, status=status, owner=owner)
    store = ChatStore(library.db)
    with store.transaction() as connection:
        for number, turn_id in enumerate(["known-0", "known-1"], start=1):
            connection.execute(
                "UPDATE chat_turns SET session_id='known-0', number=? WHERE turn_id=?",
                (number, turn_id),
            )
    gate = qa_results.gate_a(library.db)
    assert (gate["numerator"], gate["denominator"], gate["N"]) == (5, 9, 9)
    assert gate["coverage"] == pytest.approx(5 / 9)
    assert gate["cancel_rate"] == pytest.approx(1 / 13)
    assert gate["legacy"] == 1
    assert gate["pending"] == 4
    assert gate["passed"] is False
    for name in ["unknown", "insufficient", "legacy"]:
        qa_results.relabel_result(
            library.db, name, category="provider", actor="admin@example.com", reason="Reviewed"
        )
    gate = qa_results.gate_a(library.db)
    assert gate["numerator"] == 8
    assert gate["passed"] is True
    _turn(
        library, "admin-deploy-kill", activated + 10, owner="admin@example.com", reason="deploy_cut"
    )
    assert qa_results.gate_a(library.db)["deploy_killed"] == 1
    assert qa_results.gate_a(library.db)["passed"] is False


@pytest.mark.asyncio
async def test_gate_endpoint_requires_admin_and_reports_unactivated(library, admin, qa):
    assert (await qa.get("/api/system/qa-results/gate-a")).status_code == 403
    assert (await admin.get("/api/system/qa-results/gate-a")).status_code == 409
    qa_results.activate(library.db, [], now=time.time())
    response = await admin.get("/api/system/qa-results/gate-a")
    assert response.status_code == 200
    assert response.json()["N"] == 0
    assert response.json()["passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["activate", "backfill", "replay"])
async def test_maintenance_has_no_remotely_callable_http_route(admin, operation):
    assert (await admin.post(f"/api/system/qa-results/{operation}")).status_code in {404, 405}


def test_explicit_replay_can_classify_abandoned_pre_activation_event(library, monkeypatch):
    now = time.time()
    library.seed(sid="old-abandoned", status="running")
    lifecycle = LifecycleAuthority(library.db, clock=lambda: now - DAY)
    lifecycle.finish("old-abandoned", "failed")
    event = lifecycle.pending_events()[0]
    for consumer in ("broadcast", "notify", "classify"):
        lifecycle.mark_delivered(event["dedupe_id"], consumer, abandoned=consumer == "classify")
    lifecycle.acknowledge([event["dedupe_id"]])
    qa_results.activate(library.db, [], now=now)
    assert qa_results.replay_abandoned(library.db, since=now - 2 * DAY, until=now) == 1
    monkeypatch.setattr(session_repo, "db_path", library.db)
    TaskQueueService._forget_delivery_memory()
    try:
        TaskQueueService._drain_outcome_events()
        assert qa_results.read_result(library.db, "old-abandoned") is not None
        assert lifecycle.pending_events() == []
    finally:
        TaskQueueService._forget_delivery_memory()


def test_audit_failure_rolls_back_the_result_version(library):
    _turn(library, "rollback", time.time())
    _result(library, "rollback")
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_audit BEFORE INSERT ON qa_result_audit "
            "BEGIN SELECT RAISE(ABORT, 'disk-like failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        qa_results.relabel_result(
            library.db, "rollback", category="qa_environment", actor="admin", reason="Review"
        )
    assert qa_results.read_result(library.db, "rollback")["version"] == 1


def test_gate_includes_admission_failure_without_session_row(library):
    now = time.time()
    qa_results.activate(library.db, [], now=now - 10)
    root = _turn(library, "admission-chat", now)
    _result(library, root)
    store = ChatStore(library.db)
    turn_id = store.send(root, "Follow-up", "admission-message")["turn_id"]
    store.fail_admission(turn_id, "Admission failed")
    gate = qa_results.gate_a(library.db, now=now + 10)
    assert (gate["numerator"], gate["denominator"]) == (1, 2)
    assert gate["pending"] == 1


def test_local_cli_uses_ict_default_and_rejects_unbounded_batches(library, monkeypatch, capsys):
    from datetime import datetime
    from apps.admin_console.services.qa_result_commands import main

    assert (
        qa_results.DEFAULT_BACKFILL_SINCE
        == datetime.fromisoformat("2026-10-09T00:00:00+07:00").timestamp()
    )
    monkeypatch.setenv("ARTEMIS_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("ARTEMIS_AUTH_MODE", "cloudflare")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_AUD", "test-audience")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", "fixture")
    common = ["--db", str(library.db), "--traces", str(library.traces)]
    main([*common, "activate", "--at", "2026-10-20T10:00:00+07:00"])
    assert json.loads(capsys.readouterr().out)["admin_emails"] == ["admin@example.com"]
    with pytest.raises(SystemExit) as error:
        main([*common, "backfill", "--limit", "1001"])
    assert error.value.code == 2


def test_gate_uses_terminal_lifecycle_status_before_chat_reconciliation(library):
    now = time.time()
    qa_results.activate(library.db, [], now=now - 10)
    _turn(library, "vanished-chat", now, status="running")
    ChatStore(library.db)
    LifecycleAuthority(library.db).finish("vanished-chat", "failed")
    gate = qa_results.gate_a(library.db)
    assert gate["denominator"] == 1
