import importlib
import importlib.util
import json
import sqlite3

import pytest


SERVICE = "apps.admin_console.services.qa_results"


def _service():
    assert importlib.util.find_spec(SERVICE) is not None, "A2a QA result service is missing"
    return importlib.import_module(SERVICE)


@pytest.mark.parametrize(
    "execution_status,outcome,ledger_rows,expected_verdict,expected_category,expected_key",
    [
        (
            "interrupted",
            {
                "task_status": "completed",
                "evidence_ref": {
                    "step_id": "step-1",
                    "step_number": 1,
                    "image_name": "final-image",
                    "which": "post",
                },
            },
            [],
            "blocked",
            "unknown",
            "blocked.unknown",
        ),
        (
            "failed",
            {"task_status": "failed", "cause": "goal_unreachable"},
            [{"category": "qa_environment", "rule": "device_locked"}],
            "blocked",
            "qa_environment",
            "blocked.qa_environment",
        ),
        (
            "completed",
            {"task_status": "failed", "cause": "app_defect"},
            [],
            "blocked",
            "unknown",
            "blocked.unknown",
        ),
        (
            "completed",
            {
                "task_status": "partial",
                "tests": {
                    "failed": 1,
                    "failed_items": [{"kind": "assert", "anchor_step_id": "step-9"}],
                },
            },
            [],
            "fail",
            "app_defect",
            "fail.app_defect",
        ),
        (
            "completed",
            {
                "task_status": "completed",
                "tests": {"failed": 1, "failed_items": [{"kind": "check"}]},
            },
            [],
            "blocked",
            "unknown",
            "blocked.check",
        ),
        (
            "completed",
            {
                "task_status": "completed",
                "evidence_ref": {
                    "step_id": "step-1",
                    "step_number": 1,
                    "image_name": "final-image",
                    "which": "post",
                },
            },
            [],
            "pass",
            None,
            "pass.self_reported",
        ),
        (
            "completed",
            {
                "task_status": "completed",
                "tests": {"passed": 2, "failed": 0, "inconclusive": 0, "unchecked": 0},
            },
            [],
            "pass",
            None,
            "pass.verified",
        ),
    ],
)
def test_derive_result_maps_flash_and_pro_evidence(
    execution_status,
    outcome,
    ledger_rows,
    expected_verdict,
    expected_category,
    expected_key,
):
    result = _service().derive_result(execution_status, outcome, ledger_rows)

    assert result["verdict"] == expected_verdict
    assert result["category"] == expected_category
    assert result["catalog_key"] == expected_key


def test_qa_results_append_unknown_upgrade_once_and_freeze_known_category(tmp_path):
    service = _service()
    database = tmp_path / "qa-results.db"
    unknown = {
        "catalog_key": "blocked.unknown",
        "verdict": "blocked",
        "category": "unknown",
    }
    environment = {
        "catalog_key": "blocked.qa_environment",
        "verdict": "blocked",
        "category": "qa_environment",
    }
    provider = {
        "catalog_key": "blocked.provider",
        "verdict": "blocked",
        "category": "provider",
    }

    first = service.record_result(
        database, "turn-1", unknown, label_state="pending", source="outcome", now=1
    )
    upgraded = service.record_result(
        database, "turn-1", environment, label_state="final", source="ledger", now=2
    )
    frozen = service.record_result(
        database, "turn-1", provider, label_state="final", source="ledger", now=3
    )

    assert first["version"] == 1
    assert upgraded["version"] == 2
    assert frozen["version"] == 2
    assert frozen["result"]["category"] == "qa_environment"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT version, label_state, result_json FROM qa_results "
            "WHERE turn_id=? ORDER BY version",
            ("turn-1",),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0:2] == (1, "pending")
    assert rows[1][0:2] == (2, "final")


def test_final_unknown_can_be_upgraded_only_once(tmp_path):
    service = _service()
    database = tmp_path / "qa-results.db"
    unknown = {"catalog_key": "blocked.unknown", "verdict": "blocked", "category": "unknown"}
    environment = {
        "catalog_key": "blocked.qa_environment",
        "verdict": "blocked",
        "category": "qa_environment",
    }
    provider = {
        "catalog_key": "blocked.provider",
        "verdict": "blocked",
        "category": "provider",
    }

    service.record_result(database, "turn-2", unknown, label_state="final", source="timeout", now=1)
    upgraded = service.record_result(
        database, "turn-2", environment, label_state="final", source="ledger", now=2
    )
    frozen = service.record_result(
        database, "turn-2", provider, label_state="final", source="ledger", now=3
    )

    assert upgraded["version"] == frozen["version"] == 2
    assert frozen["result"]["category"] == "qa_environment"


@pytest.mark.parametrize(
    "execution_status,outcome,ledger_rows",
    [
        ("interrupted", {"cause": []}, []),
        ("completed", {"task_status": "completed", "evidence_ref": {"which": []}}, []),
        ("interrupted", None, [{"category": []}]),
        ("completed", {"task_status": "completed", "checker_items": [{"kind": []}]}, []),
    ],
)
def test_malformed_verdict_values_fall_back_to_blocked_unknown(
    execution_status, outcome, ledger_rows
):
    result = _service().derive_result(execution_status, outcome, ledger_rows)

    assert result["verdict"] == "blocked"
    assert result["category"] == "unknown"


def test_latest_run_step_ledger_row_wins_timestamp_tie_over_stuck_signal():
    result = _service().derive_result(
        "interrupted",
        {"cause": "goal_unreachable"},
        [
            {
                "category": "provider",
                "rule": "provider_timeout",
                "signal": "stuck_run",
                "classified_at": 1,
            },
            {
                "category": "qa_environment",
                "rule": "device_locked",
                "signal": "run_step",
                "classified_at": 1,
            },
        ],
    )

    assert result["category"] == "qa_environment"
    assert result["reason"] == "Điện thoại đang khoá"


def test_expired_auth_is_a_smartqa_infrastructure_result():
    result = _service().derive_result(
        "interrupted",
        None,
        [
            {
                "category": "smartqa_infra",
                "rule": "auth_expired",
                "signal": "run_step",
                "classified_at": 1,
            }
        ],
    )

    assert result["category"] == "smartqa_infra"
    assert result["reason"] == "Phiên đăng nhập SmartQA đã hết hạn"


@pytest.mark.parametrize(
    "terminal_state,terminal_key",
    [
        ("final", "blocked.unknown"),
        ("cancelled", "blocked.cancelled"),
        ("legacy", "blocked.unknown"),
    ],
)
def test_pending_unknown_can_transition_to_any_terminal_unknown_state(
    tmp_path, terminal_state, terminal_key
):
    service = _service()
    database = tmp_path / "qa-results.db"
    unknown = {"catalog_key": "blocked.unknown", "verdict": "blocked", "category": "unknown"}
    terminal = {**unknown, "catalog_key": terminal_key}

    pending = service.record_result(
        database, "turn-terminal", unknown, label_state="pending", source="pending", now=1
    )
    result = service.record_result(
        database,
        "turn-terminal",
        terminal,
        label_state=terminal_state,
        source=terminal_state,
        now=2,
    )

    assert pending["version"] == 1
    assert result["version"] == 2
    assert result["label_state"] == terminal_state
    assert result["result"]["catalog_key"] == terminal_key


@pytest.mark.parametrize("execution_status", ["failed", "completed"])
def test_flash_environment_cause_without_ledger_rule_stays_unknown(execution_status):
    result = _service().derive_result(
        execution_status,
        {"task_status": "failed", "cause": "blocked_by_environment"},
        [],
    )

    assert result["verdict"] == "blocked"
    assert result["category"] == "unknown"
    assert result["catalog_key"] == "blocked.unknown"


@pytest.mark.parametrize(
    "checker_item",
    [
        {"kind": "verify", "status": "failed"},
        {"kind": "assert", "status": "failed"},
    ],
)
def test_pro_uses_reconciled_failed_items_instead_of_stale_checker_items(checker_item):
    result = _service().derive_result(
        "completed",
        {
            "task_status": "completed",
            "tests": {
                "passed": 1,
                "failed": 0,
                "inconclusive": 0,
                "unchecked": 0,
                "failed_items": [],
            },
            "checker_items": [checker_item],
        },
        [],
    )

    assert result["verdict"] == "pass"
    assert result["catalog_key"] == "pass.verified"


def test_pro_assert_failure_without_anchor_remains_a_failure():
    result = _service().derive_result(
        "completed",
        {
            "task_status": "completed",
            "tests": {
                "passed": 0,
                "failed": 1,
                "inconclusive": 0,
                "unchecked": 0,
                "failed_items": [{"kind": "assert", "anchor_step_id": None}],
            },
        },
        [],
    )

    assert result["verdict"] == "fail"
    assert result["category"] == "app_defect"
    assert "_evidence_ref" not in result


def test_recent_run_without_outcome_or_ledger_stays_pending_until_legacy_cutoff(
    tmp_path, monkeypatch
):
    service = _service()
    database = tmp_path / "sessions.db"
    traces = tmp_path / "traces"
    traces.mkdir()
    monkeypatch.setenv("ARTEMIS_VERDICT_PENDING_CAP_SECONDS", "30")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, end_time REAL);"
            "CREATE TABLE lifecycle_outbox (session_id TEXT, created_at REAL);"
        )
        connection.execute("INSERT INTO sessions VALUES (?, ?)", ("turn-recent", 95))

    pending = service.result_for_turn(
        database, traces, "turn-recent", "failed", 1, 1, now=100
    )
    legacy = service.result_for_turn(database, traces, "turn-recent", "failed", 1, 1, now=126)

    assert pending["label_state"] == "pending"
    assert legacy["label_state"] == "legacy"


def test_pending_without_session_or_outbox_expires_from_pending_created_at(
    tmp_path, monkeypatch
):
    service = _service()
    database = tmp_path / "sessions.db"
    traces = tmp_path / "traces"
    outcome_dir = traces / "turn-no-session"
    outcome_dir.mkdir(parents=True)
    (outcome_dir / "run_outcome.json").write_text(
        json.dumps({"task_status": "failed"}), encoding="utf-8"
    )
    monkeypatch.setenv("ARTEMIS_VERDICT_PENDING_CAP_SECONDS", "30")

    pending = service.result_for_turn(
        database, traces, "turn-no-session", "failed", 1, 1, now=100
    )
    final = service.result_for_turn(
        database, traces, "turn-no-session", "failed", 1, 1, now=131
    )

    assert pending["label_state"] == "pending"
    assert final["label_state"] == "final"
    assert final["result"]["category"] == "unknown"
