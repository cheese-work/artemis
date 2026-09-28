"""Structural checks for CHE-844's immutable save/relaunch candidate receipt."""

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "qualification/candidates/pocket_actual_save_relaunch.che844.v1.json"


def test_receipt_maps_both_qualification_lanes_to_the_same_pinned_source():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    assert receipt["source_candidate"]["commit_sha"] == "a6c82bd4f8a49cf80b6bff5a5a61c7adb540004e"
    assert receipt["source_candidate"]["manifest_version"] == 4
    assert receipt["source_candidate"]["fork_sha"] == "a6c82bd4f8a49cf80b6bff5a5a61c7adb540004e"
    assert receipt["source_candidate"]["app_input_source"] == {
        "pull_request": 26,
        "commit_sha": "f60e89216c5a2c4429520e68d20d17c8b07ae6b3",
    }
    manifest = REPO_ROOT / receipt["source_candidate"]["manifest_path"]
    assert (
        hashlib.sha256(manifest.read_bytes()).hexdigest()
        == receipt["source_candidate"]["manifest_digest_sha256"]
    )
    assert (
        json.loads(manifest.read_text(encoding="utf-8"))["fork_sha"]["value"]
        == receipt["source_candidate"]["fork_sha"]
    )
    assert receipt["source_candidate"]["apk_digest_sha256"] == (
        "04795d5f3995c8e895f6440a9763c8a003c8b6ff955541e3965f5e71564e2e12"
    )
    workflow = receipt["source_candidate"]["app_workflow"]
    assert workflow["setup_semantics_id"] == "current-picture-setup"
    assert workflow["account_save_semantics_id"] == "save-account-action"
    assert workflow["balance_semantics_id"] == "verified-balance-amount-input"
    assert workflow["snapshot_semantics_id"] == "recovery-snapshot"
    assert workflow["verified_balance_input"] == "+123.45"
    assert workflow["persisted_markers"] == [
        "qual-<run_id>",
        "USD · +123.45",
        "History gap",
        "Recovery adjustment",
    ]

    lanes = {lane["issue"]: lane for lane in receipt["lanes"]}
    assert set(lanes) == {"CHE-540", "CHE-541"}
    assert lanes["CHE-540"]["transport"] == "emulator"
    assert lanes["CHE-541"]["transport"] == "usb_or_wireless_adb"


def test_receipt_pins_the_revised_journey_bytes():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    journey = REPO_ROOT / receipt["testcase"]["journey_path"]

    assert (
        hashlib.sha256(journey.read_bytes()).hexdigest()
        == receipt["testcase"]["journey_digest_sha256"]
    )


def test_journey_requires_process_death_and_native_negative_control_provenance():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    text = (REPO_ROOT / receipt["testcase"]["journey_path"]).read_text(encoding="utf-8")

    assert "`stop_app` / `am force-stop`" in text
    assert "process ended" in text
    assert "invalid and cannot pass" in text
    assert "checker_verdict_exit.py" in text
    assert "ARTEMIS_TRACES_DIR=<ledger-root>" in text
    assert "DATA_ENGINE_DB_PATH=<ledger-root>/data_engine.db" in text
    assert "artemis run --standalone" in text
    assert "DataEngine `step_id`" in text
    assert "successful native" in text
    assert "`stop_app` then `launch_app`" in text
    assert "final Checker itself records a native final-screen\ncapture step" in text
    assert "It never accepts an executor-selected\ncapture" in text
    assert "exact UI values" in text
    assert "completed` is not control success" in text
    assert 'observed name was\ncompared with "qual-<run_id>-WRONG-SUFFIX"' in text
    assert "invalid control" in text
    assert "unqualifies the entire batch" in text


def test_ledger_collection_recipe_matches_the_direct_cli_data_engine_path():
    cli_source = (REPO_ROOT / "artemis/interfaces/cli/commands/run.py").read_text(encoding="utf-8")
    agent_source = (REPO_ROOT / "artemis/sdk/agent.py").read_text(encoding="utf-8")

    assert "if test_name:" in cli_source
    assert "trace_path = traces_output_path_str or str(settings.TRACES_PATH)" in cli_source
    assert "self._tmp_traces_dir = Path(settings.TRACES_PATH)" in agent_source
    assert "traces_path=self._tmp_traces_dir" in agent_source
    assert "is_standalone = standalone or" in cli_source


def test_each_lane_retains_the_execution_controls_and_return_contract():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    for lane in receipt["lanes"]:
        assert lane["evidence_destination"]
        assert "secure-storage" in lane["fixture_reset"]
        assert len(lane["assertions"]) == 4
        assert lane["relaunch_proof"]["pass_prerequisite"] is True
        assert "force-stop" in lane["relaunch_proof"]["after_save_stop"]
        assert "pidof" in lane["relaunch_proof"]["process_ended"]
        assert lane["negative_control"]["expected_verdict"] == "fail_assertion"
        adapter = lane["negative_control"]["verdict_adapter"]
        assert adapter["path"] == "qualification/tools/checker_verdict_exit.py"
        assert (
            adapter["digest_sha256"]
            == hashlib.sha256((REPO_ROOT / adapter["path"]).read_bytes()).hexdigest()
        )
        assert adapter["expected_exit"] == 1
        assert "ARTEMIS_TRACES_DIR/<session-id>/check_ledger.jsonl" in adapter["contract"]
        assert "native final capture" in adapter["contract"]
        assert "native timestamps" in adapter["contract"]
        assert "pidof" in adapter["contract"]
        assert lane["negative_control"]["not_satisfied_by"] == "completed orchestration status"
        assert (
            lane["negative_control"]["invalid_when_missing"]
            == "Invalid control; batch unqualified."
        )
        assert lane["negative_control"]["required_evidence"][-1] == (
            "native timestamp-ordered saved-step and runner-bound final-capture proof, "
            "ARTEMIS_TRACES_DIR-derived latest-final check_ledger.jsonl path, and adapter "
            "JSON output with exit 1"
        )
        assert "verified release" in lane["cleanup_contract"]


def test_final_checker_persists_the_capture_link_used_by_the_adapter():
    checker_source = (REPO_ROOT / "artemis/agents/checker/checker.py").read_text(encoding="utf-8")
    graph_source = (REPO_ROOT / "artemis/graph/graph.py").read_text(encoding="utf-8")

    assert "checker_final_capture" in checker_source
    assert "final_capture_step_id" in checker_source
    assert '"final_capture_step_id": report.final_capture_step_id' in graph_source


SESSION_ID = "99d9a63b-8f1c-4e5d-98f5-070262adc1ee"
CHECKER_TRACE_ID = "10000000-0000-4000-8000-000000000001"
SAVED_STEP_ID = "20000000-0000-4000-8000-000000000001"
STOP_STEP_ID = "30000000-0000-4000-8000-000000000001"
LAUNCH_STEP_ID = "40000000-0000-4000-8000-000000000001"
FINAL_CAPTURE_STEP_ID = "50000000-0000-4000-8000-000000000001"
OBSERVED_ACCOUNT = "qual-run-1"
EXPECTED_ACCOUNT = "qual-run-1-WRONG-SUFFIX"
EXPECTED_BALANCE = "USD · +123.45"
PACKAGE_NAME = "dev.cheese.pocketactual"


def _native_data_engine(
    tmp_path,
    *,
    observed_account=OBSERVED_ACCOUNT,
    saved_step_number=1,
    final_capture_step_number=4,
    saved_timestamp=1.0,
    final_capture_timestamp=4.0,
    include_stop_launch=True,
    checker_session_id=SESSION_ID,
):
    traces_dir = tmp_path / "runtime-traces"
    traces_dir.mkdir()
    db_path = traces_dir / "data_engine.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE images (image_name TEXT PRIMARY KEY, ui_tree TEXT);
        CREATE TABLE steps (
            step_id TEXT PRIMARY KEY,
            session_id TEXT,
            step_number INTEGER,
            timestamp REAL,
            pre_image_name TEXT,
            action_taken TEXT
        );
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY,
            session_id TEXT,
            step_id TEXT,
            type TEXT,
            name TEXT,
            status TEXT,
            timestamp REAL,
            payload TEXT
        );
        """
    )
    saved_image_name = "native-saved-image"
    final_image_name = "native-final-image"
    saved_ui_tree = [
        {"content_desc": "recovery-snapshot"},
        {"text": observed_account},
        {"text": EXPECTED_BALANCE},
    ]
    final_ui_tree = [
        {"content_desc": "recovery-snapshot"},
        {"text": observed_account},
    ]
    connection.executemany(
        "INSERT INTO images VALUES (?, ?)",
        [
            (saved_image_name, json.dumps(saved_ui_tree)),
            (final_image_name, json.dumps(final_ui_tree)),
        ],
    )
    connection.executemany(
        "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                SAVED_STEP_ID,
                SESSION_ID,
                saved_step_number,
                saved_timestamp,
                saved_image_name,
                None,
            ),
            (STOP_STEP_ID, SESSION_ID, 2, 1.0, None, None),
            (LAUNCH_STEP_ID, SESSION_ID, 3, 1.0, None, None),
            (
                FINAL_CAPTURE_STEP_ID,
                SESSION_ID,
                final_capture_step_number,
                final_capture_timestamp,
                final_image_name,
                json.dumps(
                    {
                        "action": "checker_final_capture",
                        "attempt_id": "final#1",
                        "checker_trace_id": CHECKER_TRACE_ID,
                    }
                ),
            ),
        ],
    )
    connection.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (CHECKER_TRACE_ID, checker_session_id, None, "agent", "checker", "success", 4.1, "{}"),
    )
    if include_stop_launch:
        for index, (step_id, action) in enumerate(
            [(STOP_STEP_ID, "stop_app"), (LAUNCH_STEP_ID, "launch_app")],
            start=1,
        ):
            connection.execute(
                "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"50000000-0000-4000-8000-00000000000{index}",
                    SESSION_ID,
                    step_id,
                    "action",
                    action,
                    "success",
                    float(index + 1),
                    json.dumps({"action": {"action": action, "app_name": PACKAGE_NAME}}),
                ),
            )
    connection.commit()
    connection.close()
    return traces_dir, db_path


def _failed_final(attempt_id="final#1", **changes):
    return {
        "attempt_id": attempt_id,
        "kind": "assert",
        "status": "failed",
        "item_text": f"The post-relaunch account name must equal {EXPECTED_ACCOUNT}.",
        "trace_id": CHECKER_TRACE_ID,
        "anchor_step_id": None,
        "final_capture_step_id": FINAL_CAPTURE_STEP_ID,
        "evidence": "Final screen comparison recorded by the Checker.",
        **changes,
    }


def _adapter_result(
    tmp_path,
    records,
    traces_dir,
    db_path,
    *,
    ledger_bytes=None,
    expected_account=EXPECTED_ACCOUNT,
):
    ledger = traces_dir / SESSION_ID / "check_ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    if ledger_bytes is not None:
        ledger.write_bytes(ledger_bytes)
    else:
        ledger.write_text(
            "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
        )
    adapter = REPO_ROOT / "qualification/tools/checker_verdict_exit.py"

    return subprocess.run(
        [
            sys.executable,
            str(adapter),
            "--traces-dir",
            str(traces_dir),
            "--data-engine-db",
            str(db_path),
            "--session-id",
            SESSION_ID,
            "--saved-step-id",
            SAVED_STEP_ID,
            "--package-name",
            PACKAGE_NAME,
            "--observed-account",
            OBSERVED_ACCOUNT,
            "--expected-account",
            expected_account,
            "--expected-balance",
            EXPECTED_BALANCE,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_negative_control_verdict_adapter_accepts_native_final_control_proof(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["verdict"] == "fail_assertion"
    assert output["final_attempt_id"] == "final#1"
    assert output["saved_step_id"] == SAVED_STEP_ID
    assert output["final_capture_step_id"] == FINAL_CAPTURE_STEP_ID
    assert output["final_capture_image_name"] == "native-final-image"


def test_negative_control_verdict_adapter_rejects_a_stale_final_failure(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    latest = _failed_final("final#2", status="passed")
    result = _adapter_result(tmp_path, [_failed_final(), latest], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "checker"


def test_negative_control_verdict_adapter_rejects_substring_only_account_match(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path, observed_account="qual-run-10")
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_negative_control_verdict_adapter_rejects_the_expected_suffixed_account(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path, observed_account=EXPECTED_ACCOUNT)
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_negative_control_verdict_adapter_rejects_a_pre_relaunch_final_capture(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path, final_capture_timestamp=1.5)
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_negative_control_verdict_adapter_requires_native_stop_and_launch(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path, include_stop_launch=False)
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_negative_control_verdict_adapter_rejects_a_restart_before_the_save(tmp_path):
    traces_dir, db_path = _native_data_engine(
        tmp_path,
        saved_timestamp=3.5,
    )
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_negative_control_verdict_adapter_accepts_stop_launch_and_capture_in_one_step(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE steps SET step_number = 2 WHERE step_id IN (?, ?, ?)",
        (STOP_STEP_ID, LAUNCH_STEP_ID, FINAL_CAPTURE_STEP_ID),
    )
    connection.commit()
    connection.close()
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 1


def test_negative_control_verdict_adapter_rejects_an_unbound_final_checker_trace(tmp_path):
    traces_dir, db_path = _native_data_engine(
        tmp_path,
        checker_session_id="60000000-0000-4000-8000-000000000001",
    )
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_negative_control_verdict_adapter_requires_the_final_capture_link(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    result = _adapter_result(
        tmp_path,
        [_failed_final(final_capture_step_id="")],
        traces_dir,
        db_path,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "checker"


def test_negative_control_verdict_adapter_requires_the_deliberate_wrong_suffix(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    result = _adapter_result(
        tmp_path,
        [_failed_final()],
        traces_dir,
        db_path,
        expected_account=OBSERVED_ACCOUNT,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "account_contract"


def test_negative_control_verdict_adapter_rejects_invalid_utf8_ledger(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    result = _adapter_result(
        tmp_path,
        [],
        traces_dir,
        db_path,
        ledger_bytes=b"\xff",
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "ledger"


def test_negative_control_verdict_adapter_rejects_malformed_later_ledger_record(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    result = _adapter_result(
        tmp_path,
        [_failed_final()],
        traces_dir,
        db_path,
        ledger_bytes=(f"{json.dumps(_failed_final())}\n[]\n").encode(),
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "ledger"
