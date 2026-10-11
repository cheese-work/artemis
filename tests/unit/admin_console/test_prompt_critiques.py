"""Prompt improvement uses a default model only on click and keeps redacted lessons."""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, Mock
import uuid

from langchain_core.messages import AIMessage
import pytest

from apps.admin_console.database.repositories import prompt_critique_repository as repo
from apps.admin_console.services import prompt_improvement as service
from apps.admin_console.services.run_purge import purge_run
from artemis.llm.structured import ParseFailure, parse_structured
from artemis.services import llm as llm_service

OUTPUT = {
    "improved_prompt": "Open Settings and verify the account email is visible.",
    "changes": [{"what": "Name the expected screen", "why": "The goal was ambiguous"}],
    "lessons": ["Specify an observable result."],
}


def owned_run(library, owner="qa@example.com", **kwargs):
    sid = library.seed(**kwargs)
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE run_meta SET requested_by = ? WHERE session_id = ?", (owner, sid))
    return sid


@pytest.fixture
def model(monkeypatch):
    resolve = AsyncMock(return_value={"provider": "openai", "model": "server-default"})
    monkeypatch.setattr(service.model_discovery, "resolve", resolve)
    monkeypatch.setattr(service, "build_model", Mock(return_value=object()))
    complete = AsyncMock(return_value=service.PromptImprovement(**OUTPUT))
    monkeypatch.setattr(service, "acomplete_structured", complete)
    return resolve, complete


@pytest.mark.parametrize(
    "payload",
    [
        OUTPUT,
        {"changes": OUTPUT["changes"], "lessons": ["x"]},
        {**OUTPUT, "changes": []},
        {**OUTPUT, "improved_prompt": "  "},
        {**OUTPUT, "lessons": []},
    ],
)
def test_schema(payload):
    parsed = parse_structured(json.dumps(payload), schema=service.PromptImprovement)
    assert isinstance(parsed, service.PromptImprovement if payload == OUTPUT else ParseFailure)


def test_input_builder():
    steps = [
        {
            "step_number": n,
            "action_taken": {"action": "tap"},
            "last_execution_result": {"success": False},
            "extra_metadata": {"injected_instruction": f"steer {n}"},
        }
        for n in range(1, 4)
    ]
    result = json.loads(
        service.build_input(
            {"initial_goal": "Log in password=hunter22", "status": "failed"},
            steps,
            {"task_status": "failed", "tests": {"failed": 1}},
        )
    )
    assert "hunter22" not in json.dumps(result)
    assert "Log in" in result["original_prompt"]
    assert [s["text"] for s in result["steers"]] == ["steer 1", "steer 2", "steer 3"]
    assert result["stuck"] is True
    assert all(s["failed"] for s in result["steps"])
    assert result["verdict"]["task_status"] == "failed"
    assert result["verdict"]["status"] == "failed"
    assert result["verdict"]["tests"] == {"failed": 1}
    assert json.loads(service.build_input({"status": "interrupted"}, [], {}))["stuck"]
    steps.insert(1, {"step_number": 9, "last_execution_result": {"success": True}})
    assert not json.loads(service.build_input({"status": "completed"}, steps, {}))["stuck"]


@pytest.mark.parametrize(
    "step",
    [
        {"last_execution_result": {"execution": [{"attempts": ["Target not found"]}]}},
        {"action_taken": {"error": "broken"}},
        {"action_taken": {"action": "report_task_status", "args": {"status": "failed"}}},
        {"last_execution_result": '{"is_successful": false}'},
        {"last_execution_result": {"repair_status": "fixed"}},
        {"last_execution_result": {"execution": [{"attempts": [{"success": False}]}]}},
    ],
)
def test_failure_shapes(step):
    assert json.loads(service.build_input({}, [step], {}))["steps"][0]["failed"]


def test_input_cap_prioritizes_failure_then_recent_steps():
    steps = [{"step_number": n, "summary": "x" * 2000} for n in range(1000)]
    steps[0]["last_execution_result"] = {"success": False}
    text = service.build_input({"initial_goal": "g" * 100000}, steps, {})
    result = json.loads(text)
    assert len(text) <= service.MAX_INPUT_CHARS
    assert result["steps"][0]["step_number"] == 0
    assert 999 in [s["step_number"] for s in result["steps"]]
    assert all(len(s["summary"]) <= 400 for s in result["steps"])


def test_wrapper_always_has_selected_model(monkeypatch):
    raw = object()
    factory = Mock(return_value=raw)
    wrapper = Mock()
    monkeypatch.setattr(service.ModelFactory, "get_model", factory)
    monkeypatch.setattr(service, "RobustChatModelWrapper", wrapper)
    service.build_model({"provider": "openai", "model": "server-default"})
    assert wrapper.call_args.kwargs["selected_model"] == "openai/server-default"
    assert wrapper.call_args.kwargs["ctx"] is None
    assert factory.call_args.args[0].model_name == "server-default"


@pytest.mark.asyncio
async def test_click_stores_each_time_and_get_returns_newest(library, qa, model):
    sid = owned_run(library, goal="Settings password=hunter22")
    library.write(
        sid, "run_outcome.json", json.dumps({"task_status": "failed", "tests": {"failed": 2}})
    )
    step_id = library.step(sid, 1)
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE steps SET extra_metadata = ? WHERE step_id = ?",
            (json.dumps({"injected_instruction": "Check email too"}), step_id),
        )
        conn.execute("UPDATE run_meta SET agent_model = 'run-model' WHERE session_id = ?", (sid,))
    path = f"/api/sessions/{sid}/improve-prompt"
    first = await qa.post(path)
    assert first.status_code == 200, first.text
    second = await qa.post(path)
    assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["model"] == "openai/server-default"
    assert "hunter22" not in second.text
    assert library.count("prompt_critiques", sid) == 2
    newest = await qa.get(f"/api/sessions/{sid}/prompt-critique")
    assert newest.json()["id"] == second.json()["id"]
    resolve, complete = model
    assert resolve.call_args.args == (None,)
    assert complete.await_count == 2
    sent = str(complete.call_args.args[1])
    assert "Check email too" in sent and "hunter22" not in sent
    assert complete.call_args.kwargs["correction_attempts"] == 1


@pytest.mark.asyncio
async def test_authorization_and_active(library, qa, qa2, admin, anonymous, model):
    sid = owned_run(library)
    path = f"/api/sessions/{sid}/improve-prompt"
    assert (await qa2.post(path)).json()["code"] == "not_run_owner"
    assert (await qa2.post(path)).status_code == 403
    assert (await anonymous.post(path)).status_code == 401
    assert (await admin.post(path)).status_code == 200
    assert (await qa2.get(f"/api/sessions/{sid}/prompt-critique")).status_code == 200
    missing = await qa.post(f"/api/sessions/{uuid.uuid4()}/improve-prompt")
    assert missing.status_code == 404 and missing.json()["code"] == "run_not_visible"
    no_critique = owned_run(library)
    assert (await qa.get(f"/api/sessions/{no_critique}/prompt-critique")).status_code == 404
    for status in ("running", "queued", "paused"):
        active = owned_run(library, status=status)
        response = await qa.post(f"/api/sessions/{active}/improve-prompt")
        assert response.status_code == 409 and response.json()["code"] == "run_active"


@pytest.mark.asyncio
async def test_parse_reask_failure_stores_nothing(library, qa, model, monkeypatch):
    sid = owned_run(library)
    monkeypatch.setattr(service, "acomplete_structured", llm_service.acomplete_structured)
    complete = AsyncMock(return_value=AIMessage(content="not JSON"))
    monkeypatch.setattr(llm_service, "acomplete", complete)
    response = await qa.post(f"/api/sessions/{sid}/improve-prompt")
    assert response.status_code == 502 and response.json()["code"] == "improve_parse_failed"
    assert complete.await_count == 2
    assert "Re-emit ONLY" in complete.call_args.args[1][-1].content
    assert library.count("prompt_critiques", sid) == 0
    complete.return_value = AIMessage(content=json.dumps(OUTPUT))
    assert (await qa.post(f"/api/sessions/{sid}/improve-prompt")).status_code == 200


@pytest.mark.asyncio
async def test_model_failure_and_timeout(library, qa, model, monkeypatch):
    sid = owned_run(library)
    model[1].side_effect = RuntimeError("unavailable password=hunter22")
    response = await qa.post(f"/api/sessions/{sid}/improve-prompt")
    assert response.status_code == 502 and response.json()["code"] == "improve_model_failed"
    assert "openai/server-default" in response.text and "unavailable" in response.text
    assert "hunter22" not in response.text
    assert library.count("prompt_critiques", sid) == 0

    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    model[1].side_effect = slow
    monkeypatch.setattr(service, "TIMEOUT_SECONDS", 0.01)
    response = await qa.post(f"/api/sessions/{sid}/improve-prompt")
    assert response.status_code == 502 and "timed out" in response.text


@pytest.mark.asyncio
async def test_in_flight(library, qa, model):
    sid = owned_run(library)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        await release.wait()
        return service.PromptImprovement(**OUTPUT)

    model[1].side_effect = block
    request = asyncio.create_task(qa.post(f"/api/sessions/{sid}/improve-prompt"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        other = await qa.post(f"/api/sessions/{sid}/improve-prompt")
        assert other.status_code == 409 and other.json()["code"] == "improve_in_flight"
    finally:
        release.set()
        await request


@pytest.mark.asyncio
async def test_lessons_search_scope_removed_and_purge(library, qa, qa2, admin, model):
    mine = owned_run(library, goal="Settings")
    theirs = owned_run(library, "qa2@example.com", goal="Checkout")
    for client, sid in ((qa, mine), (qa, mine), (qa2, theirs)):
        assert (await client.post(f"/api/sessions/{sid}/improve-prompt")).status_code == 200
    assert len((await qa.get("/api/prompt-lessons")).json()["critiques"]) == 2
    assert (await qa.get("/api/prompt-lessons?scope=all")).status_code == 403
    all_rows = (await admin.get("/api/prompt-lessons?scope=all")).json()["critiques"]
    assert len(all_rows) == 3 and all_rows[0]["session_id"] == theirs
    assert len((await qa.get("/api/prompt-lessons?limit=1&offset=1")).json()["critiques"]) == 1
    for q in ("Settings", "email", "ambiguous", "observable"):
        assert (await qa.get("/api/prompt-lessons", params={"q": q})).json()["critiques"]
    assert not (await qa.get("/api/prompt-lessons?q=absent")).json()["critiques"]
    await qa.get(f"/api/sessions/{theirs}/prompt-critique")
    assert len((await qa.get("/api/prompt-lessons?scope=available")).json()["critiques"]) == 3
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?", (mine,))
    assert not (await qa.get("/api/prompt-lessons")).json()["critiques"]
    assert (await admin.get(f"/api/sessions/{mine}/prompt-critique")).status_code == 404
    purge_run(library.db, library.traces, mine)
    assert library.count("prompt_critiques", mine) == 0


@pytest.mark.asyncio
async def test_migration_idempotent_and_orphan_cleanup(library, qa, model):
    sid = owned_run(library)
    await qa.post(f"/api/sessions/{sid}/improve-prompt")
    repo.migrate(library.db)
    repo.migrate(library.db)
    assert library.count("prompt_critiques", sid) == 1
    with sqlite3.connect(library.db) as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    repo.migrate(library.db)
    assert library.count("prompt_critiques", sid) == 0


def test_input_bound_includes_json_escaping_and_preserves_failure_reason():
    text = service.build_input(
        {"initial_goal": "\x00" * 100000},
        [
            {
                "step_number": 1,
                "summary": "x" * 10000,
                "last_execution_result": {"success": False, "error": "Missing checkout button"},
            }
        ],
        {},
    )
    assert len(text) <= service.MAX_INPUT_CHARS
    assert "Missing checkout button" in text


@pytest.mark.asyncio
async def test_generated_secrets_redacted_before_storage(library, qa, model):
    sid = owned_run(library)
    model[1].return_value = service.PromptImprovement(
        improved_prompt="Open settings password=hunter22",
        changes=[{"what": "Use token=private-token", "why": "api_key=private-key"}],
        lessons=["Never share password=hunter22"],
    )
    response = await qa.post(f"/api/sessions/{sid}/improve-prompt")
    assert response.status_code == 200
    with sqlite3.connect(library.db) as conn:
        stored = str(conn.execute("SELECT * FROM prompt_critiques").fetchall())
    for secret in ("hunter22", "private-token", "private-key"):
        assert secret not in response.text and secret not in stored


def test_input_includes_every_steer_when_individual_messages_are_long():
    steps = [
        {"step_number": n, "extra_metadata": {"injected_instruction": f"steer {n} " + "x" * 1000}}
        for n in range(20)
    ]
    result = json.loads(service.build_input({}, steps, {}))
    assert len(result["steers"]) == 20
    assert result["omitted_steers"] == 0


@pytest.mark.asyncio
async def test_removal_during_generation_does_not_leave_a_critique(library, qa, model):
    sid = owned_run(library)

    async def remove(*args, **kwargs):
        with sqlite3.connect(library.db) as conn:
            conn.execute("UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?", (sid,))
        return service.PromptImprovement(**OUTPUT)

    model[1].side_effect = remove
    response = await qa.post(f"/api/sessions/{sid}/improve-prompt")
    assert response.status_code == 404
    assert library.count("prompt_critiques", sid) == 0
