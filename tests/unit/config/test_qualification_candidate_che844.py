"""Structural checks for CHE-844's immutable save/relaunch candidate receipt."""

import hashlib
import json
from pathlib import Path
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


def test_journey_requires_process_death_and_negative_control_provenance():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    text = (REPO_ROOT / receipt["testcase"]["journey_path"]).read_text(encoding="utf-8")

    assert "`stop_app` / `am force-stop`" in text
    assert "process ended" in text
    assert "invalid and cannot pass" in text
    assert "generic\nchecker failure" in text
    assert "checker_verdict_exit.py" in text
    assert "ARTEMIS_TRACES_DIR=<ledger-root>" in text
    assert "`--traces-path` controls\ntrace recording only" in text
    assert "latest `final#N` Checker ledger attempt" in text
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
        assert "latest final attempt" in adapter["contract"]
        assert lane["negative_control"]["not_satisfied_by"] == "completed orchestration status"
        assert (
            lane["negative_control"]["invalid_when_missing"]
            == "Invalid control; batch unqualified."
        )
        assert lane["negative_control"]["required_evidence"] == [
            "Recovery snapshot confirmation for the original account and USD · +123.45 balance",
            "recorded post-save process stop and verified process end",
            "relaunch evidence for the Recovery snapshot",
            "observed post-relaunch account name",
            "recorded mismatch between the observed account name and qual-<run_id>-WRONG-SUFFIX",
            (
                "recovery-snapshot observation JSON, ARTEMIS_TRACES_DIR-derived "
                "latest-final check_ledger.jsonl path, and adapter JSON output with exit 1"
            ),
        ]
        assert "verified release" in lane["cleanup_contract"]


SESSION_ID = "99d9a63b-8f1c-4e5d-98f5-070262adc1ee"
OBSERVED_ACCOUNT = "qual-run-1"
EXPECTED_ACCOUNT = "qual-run-1-WRONG-SUFFIX"


def _observation(**changes):
    value = {
        "schema_version": 1,
        "attempt_kind": "negative_control",
        "session_id": SESSION_ID,
        "screen_semantics_id": "recovery-snapshot",
        "after_relaunch": True,
        "observed_account": OBSERVED_ACCOUNT,
        "expected_account": EXPECTED_ACCOUNT,
    }
    value.update(changes)
    return value


def _adapter_result(tmp_path, records, observation, *, ledger_bytes=None, observation_bytes=None):
    traces_dir = tmp_path / "runtime-traces"
    ledger = traces_dir / SESSION_ID / "check_ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    if ledger_bytes is not None:
        ledger.write_bytes(ledger_bytes)
    else:
        ledger.write_text(
            "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
        )
    observation_path = tmp_path / "negative-control-observation.json"
    if observation_bytes is not None:
        observation_path.write_bytes(observation_bytes)
    else:
        observation_path.write_text(json.dumps(observation), encoding="utf-8")
    adapter = REPO_ROOT / "qualification/tools/checker_verdict_exit.py"

    return subprocess.run(
        [
            sys.executable,
            str(adapter),
            "--traces-dir",
            str(traces_dir),
            "--session-id",
            SESSION_ID,
            "--observation",
            str(observation_path),
            "--observed-account",
            OBSERVED_ACCOUNT,
            "--expected-account",
            EXPECTED_ACCOUNT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _failed_final(attempt_id="final#1"):
    return {
        "attempt_id": attempt_id,
        "kind": "assert",
        "status": "failed",
        "item_text": f"The post-relaunch account name must equal {EXPECTED_ACCOUNT}.",
        "evidence": "Structured observation is stored separately.",
    }


def test_negative_control_verdict_adapter_exits_one_only_for_latest_exact_assertion(tmp_path):
    result = _adapter_result(tmp_path, [_failed_final()], _observation())

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["verdict"] == "fail_assertion"
    assert output["final_attempt_id"] == "final#1"
    assert output["ledger_path"].endswith(f"{SESSION_ID}/check_ledger.jsonl")


def test_negative_control_verdict_adapter_rejects_a_stale_final_failure(tmp_path):
    latest = _failed_final("final#2")
    latest["status"] = "passed"
    result = _adapter_result(tmp_path, [_failed_final(), latest], _observation())

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "checker"


def test_negative_control_verdict_adapter_rejects_non_observation_even_when_named(tmp_path):
    result = _adapter_result(
        tmp_path,
        [_failed_final()],
        _observation(after_relaunch=False),
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "observation"


def test_negative_control_verdict_adapter_rejects_invalid_utf8_observation(tmp_path):
    result = _adapter_result(
        tmp_path,
        [_failed_final()],
        _observation(),
        observation_bytes=b"\xff",
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["verdict"] == "invalid_control"


def test_negative_control_verdict_adapter_rejects_invalid_utf8_ledger(tmp_path):
    result = _adapter_result(
        tmp_path,
        [_failed_final()],
        _observation(),
        ledger_bytes=b"\xff",
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "ledger"


def test_negative_control_verdict_adapter_rejects_a_missing_runtime_ledger(tmp_path):
    observation_path = tmp_path / "negative-control-observation.json"
    observation_path.write_text(json.dumps(_observation()), encoding="utf-8")
    adapter = REPO_ROOT / "qualification/tools/checker_verdict_exit.py"

    result = subprocess.run(
        [
            sys.executable,
            str(adapter),
            "--traces-dir",
            str(tmp_path / "runtime-traces"),
            "--session-id",
            SESSION_ID,
            "--observation",
            str(observation_path),
            "--observed-account",
            OBSERVED_ACCOUNT,
            "--expected-account",
            EXPECTED_ACCOUNT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "ledger"
