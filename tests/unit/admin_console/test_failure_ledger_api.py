# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Failure ledger at the server API seam, using the real messages of the 2026-10-07 runs."""

import asyncio
import json
import sqlite3
import time
import uuid

import pytest

from apps.admin_console.services import failure_ledger

EMPTY_LIST = (
    "Error during click: Invalid target index 2. The list is empty on this screen. Use an "
    "index shown in the Visible UI Elements list, or normalized [x, y] coordinates with a "
    "target_description (ask_explorer can locate an element that is not listed)."
)
PHONE_GONE = (
    "Phone disconnected; the run stopped after the first failed device action. "
    "Reconnect the phone and start a new run."
)
OFFLINE = (
    "artemis.clients.screen_client_factory.DeviceOfflineError: Device 127.0.0.1:39129 is not "
    "available (adb does not list it). Reconnect the device or accept the USB debugging "
    "prompt, then retry."
)
SERIAL = "127.0.0.1:39129"
ACTION = {"smartqa": "fix", "provider": "monitor", "user": "no action", "unknown": "review"}


def _ok(text):
    return {"status": "dispatched", "result": text}


def _bad(text):
    return {"status": "failed", "result": text, "error": text}


@pytest.fixture
def notices(monkeypatch):
    """Digest deliveries; flip ``delivered`` to simulate a channel that is down."""

    class Notices(list):
        delivered = True

    box = Notices()

    def send(title, message):
        box.append((title, message))
        return box.delivered

    monkeypatch.setattr(failure_ledger, "_send", send)
    return box


def seed_run(
    library,
    *,
    goal="goal",
    status="failed",
    steps=(),
    interrupt=None,
    stdout=None,
    owner="qa@example.com",
    host_id=None,
    sid=None,
    age_days=0.0,
):
    sid = library.seed(goal=goal, status=status, sid=sid, age_days=age_days)
    now = time.time()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE sessions SET interrupt_reason = ?, device_info = ? WHERE session_id = ?",
            (interrupt, json.dumps({"device_id": SERIAL}), sid),
        )
        conn.execute(
            "UPDATE run_meta SET requested_by = ?, host_id = ? WHERE session_id = ?",
            (owner, host_id, sid),
        )
        for number, (action, result) in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO steps (step_id, session_id, step_number, timestamp, summary, "
                "action_taken, last_execution_result) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{sid}-{number}",
                    sid,
                    number,
                    now,
                    f"step {number}",
                    json.dumps({"action": action}),
                    json.dumps(result),
                ),
            )
    if stdout is not None:
        library.write(sid, "stdout.log", stdout)
    return sid


def seed_recording(library, sid, error, *, device=SERIAL):
    video_id = str(uuid.uuid4())
    now = time.time()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "INSERT INTO video_recordings "
            "(video_id, session_id, device_id, start_time, end_time, status, error) "
            "VALUES (?, ?, ?, ?, ?, 'failed', ?)",
            (video_id, sid, device, now - 30, now, error),
        )
    return video_id


async def collect(admin):
    response = await admin.post("/api/system/failures/collect")
    assert response.status_code == 200, response.text
    return response.json()


async def failures(admin, **params):
    response = await admin.get("/api/system/failures", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_session(view, sid):
    return sorted(
        (row for row in view["failures"] if row["session_id"] == sid),
        key=lambda row: row["step_number"],
    )


# -- acceptance: the runs of 2026-10-07 ------------------------------------------------


@pytest.mark.asyncio
async def test_the_three_reported_runs_classify_as_the_report_says(library, admin):
    clean = seed_run(  # fd931d1c: nothing failed
        library,
        status="completed",
        steps=[
            ("manage_app", _ok("Launched app 'vpn evo'")),
            ("click", _ok("Tapped at [500, 631]")),
        ],
    )
    video = seed_run(  # 22d8d458: empty element list, then the phone dropped
        library,
        status="interrupted",
        interrupt="device_offline",
        steps=[
            ("manage_app", _ok("Launched app 'YouTube'")),
            ("press_key", _ok("Pressed key 'BACK'.")),
            ("click", _bad(EMPTY_LIST)),
            ("click", _bad(EMPTY_LIST)),
            ("click", _bad(PHONE_GONE)),
        ],
        stdout="ℹ session_id=x ✅ Artemis agent stopped.\n",
    )
    offline = seed_run(  # 53413d5f: the device was never listed by adb
        library, status="failed", stdout=f"Traceback (most recent call last):\n{OFFLINE}\n"
    )
    assert (await collect(admin))["inserted"] == 6

    view = await failures(admin)

    assert by_session(view, clean) == []
    steps = by_session(view, video)
    assert [(r["step_number"], r["category"]) for r in steps] == [
        (0, "smartqa_infra"),
        (3, "smartqa_agent"),
        (4, "smartqa_agent"),
        (5, "smartqa_infra"),
    ]
    assert steps[0]["scope"] == "run" and steps[1]["scope"] == "step"
    offline_rows = by_session(view, offline)
    run_row = next(row for row in offline_rows if row["signal"] == "run_step")
    assert (run_row["category"], run_row["rule"]) == ("smartqa_infra", "device_unavailable")
    assert run_row["device"] == SERIAL
    assert run_row["owner"] == "qa@example.com"
    assert run_row["source"] == "browser"
    assert "is not available" in run_row["evidence"]
    assert "raw_error_text" in [row["signal"] for row in offline_rows]
    assert {"day", "category", "count"} <= set(view["counts"][0])
    infra = next(c for c in view["causes"] if c["category"] == "smartqa_infra")
    assert offline in infra["session_ids"] or video in infra["session_ids"]


@pytest.mark.asyncio
async def test_host_runs_are_labelled_by_source(library, admin):
    sid = seed_run(library, stdout=f"{OFFLINE}\n", host_id="host-1")
    await collect(admin)
    row = next(row for row in by_session(await failures(admin), sid) if row["signal"] == "run_step")
    assert row["source"] == "host"


# -- the classifier, table-driven through the API ----------------------------------------

CASES = [
    # (failed step text, category, rule)
    (PHONE_GONE, "smartqa_infra", "device_unavailable"),
    (
        "Error executing click: device '127.0.0.1:39129' not found",
        "smartqa_infra",
        "device_unavailable",
    ),
    (
        "Device emulator-5554 is not available (adb reports state 'offline').",
        "smartqa_infra",
        "device_unavailable",
    ),
    ("scrcpy server failed to start on the device", "smartqa_infra", "scrcpy_failure"),
    ("Accessibility helper did not respond", "smartqa_infra", "accessibility_helper"),
    ("adb install failed: INSTALL_FAILED_USER_RESTRICTED", "smartqa_infra", "adb_install"),
    (EMPTY_LIST, "smartqa_agent", "empty_target_list"),
    ("Error executing click: Tap failed at (540, 1200)", "smartqa_agent", "tap_failed"),
    ("UI hierarchy is empty", "smartqa_agent", "empty_ui_hierarchy"),
    ("TimeoutError: LLM call timed out after 180 seconds.", "provider", "llm_timeout"),
    ("artemis.llm.reliability.LLMExhaustedError: Connection error.", "provider", "llm_unavailable"),
    ("Gateway returned 503 Service Unavailable", "provider", "llm_unavailable"),
    ("Error finding package for app: ClimaMap", "user_prompt", "app_not_installed"),
    (
        "FlashRunner failed: I couldn’t complete the task. 'Foo' is not installed.",
        "user_prompt",
        "agent_reported_unachievable",
    ),
    (
        "[web_1_ab12cd34] FlashRunner failed: The model returned no response; the reactive loop stopped.",
        "provider",
        "model_no_response",
    ),
    (
        "[web_1_ab12cd34] FlashRunner failed: Max turns reached without final status report.",
        "unknown",
        "max_turns_reached",
    ),
    (
        "FlashRunner failed: I couldn't finish; the screen kept changing.",
        "unknown",
        "no_match",
    ),
    ("Error executing key press 'APP_SWITCH'.", "unknown", "no_match"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "category", "rule"), CASES)
async def test_classifier_table(library, admin, text, category, rule):
    sid = seed_run(library, status="completed", steps=[("click", _bad(text))])
    await collect(admin)
    row = next(row for row in by_session(await failures(admin), sid) if row["signal"] == "run_step")
    assert (row["category"], row["rule"]) == (category, rule)
    assert row["action"] == ACTION[category.split("_")[0]]


@pytest.mark.asyncio
async def test_run_without_a_recognisable_cause_is_unknown(library, admin):
    sid = seed_run(library, status="failed", stdout="ℹ nothing useful here\n")
    await collect(admin)
    row = next(row for row in by_session(await failures(admin), sid) if row["signal"] == "run_step")
    assert (row["category"], row["rule"]) == ("unknown", "no_match")


@pytest.mark.asyncio
async def test_interrupted_run_is_infra_by_its_end_reason(library, admin):
    sid = seed_run(library, status="interrupted", interrupt="server_restarted")
    await collect(admin)
    (row,) = by_session(await failures(admin), sid)
    assert (row["category"], row["rule"]) == ("smartqa_infra", "run_interrupted")
    assert row["signal"] == "stuck_run"


@pytest.mark.asyncio
async def test_failed_run_uses_run_step_signal_not_stuck_run(library, admin):
    sid = seed_run(library, status="failed", stdout=f"{OFFLINE}\n")

    await collect(admin)

    row = next(row for row in by_session(await failures(admin), sid) if row["signal"] == "run_step")
    assert row["scope"] == "run"
    assert row["signal"] == "run_step"


@pytest.mark.asyncio
async def test_run_recollection_skips_an_existing_terminal_cause(library, admin):
    sid = seed_run(library, status="failed", stdout="RuntimeError: worker failed\n")

    await collect(admin)
    library.write(sid, "stdout.log", "")
    result = await collect(admin)

    rows = [row for row in by_session(await failures(admin), sid) if row["scope"] == "run"]
    assert result["inserted"] == 0
    assert {row["signal"] for row in rows} == {"run_step", "raw_error_text"}


@pytest.mark.asyncio
async def test_stuck_run_keeps_its_terminal_interruption_cause(library, admin):
    sid = seed_run(library, status="running")
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE sessions SET start_time = ? WHERE session_id = ?",
            (time.time() - 2 * 60 * 60, sid),
        )

    await collect(admin)

    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE sessions SET status = 'interrupted', interrupt_reason = 'device_offline', "
            "end_time = ? WHERE session_id = ?",
            (time.time(), sid),
        )
    await collect(admin)
    rows = by_session(await failures(admin), sid)

    assert {(row["signal"], row["rule"]) for row in rows} == {
        ("stuck_run", "run_stuck"),
        ("stuck_run", "run_interrupted"),
    }
    assert (await collect(admin))["inserted"] == 0


@pytest.mark.asyncio
async def test_failed_recordings_and_raw_error_messages_are_collected(library, admin):
    goal = "Transfer the private report to customer-834"
    failed = seed_run(library, status="completed", goal=goal)
    seed_recording(
        library,
        failed,
        f"java.lang.NoSuchMethodException: android.view.SurfaceControl.createDisplay {goal} api_key=sk-secret-1234567890123456",
    )
    readable = seed_run(library, status="completed")
    seed_recording(library, readable, "Recording finalization failed; retry the run")
    partial_goal = seed_run(
        library,
        status="completed",
        goal="transfer the private report to customer-834",
    )
    seed_recording(
        library,
        partial_goal,
        "java.lang.NoSuchMethodException: unable to transfer report for customer 834",
    )

    await collect(admin)
    view = await failures(admin)
    failed_rows = by_session(view, failed)
    readable_rows = by_session(view, readable)
    partial_goal_rows = by_session(view, partial_goal)

    assert [row["signal"] for row in failed_rows].count("recording") == 1
    assert [row["signal"] for row in failed_rows].count("raw_error_text") == 1
    assert "raw_error_text" not in [row["signal"] for row in readable_rows]
    recording_row = next(row for row in failed_rows if row["signal"] == "recording")
    assert "NoSuchMethodException" in recording_row["evidence"]
    assert all(goal not in row["evidence"] for row in failed_rows)
    assert all("sk-secret-1234567890123456" not in row["evidence"] for row in failed_rows)
    assert goal not in json.dumps(view)
    partial_json = json.dumps(partial_goal_rows).lower()
    assert "transfer" not in partial_json
    assert "report" not in partial_json
    assert "customer" not in partial_json


@pytest.mark.asyncio
async def test_raw_error_text_collects_step_and_run_exceptions_only(library, admin):
    step = seed_run(
        library,
        status="completed",
        steps=[("click", _bad("java.lang.IllegalStateException: step failed"))],
    )
    run = seed_run(
        library,
        status="failed",
        stdout="RuntimeError: worker failed\n    at app.Worker.run(Worker.java:42)\n",
    )
    readable = seed_run(
        library,
        status="completed",
        steps=[("click", _bad("Upload failed. Error: disk full"))],
    )
    plain_error = seed_run(
        library,
        status="completed",
        steps=[("click", _bad("Error: disk full"))],
    )

    await collect(admin)

    view = await failures(admin)
    assert "raw_error_text" in [row["signal"] for row in by_session(view, step)]
    assert "raw_error_text" in [row["signal"] for row in by_session(view, run)]
    assert "raw_error_text" not in [row["signal"] for row in by_session(view, readable)]
    assert "raw_error_text" not in [row["signal"] for row in by_session(view, plain_error)]


@pytest.mark.asyncio
async def test_run_evidence_prefers_exception_line_over_trailing_stack_frames(library, admin):
    sid = seed_run(
        library,
        status="failed",
        stdout="java.lang.RuntimeException: worker failed\n    at app.Worker.run(Worker.java:42)\n",
    )

    await collect(admin)

    run_row = next(
        row for row in by_session(await failures(admin), sid) if row["signal"] == "run_step"
    )
    assert run_row["evidence"].startswith("java.lang.RuntimeException: worker failed")


@pytest.mark.asyncio
async def test_prompt_side_raw_error_evidence_is_fully_redacted(library, admin):
    goal = "Install SecretMap 847"
    sid = seed_run(
        library,
        status="completed",
        goal=goal,
        steps=[("click", _bad(f"RuntimeError: Error finding package for app: {goal}"))],
    )

    await collect(admin)

    view = await failures(admin)
    raw_error = next(row for row in by_session(view, sid) if row["signal"] == "raw_error_text")
    assert raw_error["category"] == "user_prompt"
    assert raw_error["evidence"] == "[prompt-side evidence redacted]"
    assert goal not in json.dumps(view)


@pytest.mark.asyncio
async def test_stuck_run_uses_one_hour_threshold(library, admin):
    stuck = seed_run(library, status="running")
    recent = seed_run(library, status="queued")
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE sessions SET start_time = ? WHERE session_id = ?",
            (time.time() - 61 * 60, stuck),
        )
        conn.execute(
            "UPDATE sessions SET start_time = ? WHERE session_id = ?",
            (time.time() - 59 * 60, recent),
        )

    await collect(admin)

    (row,) = by_session(await failures(admin), stuck)
    assert (row["signal"], row["rule"], row["scope"]) == ("stuck_run", "run_stuck", "run")
    assert by_session(await failures(admin), recent) == []


@pytest.mark.asyncio
async def test_failure_causes_have_stable_keys_counts_run_ids_and_prompt_side(library, admin):
    error = "java.lang.NoSuchMethodException: android.view.SurfaceControl.createDisplay"
    first = seed_run(library, status="completed")
    second = seed_run(library, status="completed")
    seed_recording(library, first, error)
    seed_recording(library, second, error)
    prompt = seed_run(
        library,
        status="completed",
        steps=[("click", _bad("Error finding package for app: ClimaMap"))],
    )
    await collect(admin)
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE failure_ledger SET occurred_at = ? WHERE session_id = ? AND signal = 'raw_error_text'",
            (time.time() - 3 * 86400, first),
        )

    view = await failures(admin, days=14)
    raw_causes = [cause for cause in view["causes"] if cause["signal"] == "raw_error_text"]
    prompt_cause = next(cause for cause in view["causes"] if cause["category"] == "user_prompt")

    assert len(raw_causes) == 1
    cause = raw_causes[0]
    assert cause["count_24h"] == 1
    assert cause["count_window"] == cause["count"] == 2
    assert cause["first_seen"] < cause["last_seen"]
    assert set(cause["run_ids"]) == {first, second}
    assert cause["device"] == SERIAL
    assert cause["evidence_excerpt"]
    assert cause["defect_key"]
    assert cause["smartqa_side"] is True
    assert prompt_cause["smartqa_side"] is False
    assert by_session(view, prompt)[0]["signal"] == "run_step"


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_word", ["token", "password"])
@pytest.mark.parametrize("path", ["recording", "step"])
async def test_goal_credential_words_do_not_bypass_api_evidence_redaction(
    library, admin, goal_word, path
):
    secret = "unredactedvalue"
    evidence = f"Tap failed at (540, 1200) with {goal_word}={secret}"
    sid = seed_run(
        library,
        status="completed",
        goal=goal_word,
        steps=[("click", _bad(evidence))] if path == "step" else (),
    )
    if path == "recording":
        seed_recording(library, sid, evidence)

    await collect(admin)

    view = await failures(admin)
    assert secret not in json.dumps(view)
    assert "[REDACTED]" in json.dumps(view)


@pytest.mark.asyncio
async def test_failure_cause_counts_runs_separately_from_rows(library, admin):
    sid = seed_run(
        library,
        status="completed",
        steps=[("click", _bad(EMPTY_LIST)), ("click", _bad(EMPTY_LIST))],
    )

    await collect(admin)

    causes = await failures(admin)
    cause = next(
        cause
        for cause in causes["causes"]
        if cause["signal"] == "run_step" and sid in cause["run_ids"]
    )
    assert cause["count"] == cause["count_24h"] == cause["count_window"] == 2
    assert cause["runs_24h"] == cause["runs_window"] == 1


@pytest.mark.asyncio
async def test_goal_word_redaction_does_not_split_defect_keys_or_leak_cause(library, admin):
    error = "Tap failed at (540, 1200) on the Settings button"
    first = seed_run(
        library,
        status="completed",
        goal="Settings",
        steps=[("click", _bad(error))],
    )
    second = seed_run(
        library,
        status="completed",
        goal="Tap",
        steps=[("click", _bad(error))],
    )

    await collect(admin)

    view = await failures(admin)
    causes = [
        cause
        for cause in view["causes"]
        if cause["signal"] == "run_step" and cause["rule"] == "tap_failed"
    ]

    assert len(causes) == 1
    assert causes[0]["count"] == 2
    assert "cause" not in causes[0]
    first_row = next(row for row in by_session(view, first) if row["signal"] == "run_step")
    second_row = next(row for row in by_session(view, second) if row["signal"] == "run_step")
    assert "Settings" not in first_row["evidence"]
    assert "Tap" not in second_row["evidence"]


@pytest.mark.asyncio
async def test_api_omits_goal_words_from_cause_when_error_names_goal_label(library, admin):
    goal = "Settings"
    sid = seed_run(
        library,
        status="completed",
        goal=goal,
        steps=[("click", _bad("Tap failed at (540, 1200) on the Settings button"))],
    )

    await collect(admin)

    view = await failures(admin)
    assert goal not in json.dumps(view)
    assert goal not in json.dumps(by_session(view, sid))


@pytest.mark.asyncio
async def test_prompt_causes_do_not_starve_smartqa_causes(library, admin):
    for index in range(21):
        seed_run(
            library,
            status="completed",
            steps=[("click", _bad(f"Error finding package for app: PromptApp-{chr(65 + index)}"))],
        )
    smartqa = seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])

    await collect(admin)

    causes = (await failures(admin))["causes"]
    smartqa_causes = [cause for cause in causes if cause["category"] == "smartqa_agent"]
    assert any(smartqa in cause["run_ids"] for cause in smartqa_causes)
    assert len([cause for cause in causes if cause["category"] == "user_prompt"]) <= 20


def test_defect_key_uses_category_and_normalizes_device_identifiers():
    first = failure_ledger._cause(
        "device_unavailable", "Device emulator-5554 disconnected id abcdef1234567890"
    )
    second = failure_ledger._cause(
        "device_unavailable", "Device emulator-5556 disconnected id fedcba0987654321"
    )

    first_key = failure_ledger._defect_key("smartqa_infra", "device_unavailable", first)
    second_key = failure_ledger._defect_key("smartqa_infra", "device_unavailable", second)

    assert first_key == second_key
    assert first_key != failure_ledger._defect_key("unknown", "device_unavailable", first)


@pytest.mark.asyncio
async def test_ledger_migration_preserves_existing_rows(library, admin):
    goal = "legacy private goal"
    sid = seed_run(library, status="completed", goal=goal)
    occurred = time.time()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            """CREATE TABLE failure_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                step_number INTEGER NOT NULL,
                scope TEXT NOT NULL,
                category TEXT NOT NULL,
                rule TEXT NOT NULL,
                cause TEXT NOT NULL,
                evidence TEXT NOT NULL,
                device TEXT,
                source TEXT NOT NULL,
                owner TEXT,
                occurred_at REAL NOT NULL,
                classified_at REAL NOT NULL,
                UNIQUE (session_id, step_number)
            )"""
        )
        conn.execute(
            "INSERT INTO failure_ledger "
            "(session_id, step_number, scope, category, rule, cause, evidence, device, source, "
            "owner, occurred_at, classified_at) VALUES (?, 1, 'step', 'smartqa_agent', "
            "'empty_target_list', ?, ?, ?, 'browser', ?, ?, ?)",
            (
                sid,
                f"legacy-cause|{goal}",
                f"legacy evidence {goal}",
                SERIAL,
                "qa@example.com",
                occurred,
                occurred,
            ),
        )
        conn.execute(
            "INSERT INTO failure_ledger "
            "(session_id, step_number, scope, category, rule, cause, evidence, device, source, "
            "owner, occurred_at, classified_at) VALUES (?, 0, 'run', 'unknown', 'no_match', ?, ?, "
            "?, 'browser', ?, ?, ?)",
            (
                sid,
                f"legacy-run-cause|{goal}",
                f"legacy run evidence {goal}",
                SERIAL,
                "qa@example.com",
                occurred,
                occurred,
            ),
        )

    await collect(admin)
    view = await failures(admin)
    rows = by_session(view, sid)

    assert [row["signal"] for row in rows] == ["run_step", "run_step"]
    assert goal not in json.dumps(view)
    with sqlite3.connect(library.db) as conn:
        columns = {record[1] for record in conn.execute("PRAGMA table_info(failure_ledger)")}
        assert "signal" in columns
        assert (
            goal
            not in conn.execute(
                "SELECT cause FROM failure_ledger WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO failure_ledger "
            "(session_id, step_number, signal, scope, category, rule, cause, evidence, source, "
            "occurred_at, classified_at) VALUES (?, 1, 'raw_error_text', 'step', 'unknown', "
            "'raw_error_text', 'second-signal', 'second evidence', 'browser', ?, ?)",
            (sid, occurred, occurred),
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM failure_ledger WHERE session_id = ? AND step_number = 1",
                (sid,),
            ).fetchone()[0]
            == 2
        )
        for cause in ("run_stuck|device timeout", "run_interrupted|device offline"):
            conn.execute(
                "INSERT INTO failure_ledger "
                "(session_id, step_number, signal, scope, category, rule, cause, evidence, source, "
                "occurred_at, classified_at) VALUES (?, 0, 'stuck_run', 'run', 'smartqa_infra', "
                "'run_stuck', ?, 'run evidence', 'browser', ?, ?)",
                (sid, cause, occurred, occurred),
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM failure_ledger WHERE session_id = ? AND step_number = 0 "
                "AND signal = 'stuck_run'",
                (sid,),
            ).fetchone()[0]
            == 2
        )


@pytest.mark.asyncio
async def test_migration_corrects_legacy_failed_run_signal_without_recollection(library, admin):
    sid = seed_run(library, status="failed", stdout=f"{OFFLINE}\n")
    occurred = time.time()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            """CREATE TABLE failure_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                step_number INTEGER NOT NULL,
                signal TEXT NOT NULL,
                scope TEXT NOT NULL,
                category TEXT NOT NULL,
                rule TEXT NOT NULL,
                cause TEXT NOT NULL,
                evidence TEXT NOT NULL,
                device TEXT,
                source TEXT NOT NULL,
                owner TEXT,
                occurred_at REAL NOT NULL,
                classified_at REAL NOT NULL,
                UNIQUE (session_id, step_number, signal)
            )"""
        )
        conn.execute(
            "INSERT INTO failure_ledger "
            "(session_id, step_number, signal, scope, category, rule, cause, evidence, source, "
            "occurred_at, classified_at) VALUES (?, 0, 'stuck_run', 'run', 'unknown', 'no_match', "
            "'legacy-cause', 'legacy evidence', 'browser', ?, ?)",
            (sid, occurred, occurred),
        )

    await collect(admin)

    rows = by_session(await failures(admin), sid)
    assert {row["signal"] for row in rows} == {"run_step"}


@pytest.mark.asyncio
async def test_migration_maps_legacy_interrupted_run_to_stuck_run(library, admin):
    sid = seed_run(library, status="interrupted", interrupt="device_offline")
    occurred = time.time()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            """CREATE TABLE failure_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                step_number INTEGER NOT NULL,
                scope TEXT NOT NULL,
                category TEXT NOT NULL,
                rule TEXT NOT NULL,
                cause TEXT NOT NULL,
                evidence TEXT NOT NULL,
                device TEXT,
                source TEXT NOT NULL,
                owner TEXT,
                occurred_at REAL NOT NULL,
                classified_at REAL NOT NULL,
                UNIQUE (session_id, step_number)
            )"""
        )
        conn.execute(
            "INSERT INTO failure_ledger "
            "(session_id, step_number, scope, category, rule, cause, evidence, source, "
            "occurred_at, classified_at) VALUES (?, 0, 'run', 'smartqa_infra', 'run_interrupted', "
            "'legacy-interruption', 'interrupted: device_offline', 'browser', ?, ?)",
            (sid, occurred, occurred),
        )

    await collect(admin)

    rows = by_session(await failures(admin), sid)
    assert len(rows) == 1
    assert (rows[0]["signal"], rows[0]["rule"]) == ("stuck_run", "run_interrupted")


@pytest.mark.asyncio
async def test_migration_remaps_interrupted_run_step_but_preserves_raw_error_signal(library, admin):
    sid = seed_run(library, status="interrupted", interrupt="device_offline")
    occurred = time.time()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            """CREATE TABLE failure_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                step_number INTEGER NOT NULL,
                signal TEXT NOT NULL,
                scope TEXT NOT NULL,
                category TEXT NOT NULL,
                rule TEXT NOT NULL,
                cause TEXT NOT NULL,
                evidence TEXT NOT NULL,
                device TEXT,
                source TEXT NOT NULL,
                owner TEXT,
                occurred_at REAL NOT NULL,
                classified_at REAL NOT NULL,
                UNIQUE (session_id, step_number, signal)
            )"""
        )
        conn.executemany(
            "INSERT INTO failure_ledger "
            "(session_id, step_number, signal, scope, category, rule, cause, evidence, source, "
            "occurred_at, classified_at) VALUES (?, 0, ?, 'run', 'smartqa_infra', ?, ?, ?, "
            "'browser', ?, ?)",
            [
                (
                    sid,
                    "run_step",
                    "run_interrupted",
                    "legacy-run",
                    "interrupted: device_offline",
                    occurred,
                    occurred,
                ),
                (
                    sid,
                    "raw_error_text",
                    "raw_error_text",
                    "legacy-error",
                    "RuntimeError: offline",
                    occurred,
                    occurred,
                ),
            ],
        )

    await collect(admin)

    rows = by_session(await failures(admin), sid)
    assert {(row["signal"], row["rule"]) for row in rows} == {
        ("stuck_run", "run_interrupted"),
        ("raw_error_text", "raw_error_text"),
    }


@pytest.mark.asyncio
async def test_evidence_is_redacted(library, admin):
    sid = seed_run(
        library,
        status="completed",
        steps=[("click", _bad("Tap failed at (1, 2) with api_key=sk-secret-123456"))],
    )
    await collect(admin)
    (row,) = by_session(await failures(admin), sid)
    assert "sk-secret-123456" not in row["evidence"]


# -- ledger behaviour ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_is_idempotent(library, admin):
    sid = seed_run(library, status="interrupted", steps=[("click", _bad(EMPTY_LIST))])
    first = await collect(admin)
    again = await collect(admin)
    assert first["inserted"] > 0
    assert again["inserted"] == 0
    assert len(by_session(await failures(admin), sid)) == first["inserted"]


@pytest.mark.asyncio
async def test_a_run_is_picked_up_once_it_fails(library, admin):
    sid = seed_run(library, status="running", steps=[("click", _bad(EMPTY_LIST))])
    await collect(admin)
    assert [r["scope"] for r in by_session(await failures(admin), sid)] == ["step"]
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE sessions SET status = 'failed' WHERE session_id = ?", (sid,))
    library.write(sid, "stdout.log", f"{OFFLINE}\n")
    await collect(admin)
    rows = by_session(await failures(admin), sid)
    assert {(row["scope"], row["signal"]) for row in rows} == {
        ("run", "run_step"),
        ("run", "raw_error_text"),
        ("step", "run_step"),
    }


@pytest.mark.asyncio
async def test_view_counts_by_day_and_category_and_ranks_repeated_causes(library, admin):
    for _ in range(3):
        seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])
    seed_run(
        library, status="completed", steps=[("click", _bad("Error finding package for app: Foo"))]
    )
    await collect(admin)

    view = await failures(admin)

    counts = {(c["day"], c["category"]): c["count"] for c in view["counts"]}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert counts[(today, "smartqa_agent")] == 3
    assert counts[(today, "user_prompt")] == 1
    top = view["causes"][0]
    assert (top["rule"], top["count"], top["action"]) == ("empty_target_list", 3, "fix")
    assert len(top["session_ids"]) == 3
    prompt_side = next(c for c in view["causes"] if c["category"] == "user_prompt")
    assert prompt_side["action"] == "no action"


@pytest.mark.asyncio
async def test_window_excludes_old_failures(library, admin):
    sid = seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE steps SET timestamp = ?", (time.time() - 40 * 86400,))
    await collect(admin)
    assert by_session(await failures(admin, days=14), sid) == []
    assert len(by_session(await failures(admin, days=60), sid)) == 1


# -- digest ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_lists_new_smartqa_causes_once_and_skips_prompt_side(library, admin, notices):
    seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])
    seed_run(library, status="failed", stdout=f"{OFFLINE}\n")
    seed_run(
        library, status="completed", steps=[("click", _bad("Error finding package for app: Foo"))]
    )
    seed_run(library, status="failed", stdout="LLMExhaustedError: Connection error.\n")
    await collect(admin)

    first = (await admin.post("/api/system/failures/digest")).json()

    assert first["sent"] is True
    assert {c["rule"] for c in first["causes"]} == {"empty_target_list", "device_unavailable"}
    assert len(notices) == 1
    assert "Foo" not in notices[0][1]
    assert "Connection error" not in notices[0][1]

    second = (await admin.post("/api/system/failures/digest")).json()
    assert second == {"sent": False, "causes": []}
    assert len(notices) == 1

    seed_run(
        library,
        status="completed",
        steps=[("click", _bad("Error executing click: Tap failed at (1, 2)"))],
    )
    await collect(admin)
    third = (await admin.post("/api/system/failures/digest")).json()
    assert [c["rule"] for c in third["causes"]] == ["tap_failed"]
    assert len(notices) == 2


@pytest.mark.asyncio
async def test_digest_repeats_until_a_delivery_succeeds(library, admin, notices):
    seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])
    await collect(admin)
    notices.delivered = False
    assert (await admin.post("/api/system/failures/digest")).json()["sent"] is False
    notices.delivered = True
    assert (await admin.post("/api/system/failures/digest")).json()["sent"] is True
    assert len(notices) == 2


# -- access ------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/system/failures"),
        ("post", "/api/system/failures/collect"),
        ("post", "/api/system/failures/digest"),
    ],
)
async def test_only_admins_reach_the_failure_endpoints(library, qa, anonymous, method, path):
    for client in (qa, anonymous):
        assert (await getattr(client, method)(path)).status_code in (401, 403)


# -- deletion, retention and concurrency -----------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_run_removes_its_failures_from_view_and_digest(library, admin, notices):
    gone = seed_run(library, status="failed", stdout=f"{OFFLINE}\n")
    kept = seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])
    await collect(admin)

    assert (await admin.post(f"/api/runs/{gone}/delete")).status_code == 200

    view = await failures(admin)
    assert by_session(view, gone) == []
    assert len(by_session(view, kept)) == 1
    assert all(gone not in cause["session_ids"] for cause in view["causes"])
    assert library.count("failure_ledger", gone) == 0
    sent = (await admin.post("/api/system/failures/digest")).json()
    assert [c["rule"] for c in sent["causes"]] == ["empty_target_list"]
    assert gone[:8] not in notices[0][1]


@pytest.mark.asyncio
async def test_a_tombstoned_run_is_hidden_before_its_cleanup_finishes(library, admin, notices):
    sid = seed_run(library, status="failed", stdout=f"{OFFLINE}\n")
    await collect(admin)
    with sqlite3.connect(library.db) as conn:  # deleted, files still waiting on a lease
        conn.execute("UPDATE run_meta SET deleted_at = 1.0 WHERE session_id = ?", (sid,))

    assert by_session(await failures(admin), sid) == []
    assert (await admin.post("/api/system/failures/digest")).json() == {"sent": False, "causes": []}
    assert await collect(admin) == {"scanned": 0, "inserted": 0}


@pytest.mark.asyncio
async def test_retention_sweep_purges_ledger_rows(library, admin):
    from apps.admin_console.services import run_retention

    old = seed_run(library, status="failed", age_days=40, stdout=f"{OFFLINE}\n")
    await collect(admin)
    assert library.count("failure_ledger", old) == 2
    assert (await admin.put("/api/system/retention", json={"days": 30})).status_code == 200
    assert (await admin.post("/api/system/retention/dry-run")).status_code == 200
    assert (await admin.put("/api/system/retention", json={"enabled": True})).status_code == 200

    await asyncio.to_thread(run_retention.enforce)

    assert library.count("failure_ledger", old) == 0
    assert by_session(await failures(admin), old) == []


@pytest.mark.asyncio
async def test_concurrent_digests_deliver_each_cause_once(library, admin, notices, monkeypatch):
    seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])
    await collect(admin)
    real_send = failure_ledger._send

    def slow_send(title, message):
        time.sleep(0.2)  # both callers are inside digest() before either finishes
        return real_send(title, message)

    monkeypatch.setattr(failure_ledger, "_send", slow_send)

    first, second = await asyncio.gather(
        admin.post("/api/system/failures/digest"), admin.post("/api/system/failures/digest")
    )

    assert len(notices) == 1
    assert sorted(r.json()["sent"] for r in (first, second)) == [False, True]


@pytest.mark.asyncio
async def test_a_failed_concurrent_delivery_is_retried(library, admin, notices):
    seed_run(library, status="completed", steps=[("click", _bad(EMPTY_LIST))])
    await collect(admin)
    notices.delivered = False
    results = await asyncio.gather(
        admin.post("/api/system/failures/digest"), admin.post("/api/system/failures/digest")
    )
    assert all(r.json()["sent"] is False for r in results)
    notices.delivered = True
    assert (await admin.post("/api/system/failures/digest")).json()["sent"] is True
