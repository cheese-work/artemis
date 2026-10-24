"""Synthetic, device-free tests for the Tinos log verifier and capture helper."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER = REPO_ROOT / "qualification/tools/tinos_demo_verdict_exit.py"
CAPTURE_HELPER = REPO_ROOT / "qualification/tools/capture_tinos_demo_logs.sh"
SESSION_ID = "61000000-0000-4000-8000-000000000001"
TRACE_ID = "61000000-0000-4000-8000-000000000002"
CAPTURE_ID = "61000000-0000-4000-8000-000000000003"
FIRST_TAP_ID = "61000000-0000-4000-8000-000000000004"
SECOND_TAP_ID = "61000000-0000-4000-8000-000000000005"
SERIAL = "SYNTHETIC-ONLY-NO-DEVICE"
ALLOW = "show allowed: shown and dismissed"
DENIAL = "show denied by Tinos rules (NOT_ALLOWED)"
REVENUE = (
    "revenue 123 micros USD unit=ca-app-pub-3940256099942544/1033173712 "
    "network=Google precision=publisher"
)
PASSING_LOG = (
    f"1729999902.700 12 12 I TinosDemo: {REVENUE}\n"
    f"1729999904.100 12 12 I TinosDemo: {ALLOW}\n"
    f"1729999906.000 12 12 I TinosDemo: {DENIAL}\n"
)
DEVICE_TO_HOST_OFFSET = 99.0


def _synthetic_run(
    tmp_path: Path,
    *,
    log_text: str | None = None,
    first_visible_at: float = 1730000001,
    close_at: float = 1730000001.5,
    second_tap_at: float = 1730000005,
    feature_tap_times: tuple[float, ...] | None = None,
    feature_tap_texts: tuple[str, ...] | None = None,
    feature_dispatch_intervals: tuple[tuple[float, float], ...] | None = None,
    missing_feature_trace_indices: tuple[int, ...] = (),
    failed_feature_trace_indices: tuple[int, ...] = (),
):
    if log_text is None:
        log_text = (
            f"{1730000001.7 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {REVENUE}\n"
            f"{close_at + 0.8 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {ALLOW}\n"
            f"{second_tap_at + 1.0 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {DENIAL}\n"
        )
    traces = tmp_path / "ledger"
    session_dir = traces / SESSION_ID
    session_dir.mkdir(parents=True)
    log_path = tmp_path / "tinos-demo-logcat.txt"
    log_path.write_text(log_text, encoding="utf-8")
    capture_metadata = tmp_path / "tinos-demo-logcat.meta"
    capture_metadata.write_text(
        "\n".join(
            (
                f"serial={SERIAL}",
                "start_cursor=1729999900.000",
                "device_start_epoch=1729999900",
                "host_time_before_epoch=1729999999.450",
                "host_time_after_epoch=1729999999.550",
                "logcat_pid=12345",
                "runner_exit_status=0",
                "logcat_stopped=true",
                "logcat_wait_status=130",
                "",
            )
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "data_engine.db"
    if feature_tap_times is None:
        feature_tap_times = (1730000000.75, second_tap_at)
    if feature_tap_texts is None:
        feature_tap_texts = ("Direct Message",) * len(feature_tap_times)
    if feature_dispatch_intervals is None:
        feature_dispatch_intervals = tuple(
            (timestamp + 0.05, 0.2) for timestamp in feature_tap_times
        )
    assert len(feature_tap_times) == len(feature_tap_texts) == len(feature_dispatch_intervals)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE images (image_name TEXT PRIMARY KEY, ui_tree TEXT);
            CREATE TABLE steps (
                step_id TEXT PRIMARY KEY, session_id TEXT, timestamp REAL,
                pre_image_name TEXT, action_taken TEXT
            );
            CREATE TABLE traces (
                trace_id TEXT PRIMARY KEY, session_id TEXT, step_id TEXT,
                parent_trace_id TEXT, type TEXT, name TEXT, timestamp REAL,
                duration REAL, status TEXT, payload TEXT
            );
            """
        )
        connection.execute("INSERT INTO images VALUES (?, ?)", ("synthetic-final", "{}"))
        tap_steps = []
        action_traces = []
        for index, (timestamp, target_text, dispatch_interval) in enumerate(
            zip(feature_tap_times, feature_tap_texts, feature_dispatch_intervals, strict=True)
        ):
            step_id = f"61000000-0000-4000-8000-{index + 10:012d}"
            action = {
                "action": "tap",
                "coordinates": [540, 900],
                "coordinate_space": "pixel",
                "normalized_coordinates": [500, 375],
                "times": 1,
                "delay_ms": 100,
                "target_text": target_text,
                "target_bounds": "[420,820][660,980]",
                "target_resource_id": None,
                "target_class": "android.widget.TextView",
            }
            tap_steps.append((step_id, SESSION_ID, timestamp, None, json.dumps(action)))
            if index in missing_feature_trace_indices:
                continue
            dispatch_start, duration = dispatch_interval
            action_traces.append(
                (
                    f"61000000-0000-4000-8000-{index + 20:012d}",
                    SESSION_ID,
                    step_id,
                    None,
                    "action",
                    "tap",
                    dispatch_start,
                    duration,
                    "failed" if index in failed_feature_trace_indices else "success",
                    json.dumps(
                        {
                            "action": action,
                            "success": index not in failed_feature_trace_indices,
                            "error_msg": "",
                            "post_screenshot": None,
                            "timestamp": dispatch_start + duration,
                            "relative_time": str(dispatch_start + duration - 1729999999),
                            "burst": False,
                        }
                    ),
                )
            )
        final_capture_at = (
            max(
                max(feature_tap_times, default=1730000000),
                max(
                    (start + duration for start, duration in feature_dispatch_intervals), default=0
                ),
            )
            + 2
        )
        connection.executemany(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "61000000-0000-4000-8000-000000000006",
                    SESSION_ID,
                    1729999999,
                    None,
                    json.dumps({"action": "launch", "target": "home"}),
                ),
                *tap_steps,
                (
                    CAPTURE_ID,
                    SESSION_ID,
                    final_capture_at,
                    "synthetic-final",
                    json.dumps(
                        {
                            "action": "checker_final_capture",
                            "attempt_id": "final#1",
                            "checker_trace_id": TRACE_ID,
                        }
                    ),
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                *action_traces,
                (
                    TRACE_ID,
                    SESSION_ID,
                    None,
                    None,
                    "agent",
                    "checker",
                    final_capture_at,
                    None,
                    "success",
                    "{}",
                ),
            ],
        )
    record = {
        "attempt_id": "final#1",
        "checkpoint_id": "final",
        "trace_id": TRACE_ID,
        "kind": "assert",
        "item_text": "The positive Tinos scenario assertions pass.",
        "status": "passed",
        "final_capture_step_id": CAPTURE_ID,
    }
    (session_dir / "check_ledger.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    command = [
        sys.executable,
        str(VERIFIER),
        "--mode",
        "positive",
        "--traces-dir",
        str(traces),
        "--data-engine-db",
        str(db_path),
        "--session-id",
        SESSION_ID,
        "--logcat-file",
        str(log_path),
        "--capture-metadata-file",
        str(capture_metadata),
        "--device-serial",
        SERIAL,
        "--runner-exit-status",
        "0",
        "--first-visible-at",
        str(first_visible_at),
        "--closed-at",
        str(close_at),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return result


def _json_result(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def test_synthetic_positive_verifier_accepts_ordered_session_events(tmp_path):
    result = _synthetic_run(tmp_path)

    assert result.returncode == 0
    output = _json_result(result)
    assert output["verdict"] == "PASS"
    assert output["allow_timestamp"] == pytest.approx(1730000002.3)
    assert output["first_tap_dispatch_end"] == pytest.approx(1730000001.0)
    assert output["second_tap_dispatch_end"] == pytest.approx(1730000005.25)
    assert output["device_to_host_offset_seconds"] == pytest.approx(DEVICE_TO_HOST_OFFSET)


@pytest.mark.parametrize(
    (
        "log_text",
        "first_visible_at",
        "close_at",
        "second_tap_at",
        "exit_code",
        "verdict",
        "reason",
    ),
    [
        pytest.param(
            "".join(
                line
                for line in PASSING_LOG.splitlines(keepends=True)
                if f"TinosDemo: {REVENUE}" not in line
            ),
            1730000001,
            1730000001.5,
            1730000005,
            10,
            "FAIL",
            "revenue_event_missing",
            id="synthetic-positive-missing-revenue",
        ),
        pytest.param(
            "".join(
                line
                for line in PASSING_LOG.splitlines(keepends=True)
                if f"TinosDemo: {ALLOW}" not in line
            ),
            1730000001,
            1730000001.5,
            1730000005,
            11,
            "NOT-RUN",
            "allow_event_missing",
            id="synthetic-positive-missing-allow",
        ),
        pytest.param(
            PASSING_LOG.replace(f"TinosDemo: {DENIAL}\n", ""),
            1730000001,
            1730000001.5,
            1730000005,
            10,
            "FAIL",
            "denial_event_missing",
            id="synthetic-positive-missing-denial",
        ),
        pytest.param(
            None,
            1730000001,
            1730000015,
            1730000017,
            11,
            "NOT-RUN",
            "timing_window_missed",
            id="synthetic-positive-timing-window-missed",
        ),
        pytest.param(
            PASSING_LOG.replace("1729999902.700", "1729999802.700"),
            1730000001,
            1730000001.5,
            1730000005,
            12,
            "INVALID-EVIDENCE",
            "event_before_logcat_cursor",
            id="synthetic-positive-stale-log-event",
        ),
    ],
)
def test_synthetic_positive_verifier_classifies_missing_and_stale_events(
    tmp_path, log_text, first_visible_at, close_at, second_tap_at, exit_code, verdict, reason
):
    result = _synthetic_run(
        tmp_path,
        log_text=log_text,
        first_visible_at=first_visible_at,
        close_at=close_at,
        second_tap_at=second_tap_at,
    )

    assert result.returncode == exit_code
    output = _json_result(result)
    assert output["verdict"] == verdict
    assert output["reason"] == reason


def test_synthetic_positive_verifier_rejects_denial_before_second_tap(tmp_path):
    log_text = PASSING_LOG.replace("1729999906.000", "1729999903.400")
    result = _synthetic_run(tmp_path, log_text=log_text)

    assert result.returncode == 12
    assert _json_result(result)["reason"] == "denial_before_second_tap"


def test_synthetic_positive_verifier_rejects_event_ambiguous_with_first_tap(tmp_path):
    log_text = PASSING_LOG.replace("1729999902.700", "1729999901.900")
    result = _synthetic_run(tmp_path, log_text=log_text)

    assert result.returncode == 12
    assert _json_result(result)["reason"] == "event_time_order_uncertain"


def test_synthetic_positive_second_allow_inside_window_is_gate_failure(tmp_path):
    second_allow = f"{1730000006.0 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {ALLOW}\n"
    log_text = PASSING_LOG.replace(f"1729999906.000 12 12 I TinosDemo: {DENIAL}\n", second_allow)
    result = _synthetic_run(tmp_path, log_text=log_text)

    assert result.returncode == 10
    output = _json_result(result)
    assert output["verdict"] == "FAIL"
    assert output["reason"] == "gate_allowed_inside_interval"
    assert output["revenue_observation"] == "present"


def test_synthetic_positive_gate_failure_reports_missing_revenue_separately(tmp_path):
    second_allow = f"{1730000006.0 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {ALLOW}\n"
    no_revenue = PASSING_LOG.replace(f"1729999902.700 12 12 I TinosDemo: {REVENUE}\n", "").replace(
        f"1729999906.000 12 12 I TinosDemo: {DENIAL}\n", second_allow
    )
    result = _synthetic_run(tmp_path, log_text=no_revenue)

    assert result.returncode == 10
    output = _json_result(result)
    assert output["reason"] == "gate_allowed_inside_interval"
    assert output["revenue_observation"] == "missing"


def test_synthetic_positive_duplicate_allow_outside_window_is_not_run(tmp_path):
    second_tap_at = 1730000018
    second_allow = f"{second_tap_at + 0.8 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {ALLOW}\n"
    log_text = PASSING_LOG.replace(f"1729999906.000 12 12 I TinosDemo: {DENIAL}\n", second_allow)
    result = _synthetic_run(
        tmp_path,
        log_text=log_text,
        second_tap_at=second_tap_at,
    )

    assert result.returncode == 11
    output = _json_result(result)
    assert output["verdict"] == "NOT-RUN"
    assert output["reason"] == "timing_window_missed"


@pytest.mark.parametrize("tap_count", [1, 3], ids=["one-tap", "three-taps"])
def test_synthetic_positive_requires_exactly_two_session_feature_taps(tmp_path, tap_count):
    tap_times = tuple(1730000000.75 + index for index in range(tap_count))
    result = _synthetic_run(tmp_path, feature_tap_times=tap_times)

    assert result.returncode == 12
    assert _json_result(result)["reason"] == "feature_tap_count"


def test_synthetic_positive_rejects_planned_tap_without_successful_action_trace(tmp_path):
    result = _synthetic_run(tmp_path, missing_feature_trace_indices=(1,))

    assert result.returncode == 12
    assert _json_result(result)["reason"] == "feature_tap_execution_missing"


def test_synthetic_positive_rejects_failed_feature_action_trace(tmp_path):
    result = _synthetic_run(tmp_path, failed_feature_trace_indices=(1,))

    assert result.returncode == 12
    assert _json_result(result)["reason"] == "feature_tap_execution_failed"


def test_synthetic_positive_uses_dispatch_end_for_late_denial(tmp_path):
    dispatch_intervals = ((1730000000.8, 0.2), (1730000017.5, 0.1))
    log_text = (
        f"{1730000001.7 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {REVENUE}\n"
        f"{1730000002.3 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {ALLOW}\n"
        f"{1730000018.0 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {DENIAL}\n"
    )
    result = _synthetic_run(
        tmp_path,
        log_text=log_text,
        feature_dispatch_intervals=dispatch_intervals,
    )

    assert result.returncode == 11
    assert _json_result(result)["reason"] == "timing_window_missed"


def test_synthetic_positive_late_second_allow_is_not_run(tmp_path):
    dispatch_intervals = ((1730000000.8, 0.2), (1730000017.5, 0.1))
    log_text = (
        f"{1730000001.7 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {REVENUE}\n"
        f"{1730000002.3 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {ALLOW}\n"
        f"{1730000018.0 - DEVICE_TO_HOST_OFFSET:.3f} 12 12 I TinosDemo: {ALLOW}\n"
    )
    result = _synthetic_run(
        tmp_path,
        log_text=log_text,
        feature_dispatch_intervals=dispatch_intervals,
    )

    assert result.returncode == 11
    assert _json_result(result)["reason"] == "timing_window_missed"


@pytest.mark.parametrize(
    "target_text",
    [
        "Direct Message",
        "Direct Message\nSend a message without saving the contact",
        "Direct Message, Send a message without saving the contact",
    ],
    ids=["single-label", "merged-lines", "merged-comma"],
)
def test_synthetic_positive_accepts_and_reports_merged_feature_target_text(tmp_path, target_text):
    result = _synthetic_run(tmp_path, feature_tap_texts=(target_text, target_text))

    assert result.returncode == 0
    output = _json_result(result)
    assert output["tap_observations"] == [
        {
            "planned_timestamp": pytest.approx(1730000000.75),
            "target_text": target_text,
            "action_trace_id": "61000000-0000-4000-8000-000000000020",
            "dispatch_start": pytest.approx(1730000000.8),
            "dispatch_end": pytest.approx(1730000001.0),
        },
        {
            "planned_timestamp": pytest.approx(1730000005),
            "target_text": target_text,
            "action_trace_id": "61000000-0000-4000-8000-000000000021",
            "dispatch_start": pytest.approx(1730000005.05),
            "dispatch_end": pytest.approx(1730000005.25),
        },
    ]


def test_synthetic_positive_rejects_and_reports_unmatched_feature_target_text(tmp_path):
    target_text = "Message Directly, Send a message without saving the contact"
    result = _synthetic_run(tmp_path, feature_tap_texts=(target_text, target_text))

    assert result.returncode == 12
    output = _json_result(result)
    assert output["reason"] == "feature_tap_count"
    assert [tap["target_text"] for tap in output["tap_observations"]] == [target_text] * 2


@pytest.mark.parametrize(
    "runner_status", [0, 17], ids=["synthetic-runner-pass", "synthetic-runner-failure"]
)
def test_synthetic_capture_helper_pins_serial_cursor_and_stops_logger(tmp_path, runner_status):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    adb_args_path = tmp_path / "adb-args.json"
    fake_adb = fake_bin / "adb"
    fake_adb.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json, signal, sys, time",
                "args = sys.argv[1:]",
                "if args[2:4] == ['shell', 'date']:",
                "    print('1729999900')",
                "    raise SystemExit(0)",
                f"open({str(adb_args_path)!r}, 'w').write(json.dumps(args))",
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))",
                "while True: time.sleep(0.05)",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_adb.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    logcat_path = tmp_path / "tinos-demo-logcat.txt"
    metadata_path = tmp_path / "tinos-demo-logcat.meta"
    runner_code = (
        "from pathlib import Path\nimport sys, time\n"
        f"target = Path({str(adb_args_path)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not target.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        f"raise SystemExit({runner_status} if target.exists() else 98)\n"
    )
    runner = [sys.executable, "-c", runner_code]

    result = subprocess.run(
        [
            "bash",
            str(CAPTURE_HELPER),
            SERIAL,
            str(logcat_path),
            str(metadata_path),
            "--",
            *runner,
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == runner_status
    metadata = dict(line.split("=", 1) for line in metadata_path.read_text().splitlines())
    assert metadata["serial"] == SERIAL
    assert float(metadata["start_cursor"]) > 0
    assert int(metadata["device_start_epoch"]) == 1729999900
    assert float(metadata["host_time_before_epoch"]) <= float(metadata["host_time_after_epoch"])
    assert int(metadata["logcat_pid"]) > 0
    assert metadata["runner_exit_status"] == str(runner_status)
    assert metadata["logcat_stopped"] == "true"
    assert json.loads(adb_args_path.read_text()) == [
        "-s",
        SERIAL,
        "logcat",
        "-v",
        "epoch",
        "-T",
        metadata["start_cursor"],
        "-s",
        "TinosDemo:I",
        "*:S",
    ]


def test_synthetic_capture_startup_failure_does_not_launch_runner(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_adb = fake_bin / "adb"
    fake_adb.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import os, sys",
                "args = sys.argv[1:]",
                "if args[2:4] == ['shell', 'date']:",
                "    print('1729999900')",
                "    raise SystemExit(0)",
                "raise SystemExit(int(os.environ['FAKE_ADB_LOGCAT_EXIT']))",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_adb.chmod(0o755)
    runner_marker = tmp_path / "runner-started"
    runner_code = f"from pathlib import Path; Path({str(runner_marker)!r}).touch()"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_ADB_LOGCAT_EXIT": "42",
    }

    result = subprocess.run(
        [
            "bash",
            str(CAPTURE_HELPER),
            SERIAL,
            str(tmp_path / "tinos-demo-logcat.txt"),
            str(tmp_path / "tinos-demo-logcat.meta"),
            "--",
            sys.executable,
            "-c",
            runner_code,
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 70
    assert not runner_marker.exists()
