"""Synthetic evidence tests for the Tinos negative-control verdict."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "qualification/tools/tinos_demo_verdict_exit.py"
CAPTURE_WRAPPER = REPO_ROOT / "qualification/tools/capture_tinos_demo_logs.sh"
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
    feature_tap_count: int = 1,
    feature_tap_times: tuple[float, ...] | None = None,
    feature_click_counts: tuple[int | None, ...] | None = None,
    trace_click_counts: tuple[int | None, ...] | None = None,
    feature_tap_text: str = "Direct Message",
    feature_dispatch_interval: tuple[float, float] | None = None,
    include_feature_tap_trace: bool = True,
    coordinate_pair_tap: bool = False,
    self_heal_trace_coordinates: bool = False,
    mode: str = "control",
    captured_files: tuple[Path, Path] | None = None,
    first_visible_at: float = 1730000000,
    closed_at: float = 1730000000.2,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    traces_dir = tmp_path / "ledger"
    session_dir = traces_dir / SESSION_ID
    session_dir.mkdir(parents=True)
    if captured_files is None:
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
    else:
        logcat_path, capture_metadata_path = captured_files
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

    if feature_tap_times is None:
        feature_tap_times = tuple(
            1729999999.0 + tap_number / 10 for tap_number in range(feature_tap_count)
        )
    if feature_dispatch_interval is None:
        feature_dispatch_interval = (feature_tap_times[0] + 0.1, 0.2)
    if feature_click_counts is None:
        feature_click_counts = (None,) * len(feature_tap_times)
    if trace_click_counts is None:
        trace_click_counts = feature_click_counts

    def feature_action(click_count: int | None) -> dict[str, object]:
        if coordinate_pair_tap:
            return {
                "action": "tap",
                "coordinates": [540, 900],
                "target_description": "synthetic coordinates",
            }
        action = {
            "action": "tap",
            "coordinates": [540, 900],
            "coordinate_space": "pixel",
            "normalized_coordinates": [500, 375],
            "delay_ms": 100,
            "target_text": feature_tap_text,
            "target_bounds": "[420,820][660,980]",
            "target_resource_id": None,
            "target_class": "android.widget.TextView",
        }
        if click_count is not None:
            action["times"] = click_count
        return action

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
                step_id TEXT,
                parent_trace_id TEXT,
                type TEXT,
                name TEXT,
                timestamp REAL,
                duration REAL,
                status TEXT,
                payload TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO images VALUES (?, ?)",
            ("synthetic-final-image", json.dumps({"screen": "synthetic"})),
        )
        tap_steps = [
            (
                f"60000000-0000-4000-8000-{tap_number + 5:012d}",
                SESSION_ID,
                tap_timestamp,
                None,
                json.dumps(feature_action(feature_click_counts[tap_number])),
            )
            for tap_number, tap_timestamp in enumerate(feature_tap_times)
        ]
        action_traces = []
        for tap_number, tap_step in enumerate(tap_steps):
            if not include_feature_tap_trace:
                continue
            dispatch_start = feature_dispatch_interval[0] + (tap_step[2] - feature_tap_times[0])
            duration = feature_dispatch_interval[1]
            action = json.loads(tap_step[4])
            trace_action = action.copy()
            trace_click_count = trace_click_counts[tap_number]
            if trace_click_count is None:
                trace_action.pop("times", None)
            else:
                trace_action["times"] = trace_click_count
            if self_heal_trace_coordinates:
                trace_action["coordinates"] = [542, 902]
                trace_action.pop("normalized_coordinates", None)
            action_traces.append(
                (
                    f"60000000-0000-4000-8000-{tap_number + 20:012d}",
                    SESSION_ID,
                    tap_step[0],
                    None,
                    "action",
                    "tap",
                    dispatch_start,
                    duration,
                    "success",
                    json.dumps(
                        {
                            "action": trace_action,
                            "success": True,
                            "error_msg": "",
                            "post_screenshot": None,
                            "timestamp": dispatch_start + duration,
                            "relative_time": str(dispatch_start + duration - 1729999998),
                            "burst": False,
                        }
                    ),
                )
            )
        connection.executemany(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
            (
                *tap_steps,
                (
                    CAPTURE_STEP_ID,
                    SESSION_ID,
                    max(feature_tap_times, default=1729999999.0) + 3,
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
        connection.executemany(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                *action_traces,
                (
                    CHECKER_TRACE_ID,
                    SESSION_ID,
                    None,
                    None,
                    "agent",
                    "checker",
                    max(feature_tap_times, default=1729999999.0) + 4,
                    None,
                    "success",
                    "{}",
                ),
            ],
        )

    command = [
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
        mode,
        "--runner-exit-status",
        str(runner_exit_status),
    ]
    if mode == "positive":
        command.extend(
            [
                "--first-visible-at",
                str(first_visible_at),
                "--closed-at",
                str(closed_at),
            ]
        )
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return result, db_path


def _run_capture_wrapper(tmp_path: Path, log_text: str) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "date").write_text(
        "#!/bin/sh\nprintf '1729999998.450\\n'\n",
        encoding="utf-8",
    )
    (bin_dir / "adb").write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['-s', 'SYNTHETIC-ONLY-NO-DEVICE', 'shell', 'date', '+%s']:\n"
        "    print('1729999998')\n"
        "elif args[:3] == ['-s', 'SYNTHETIC-ONLY-NO-DEVICE', 'logcat']:\n"
        "    sys.stdout.write(os.environ['TINOS_SYNTHETIC_LOG_LINES'])\n"
        "    sys.stdout.flush()\n"
        "    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "    signal.pause()\n"
        "else:\n"
        "    raise SystemExit(64)\n",
        encoding="utf-8",
    )
    (bin_dir / "date").chmod(0o755)
    (bin_dir / "adb").chmod(0o755)
    logcat_path = tmp_path / "tinos-demo-logcat.txt"
    metadata_path = tmp_path / "tinos-demo-logcat.meta"
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
    environment["TINOS_SYNTHETIC_LOG_LINES"] = log_text
    result = subprocess.run(
        [
            "/bin/bash",
            str(CAPTURE_WRAPPER),
            "SYNTHETIC-ONLY-NO-DEVICE",
            str(logcat_path),
            str(metadata_path),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    return logcat_path, metadata_path


def test_capture_wrapper_output_is_accepted_by_control_verifier(tmp_path):
    captured_files = _run_capture_wrapper(tmp_path, VALID_LOG)
    result, _ = _synthetic_evidence(tmp_path, captured_files=captured_files)

    assert result.returncode == 1
    assert json.loads(result.stdout)["reason"] == "intended_false_assertion"


def test_capture_wrapper_output_is_accepted_by_positive_verifier(tmp_path):
    positive_log = (
        "1730000001.000 12 12 I TinosDemo: show allowed: shown and dismissed\n"
        "1730000001.200 12 12 I TinosDemo: revenue 123 micros USD "
        "unit=ca-app-pub-3940256099942544/1033173712 network=Google "
        "precision=publisher\n"
        "1730000002.500 12 12 I TinosDemo: "
        "show denied by Tinos rules (NOT_ALLOWED)\n"
    )
    captured_files = _run_capture_wrapper(tmp_path, positive_log)
    result, _ = _synthetic_evidence(
        tmp_path,
        assertion_status="passed",
        feature_tap_times=(1729999999.0, 1730000001.6),
        mode="positive",
        captured_files=captured_files,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["verdict"] == "PASS"


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


def test_tinos_control_requires_exactly_one_session_feature_tap(tmp_path):
    result, _ = _synthetic_evidence(tmp_path, feature_tap_count=2)

    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["verdict"] == "invalid_control"
    assert output["reason"] == "feature_tap_count"


@pytest.mark.parametrize("mode", ["control", "positive"])
@pytest.mark.parametrize("repeated_in", ["planned", "trace"])
def test_synthetic_repeated_feature_clicks_are_rejected_in_both_modes(tmp_path, mode, repeated_in):
    tap_times = (1729999999.0,) if mode == "control" else (1729999999.0, 1730000001.6)
    click_counts = (2,) + (None,) * (len(tap_times) - 1)
    planned_click_counts = click_counts if repeated_in == "planned" else None
    traced_click_counts = click_counts if repeated_in == "trace" else None
    positive_log = (
        "1730000001.000 12 12 I TinosDemo: show allowed: shown and dismissed\n"
        "1730000001.200 12 12 I TinosDemo: revenue 123 micros USD "
        "unit=ca-app-pub-3940256099942544/1033173712 network=Google "
        "precision=publisher\n"
        "1730000002.500 12 12 I TinosDemo: "
        "show denied by Tinos rules (NOT_ALLOWED)\n"
    )
    result, _ = _synthetic_evidence(
        tmp_path,
        log_text=positive_log if mode == "positive" else VALID_LOG,
        assertion_status="passed" if mode == "positive" else "failed",
        feature_tap_times=tap_times,
        feature_click_counts=planned_click_counts,
        trace_click_counts=traced_click_counts,
        mode=mode,
    )

    assert result.returncode == (12 if mode == "positive" else 2)
    output = json.loads(result.stdout)
    assert output["verdict"] == ("INVALID-EVIDENCE" if mode == "positive" else "invalid_control")
    assert output["reason"] == "feature_tap_click_count"


def test_control_rejects_planned_tap_without_successful_action_trace(tmp_path):
    result, _ = _synthetic_evidence(tmp_path, include_feature_tap_trace=False)

    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["verdict"] == "invalid_control"
    assert output["reason"] == "feature_tap_execution_missing"


def test_synthetic_self_healed_trace_matches_planned_tap_identity(tmp_path):
    result, _ = _synthetic_evidence(tmp_path, self_heal_trace_coordinates=True)

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["verdict"] == "fail_assertion"
    assert output["reason"] == "intended_false_assertion"


def test_synthetic_coordinate_pair_tap_is_not_feature_tap_evidence(tmp_path):
    result, _ = _synthetic_evidence(tmp_path, coordinate_pair_tap=True)

    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["verdict"] == "invalid_control"
    assert output["reason"] == "feature_tap_count"


@pytest.mark.parametrize(
    "target_text",
    [
        "Direct Message",
        "Direct Message\nSend a message without saving the contact",
        "Direct Message, Send a message without saving the contact",
    ],
    ids=["single-label", "merged-lines", "merged-comma"],
)
def test_control_accepts_and_reports_merged_feature_target_text(tmp_path, target_text):
    result, _ = _synthetic_evidence(tmp_path, feature_tap_text=target_text)

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["verdict"] == "fail_assertion"
    assert output["tap_observations"] == [
        {
            "planned_timestamp": pytest.approx(1729999999.0),
            "target_text": target_text,
            "action_trace_id": "60000000-0000-4000-8000-000000000020",
            "dispatch_start": pytest.approx(1729999999.1),
            "dispatch_end": pytest.approx(1729999999.3),
        }
    ]


def test_control_rejects_and_reports_unmatched_feature_target_text(tmp_path):
    target_text = "Message Directly, Send a message without saving the contact"
    result, _ = _synthetic_evidence(tmp_path, feature_tap_text=target_text)

    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["verdict"] == "invalid_control"
    assert output["reason"] == "feature_tap_count"
    assert output["tap_observations"] == [
        {
            "planned_timestamp": pytest.approx(1729999999.0),
            "target_text": target_text,
        }
    ]
