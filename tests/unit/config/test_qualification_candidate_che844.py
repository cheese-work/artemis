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
    assert "artemis run --standalone" in text
    assert "`--traces-path` controls\ntrace recording only" in text
    assert "latest `final#N` Checker ledger attempt" in text
    assert "capture a UI hierarchy XML" in text
    assert "ui_hierarchy_sha256" in text
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
                "hash-verified post-relaunch recovery-snapshot UI hierarchy and evidence "
                "manifest, ARTEMIS_TRACES_DIR-derived latest-final check_ledger.jsonl path, "
                "and adapter JSON output with exit 1"
            ),
        ]
        assert "verified release" in lane["cleanup_contract"]


SESSION_ID = "99d9a63b-8f1c-4e5d-98f5-070262adc1ee"
OBSERVED_ACCOUNT = "qual-run-1"
EXPECTED_ACCOUNT = "qual-run-1-WRONG-SUFFIX"
STEP_ID = "step-post-relaunch-42"


def _evidence_manifest(tmp_path, **changes):
    hierarchy = tmp_path / "post-relaunch-ui.xml"
    hierarchy.write_text(
        (
            '<hierarchy><node content-desc="recovery-snapshot" '
            f'text="{OBSERVED_ACCOUNT}" /></hierarchy>'
        ),
        encoding="utf-8",
    )
    value = {
        "schema_version": 2,
        "attempt_kind": "negative_control",
        "session_id": SESSION_ID,
        "screen_semantics_id": "recovery-snapshot",
        "final_attempt_id": "final#1",
        "post_relaunch_step_id": STEP_ID,
        "ui_hierarchy": {
            "path": hierarchy.name,
            "sha256": hashlib.sha256(hierarchy.read_bytes()).hexdigest(),
        },
    }
    value.update(changes)
    manifest = tmp_path / "negative-control-evidence.json"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    return manifest, value["ui_hierarchy"]["sha256"]


def _adapter_result(tmp_path, records, manifest, *, ledger_bytes=None):
    traces_dir = tmp_path / "runtime-traces"
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
            "--session-id",
            SESSION_ID,
            "--evidence-manifest",
            str(manifest),
            "--observed-account",
            OBSERVED_ACCOUNT,
            "--expected-account",
            EXPECTED_ACCOUNT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _failed_final(attempt_id="final#1", *, evidence_digest, **changes):
    return {
        "attempt_id": attempt_id,
        "kind": "assert",
        "status": "failed",
        "item_text": f"The post-relaunch account name must equal {EXPECTED_ACCOUNT}.",
        "trace_id": SESSION_ID,
        "anchor_step_id": STEP_ID,
        "evidence": (
            f"observed_account={OBSERVED_ACCOUNT}; "
            f"expected_account={EXPECTED_ACCOUNT}; "
            f"ui_hierarchy_sha256={evidence_digest}"
        ),
        **changes,
    }


def test_negative_control_verdict_adapter_exits_one_only_for_latest_exact_assertion(tmp_path):
    manifest, digest = _evidence_manifest(tmp_path)
    result = _adapter_result(tmp_path, [_failed_final(evidence_digest=digest)], manifest)

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["verdict"] == "fail_assertion"
    assert output["final_attempt_id"] == "final#1"
    assert output["ledger_path"].endswith(f"{SESSION_ID}/check_ledger.jsonl")


def test_negative_control_verdict_adapter_rejects_a_stale_final_failure(tmp_path):
    manifest, digest = _evidence_manifest(tmp_path, final_attempt_id="final#2")
    latest = _failed_final("final#2", evidence_digest=digest)
    latest["status"] = "passed"
    result = _adapter_result(tmp_path, [_failed_final(evidence_digest=digest), latest], manifest)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "checker"


def test_negative_control_verdict_adapter_rejects_checker_non_observation_even_when_named(
    tmp_path,
):
    manifest, digest = _evidence_manifest(tmp_path)
    result = _adapter_result(
        tmp_path,
        [
            _failed_final(
                evidence_digest=digest,
                evidence=(
                    f"Could not observe {OBSERVED_ACCOUNT} after relaunch; "
                    f"expected_account={EXPECTED_ACCOUNT}; "
                    f"ui_hierarchy_sha256={digest}"
                ),
            )
        ],
        manifest,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "checker"


def test_negative_control_verdict_adapter_rejects_a_hierarchy_without_the_account(tmp_path):
    manifest, digest = _evidence_manifest(tmp_path)
    hierarchy = tmp_path / "post-relaunch-ui.xml"
    hierarchy.write_text(
        '<hierarchy><node content-desc="recovery-snapshot" text="missing" /></hierarchy>',
        encoding="utf-8",
    )
    result = _adapter_result(tmp_path, [_failed_final(evidence_digest=digest)], manifest)

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "evidence_manifest"


def test_negative_control_verdict_adapter_requires_the_deliberate_wrong_suffix(tmp_path):
    manifest, digest = _evidence_manifest(tmp_path)
    adapter = REPO_ROOT / "qualification/tools/checker_verdict_exit.py"
    traces_dir = tmp_path / "runtime-traces"
    ledger = traces_dir / SESSION_ID / "check_ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps(_failed_final(evidence_digest=digest)), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(adapter),
            "--traces-dir",
            str(traces_dir),
            "--session-id",
            SESSION_ID,
            "--evidence-manifest",
            str(manifest),
            "--observed-account",
            OBSERVED_ACCOUNT,
            "--expected-account",
            OBSERVED_ACCOUNT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "evidence_manifest"


def test_negative_control_verdict_adapter_rejects_a_mismatched_post_relaunch_step(tmp_path):
    manifest, digest = _evidence_manifest(tmp_path)
    result = _adapter_result(
        tmp_path,
        [_failed_final(evidence_digest=digest, anchor_step_id="step-before-relaunch")],
        manifest,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "checker"


def test_negative_control_verdict_adapter_rejects_invalid_utf8_ledger(tmp_path):
    manifest, _ = _evidence_manifest(tmp_path)
    result = _adapter_result(
        tmp_path,
        [],
        manifest,
        ledger_bytes=b"\xff",
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "ledger"


def test_negative_control_verdict_adapter_rejects_a_missing_runtime_ledger(tmp_path):
    manifest, _ = _evidence_manifest(tmp_path)
    adapter = REPO_ROOT / "qualification/tools/checker_verdict_exit.py"

    result = subprocess.run(
        [
            sys.executable,
            str(adapter),
            "--traces-dir",
            str(tmp_path / "runtime-traces"),
            "--session-id",
            SESSION_ID,
            "--evidence-manifest",
            str(manifest),
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


def test_negative_control_verdict_adapter_rejects_malformed_later_final_attempt(tmp_path):
    manifest, digest = _evidence_manifest(tmp_path)
    result = _adapter_result(
        tmp_path,
        [_failed_final(evidence_digest=digest), {"attempt_id": "final#2"}],
        manifest,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "checker"


def test_negative_control_verdict_adapter_rejects_malformed_later_ledger_record(tmp_path):
    manifest, digest = _evidence_manifest(tmp_path)
    result = _adapter_result(
        tmp_path,
        [_failed_final(evidence_digest=digest)],
        manifest,
        ledger_bytes=(f"{json.dumps(_failed_final(evidence_digest=digest))}\n[]\n").encode(),
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "ledger"
