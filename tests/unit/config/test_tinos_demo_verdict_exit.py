"""Synthetic evidence tests for the Tinos negative-control verdict."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "qualification/tools/tinos_demo_verdict_exit.py"
SESSION_ID = "60000000-0000-4000-8000-000000000001"
CHECKER_TRACE_ID = "60000000-0000-4000-8000-000000000002"
CAPTURE_STEP_ID = "60000000-0000-4000-8000-000000000003"
STALE_CAPTURE_STEP_ID = "60000000-0000-4000-8000-000000000004"
FALSE_ASSERTION = (
    "The first Direct Message feature click must be denied by Tinos rules (NOT_ALLOWED)."
)
ALLOW_LOG = "1730000000.500 12 12 I TinosDemo: show allowed: shown and dismissed\n"
REVENUE_LOG = (
    "1730000001.000 12 12 I TinosDemo: revenue 123 micros USD "
    "unit=ca-app-pub-3940256099942544/1033173712 "
    "network=Google precision=publisher\n"
)
FILL_FAILURE_LOG = (
    "1730000000.000 12 12 I TinosDemo: not loaded; skipping\n" + ALLOW_LOG + REVENUE_LOG
)
VALID_LOG = ALLOW_LOG + REVENUE_LOG


def _synthetic_evidence(
    tmp_path: Path,
    *,
    log_text: str = VALID_LOG,
    assertion_status: str = "failed",
    ledger_capture_id: str = CAPTURE_STEP_ID,
    capture_attempt_id: str = "final#1",
    runner_exit_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    traces_dir = tmp_path / "ledger"
    session_dir = traces_dir / SESSION_ID
    session_dir.mkdir(parents=True)
    logcat_path = tmp_path / "tinos-demo-logcat.txt"
    logcat_path.write_text(log_text, encoding="utf-8")
    capture_metadata_path = tmp_path / "tinos-demo-logcat.meta"
    capture_metadata_path.write_text(
        "serial=SYNTHETIC-ONLY-NO-DEVICE\n"
        "start_cursor=1729999998.000\n"
        "device_start_epoch=1729999998\n"
        "host_time_before_epoch=1729999998.450\n"
        "host_time_after_epoch=1729999998.550\n"
        "logcat_pid=12345\n"
        f"runner_exit_status={runner_exit_status}\n"
        "logcat_stopped=true\n"
        "logcat_wait_status=130\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "data_engine.db"

    record = {
        "attempt_id": "final#1",
        "checkpoint_id": "final",
        "trace_id": CHECKER_TRACE_ID,
        "kind": "assert",
        "item_text": FALSE_ASSERTION,
        "status": assertion_status,
        "final_capture_step_id": ledger_capture_id,
    }
    (session_dir / "check_ledger.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE images (
                image_name TEXT PRIMARY KEY,
                ui_tree TEXT
            );
            CREATE TABLE steps (
                step_id TEXT PRIMARY KEY,
                session_id TEXT,
                timestamp REAL,
                pre_image_name TEXT,
                action_taken TEXT
            );
            CREATE TABLE traces (
                trace_id TEXT PRIMARY KEY,
                session_id TEXT,
                type TEXT,
                name TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO images VALUES (?, ?)",
            ("synthetic-final-image", json.dumps({"screen": "synthetic"})),
        )
        connection.executemany(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
            (
                (
                    "60000000-0000-4000-8000-000000000005",
                    SESSION_ID,
                    1729999999.0,
                    None,
                    json.dumps({"action": "tap", "target": "first-feature"}),
                ),
                (
                    CAPTURE_STEP_ID,
                    SESSION_ID,
                    1730000002.0,
                    "synthetic-final-image",
                    json.dumps(
                        {
                            "action": "checker_final_capture",
                            "attempt_id": capture_attempt_id,
                            "checker_trace_id": CHECKER_TRACE_ID,
                        }
                    ),
                ),
            ),
        )
        connection.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?)",
            (CHECKER_TRACE_ID, SESSION_ID, "agent", "checker"),
        )

    result = subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "--traces-dir",
            str(traces_dir),
            "--data-engine-db",
            str(db_path),
            "--session-id",
            SESSION_ID,
            "--logcat-file",
            str(logcat_path),
            "--capture-metadata-file",
            str(capture_metadata_path),
            "--device-serial",
            "SYNTHETIC-ONLY-NO-DEVICE",
            "--mode",
            "control",
            "--first-tap-at",
            "1729999999",
            "--runner-exit-status",
            str(runner_exit_status),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result, db_path


@pytest.mark.parametrize(
    (
        "log_text",
        "assertion_status",
        "ledger_capture_id",
        "runner_exit",
        "exit_code",
        "verdict",
        "reason",
    ),
    [
        pytest.param(
            VALID_LOG,
            "failed",
            CAPTURE_STEP_ID,
            0,
            1,
            "fail_assertion",
            "intended_false_assertion",
            id="synthetic-intended-failure",
        ),
        pytest.param(
            ALLOW_LOG,
            "failed",
            CAPTURE_STEP_ID,
            0,
            2,
            "invalid_control",
            "revenue_event_missing",
            id="synthetic-missing-revenue",
        ),
        pytest.param(
            REVENUE_LOG,
            "failed",
            CAPTURE_STEP_ID,
            0,
            2,
            "NOT-RUN",
            "allow_event_missing",
            id="synthetic-missing-allow",
        ),
        pytest.param(
            FILL_FAILURE_LOG,
            "failed",
            CAPTURE_STEP_ID,
            0,
            2,
            "invalid_control",
            "fill_failure",
            id="synthetic-fill-failure",
        ),
        pytest.param(
            VALID_LOG,
            "failed",
            STALE_CAPTURE_STEP_ID,
            0,
            2,
            "invalid_control",
            "native_capture",
            id="synthetic-stale-or-mismatched-capture",
        ),
        pytest.param(
            VALID_LOG,
            "passed",
            CAPTURE_STEP_ID,
            0,
            2,
            "invalid_control",
            "checker",
            id="synthetic-plain-pass",
        ),
        pytest.param(
            VALID_LOG,
            "failed",
            CAPTURE_STEP_ID,
            17,
            2,
            "invalid_control",
            "runner_process_failure",
            id="synthetic-arbitrary-runner-failure",
        ),
    ],
)
def test_tinos_adapter_classifies_synthetic_control_evidence(
    tmp_path,
    log_text,
    assertion_status,
    ledger_capture_id,
    runner_exit,
    exit_code,
    verdict,
    reason,
):
    result, _ = _synthetic_evidence(
        tmp_path,
        log_text=log_text,
        assertion_status=assertion_status,
        ledger_capture_id=ledger_capture_id,
        runner_exit_status=runner_exit,
    )

    assert result.returncode == exit_code
    output = json.loads(result.stdout)
    assert output["reason"] == reason
    assert output["verdict"] == verdict
    if exit_code == 1:
        assert output["session_id"] == SESSION_ID
        assert output["checker_trace_id"] == CHECKER_TRACE_ID
        assert output["final_capture_step_id"] == CAPTURE_STEP_ID
        assert output["final_capture_image_name"] == "synthetic-final-image"


def test_tinos_adapter_rejects_a_checker_capture_from_another_attempt(tmp_path):
    result, _ = _synthetic_evidence(tmp_path, capture_attempt_id="final#2")

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_tinos_adapter_rejects_a_revenue_line_without_the_literal_micros_token(tmp_path):
    malformed = ALLOW_LOG + REVENUE_LOG.replace("123 micros USD", "123 USD")
    result, _ = _synthetic_evidence(tmp_path, log_text=malformed)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "revenue_format_or_unit"


def test_tinos_adapter_rejects_a_non_test_ad_unit(tmp_path):
    wrong_unit = REVENUE_LOG.replace("1033173712", "wrong-test-unit")
    result, _ = _synthetic_evidence(tmp_path, log_text=ALLOW_LOG + wrong_unit)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "revenue_format_or_unit"


def test_tinos_adapter_rejects_a_denial_in_the_one_tap_control(tmp_path):
    denial = "1730000000.800 12 12 I TinosDemo: show denied by Tinos rules (NOT_ALLOWED)\n"
    result, _ = _synthetic_evidence(tmp_path, log_text=VALID_LOG + denial)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "unexpected_denial_event"


def test_tinos_adapter_rejects_a_log_event_outside_the_session_window(tmp_path):
    stale_allow = ALLOW_LOG.replace("1730000000.500", "1729999998.500")
    result, _ = _synthetic_evidence(tmp_path, log_text=stale_allow + REVENUE_LOG)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "event_outside_session_window"
