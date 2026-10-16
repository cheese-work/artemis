import importlib
import importlib.util
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
