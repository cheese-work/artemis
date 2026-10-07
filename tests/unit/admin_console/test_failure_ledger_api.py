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

import json
import sqlite3
import time

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
    status="failed",
    steps=(),
    interrupt=None,
    stdout=None,
    owner="qa@example.com",
    host_id=None,
    sid=None,
):
    sid = library.seed(status=status, sid=sid)
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
    assert (await collect(admin))["inserted"] == 5

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
    (run_row,) = by_session(view, offline)
    assert (run_row["category"], run_row["rule"]) == ("smartqa_infra", "device_unavailable")
    assert run_row["device"] == SERIAL
    assert run_row["owner"] == "qa@example.com"
    assert run_row["source"] == "browser"
    assert "is not available" in run_row["evidence"]
    assert {"day", "category", "count"} <= set(view["counts"][0])
    infra = next(c for c in view["causes"] if c["category"] == "smartqa_infra")
    assert offline in infra["session_ids"] or video in infra["session_ids"]


@pytest.mark.asyncio
async def test_host_runs_are_labelled_by_source(library, admin):
    sid = seed_run(library, stdout=f"{OFFLINE}\n", host_id="host-1")
    await collect(admin)
    assert by_session(await failures(admin), sid)[0]["source"] == "host"


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
    ("Error executing key press 'APP_SWITCH'.", "unknown", "no_match"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "category", "rule"), CASES)
async def test_classifier_table(library, admin, text, category, rule):
    sid = seed_run(library, status="completed", steps=[("click", _bad(text))])
    await collect(admin)
    (row,) = by_session(await failures(admin), sid)
    assert (row["category"], row["rule"]) == (category, rule)
    assert row["action"] == ACTION[category.split("_")[0]]


@pytest.mark.asyncio
async def test_run_without_a_recognisable_cause_is_unknown(library, admin):
    sid = seed_run(library, status="failed", stdout="ℹ nothing useful here\n")
    await collect(admin)
    (row,) = by_session(await failures(admin), sid)
    assert (row["category"], row["rule"]) == ("unknown", "no_match")


@pytest.mark.asyncio
async def test_interrupted_run_is_infra_by_its_end_reason(library, admin):
    sid = seed_run(library, status="interrupted", interrupt="server_restarted")
    await collect(admin)
    (row,) = by_session(await failures(admin), sid)
    assert (row["category"], row["rule"]) == ("smartqa_infra", "run_interrupted")


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
    assert [r["scope"] for r in by_session(await failures(admin), sid)] == ["run", "step"]


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
