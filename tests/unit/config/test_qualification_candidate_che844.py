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

    assert receipt["source_candidate"]["commit_sha"] == "f60e89216c5a2c4429520e68d20d17c8b07ae6b3"
    assert receipt["source_candidate"]["manifest_version"] == 3
    assert receipt["source_candidate"]["fork_sha"] == "25a2c5b2c839b674994ee4a2a49e79209142bd1d"
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
    assert "native DataEngine `step_id`" in text
    assert "successful native" in text
    assert "`stop_app` then `launch_app`" in text
    assert "exact UI\nvalues" in text
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
        assert "process ended" in lane["relaunch_proof"]["process_ended"]
        assert lane["negative_control"]["expected_verdict"] == "fail_assertion"
        adapter = lane["negative_control"]["verdict_adapter"]
        assert adapter["path"] == "qualification/tools/checker_verdict_exit.py"
        assert (
            adapter["digest_sha256"]
            == hashlib.sha256((REPO_ROOT / adapter["path"]).read_bytes()).hexdigest()
        )
        assert adapter["expected_exit"] == 1
        assert "ARTEMIS_TRACES_DIR/<session-id>/check_ledger.jsonl" in adapter["contract"]
        assert "native DataEngine" in adapter["contract"]
        assert lane["negative_control"]["not_satisfied_by"] == "completed orchestration status"
        assert (
            lane["negative_control"]["invalid_when_missing"]
            == "Invalid control; batch unqualified."
        )
        assert lane["negative_control"]["required_evidence"][-1] == (
            "native post-relaunch DataEngine step/image proof, ARTEMIS_TRACES_DIR-derived "
            "latest-final check_ledger.jsonl path, and adapter JSON output with exit 1"
        )
        assert "verified release" in lane["cleanup_contract"]


SESSION_ID = "99d9a63b-8f1c-4e5d-98f5-070262adc1ee"
CHECKER_TRACE_ID = "10000000-0000-4000-8000-000000000001"
STOP_STEP_ID = "20000000-0000-4000-8000-000000000001"
LAUNCH_STEP_ID = "30000000-0000-4000-8000-000000000001"
POST_RELAUNCH_STEP_ID = "40000000-0000-4000-8000-000000000001"
OBSERVED_ACCOUNT = "qual-run-1"
EXPECTED_ACCOUNT = "qual-run-1-WRONG-SUFFIX"
PACKAGE_NAME = "dev.cheese.pocketactual"


def _native_data_engine(
    tmp_path,
    *,
    observed_account=OBSERVED_ACCOUNT,
    post_step_number=4,
    include_stop_launch=True,
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
            pre_image_name TEXT
        );
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY,
            session_id TEXT,
            step_id TEXT,
            type TEXT,
            name TEXT,
            status TEXT,
            payload TEXT
        );
        """
    )
    image_name = "native-post-relaunch-image"
    ui_tree = [
        {"content_desc": "recovery-snapshot"},
        {"text": observed_account},
    ]
    connection.execute(
        "INSERT INTO images VALUES (?, ?)",
        (image_name, json.dumps(ui_tree)),
    )
    connection.executemany(
        "INSERT INTO steps VALUES (?, ?, ?, ?)",
        [
            (STOP_STEP_ID, SESSION_ID, 2, None),
            (LAUNCH_STEP_ID, SESSION_ID, 3, None),
            (POST_RELAUNCH_STEP_ID, SESSION_ID, post_step_number, image_name),
        ],
    )
    if include_stop_launch:
        for index, (step_id, action) in enumerate(
            [(STOP_STEP_ID, "stop_app"), (LAUNCH_STEP_ID, "launch_app")],
            start=1,
        ):
            connection.execute(
                "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"50000000-0000-4000-8000-00000000000{index}",
                    SESSION_ID,
                    step_id,
                    "action",
                    action,
                    "success",
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
        "evidence": "Final screen comparison recorded by the Checker.",
        **changes,
    }


def _adapter_result(
    tmp_path,
    records,
    traces_dir,
    db_path,
    *,
    post_relaunch_step_id=POST_RELAUNCH_STEP_ID,
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
            "--post-relaunch-step-id",
            post_relaunch_step_id,
            "--package-name",
            PACKAGE_NAME,
            "--observed-account",
            OBSERVED_ACCOUNT,
            "--expected-account",
            expected_account,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_negative_control_verdict_adapter_accepts_native_final_and_post_relaunch_capture(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path)
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["verdict"] == "fail_assertion"
    assert output["final_attempt_id"] == "final#1"
    assert output["post_relaunch_step_id"] == POST_RELAUNCH_STEP_ID
    assert output["post_relaunch_image_name"] == "native-post-relaunch-image"


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


def test_negative_control_verdict_adapter_rejects_a_pre_relaunch_capture(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path, post_step_number=1)
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


def test_negative_control_verdict_adapter_requires_native_stop_and_launch(tmp_path):
    traces_dir, db_path = _native_data_engine(tmp_path, include_stop_launch=False)
    result = _adapter_result(tmp_path, [_failed_final()], traces_dir, db_path)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "native_capture"


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
