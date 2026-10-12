import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.admin_console.database.connection import db_session
from apps.admin_console.services import automatic_prompt_tips as tips
from tests.unit.admin_console.test_prompt_critiques import model as model


@pytest.mark.asyncio
@pytest.mark.parametrize("test_outcome", ["pass", "fail"])
async def test_completed_execution_automatically_uses_default_model_once(
    library, model, test_outcome
):
    identity = library.seed(goal="Check login", status="completed")
    library.write(
        identity,
        "run_outcome.json",
        json.dumps(
            {
                "tests": {
                    "failed": int(test_outcome == "fail"),
                    "passed": int(test_outcome == "pass"),
                }
            }
        ),
    )
    await asyncio.gather(tips.ensure_tip(identity), tips.ensure_tip(identity))
    await tips.ensure_tip(identity)
    resolve, complete = model
    resolve.assert_awaited_once_with(None)
    complete.assert_awaited_once()
    tip = tips.read_tip(identity)
    assert tip["state"] == "complete"
    assert tip["text"] == "Open Settings and verify the account email is visible."
    with db_session(library.db) as connection, connection:
        assert connection.execute("SELECT count(*) FROM prompt_critiques").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["failed", "interrupted", "cancelled", "running", "queued", "paused"]
)
async def test_incomplete_execution_makes_no_model_call(library, model, status):
    identity = library.seed(status=status)
    await tips.ensure_tip(identity)
    assert tips.read_tip(identity) is None
    resolve, complete = model
    resolve.assert_not_awaited()
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_tip_is_terminal_and_does_not_retry_or_echo_errors(library, model, caplog):
    identity = library.seed()
    resolve, complete = model
    complete.side_effect = RuntimeError("SECRET-IN-MODEL-ERROR")
    await tips.ensure_tip(identity)
    await tips.ensure_tip(identity)
    assert tips.read_tip(identity) == {"state": "failed", "text": None}
    complete.assert_awaited_once()
    assert "SECRET-IN-MODEL-ERROR" not in caplog.text


def test_stale_claim_does_not_leave_terminal_polling_running_forever(library):
    identity = library.seed()
    with db_session(library.db) as connection, connection:
        connection.execute(
            "INSERT INTO prompt_critiques(id,session_id,created_at,model,original_prompt,improved_prompt,changes_json,lessons_json,automatic_state) VALUES (?,?,1,'pending','goal','','[]','[]','generating')",
            (f"automatic:{identity}", identity),
        )
    assert tips.read_tip(identity) == {"state": "failed", "text": None}


@pytest.mark.asyncio
async def test_automatic_tip_input_never_uses_private_request_events(library, model):
    from artemis.data_engine.chat_sessions import ChatStore

    identity = library.seed(goal="Check login password=hunter22")
    store = ChatStore(library.db)
    store.record_event(
        identity,
        "input_request",
        "PRIVATE-REQUEST-SENTINEL",
        payload={"state": "sent", "secret": True},
    )
    await tips.ensure_tip(identity)
    resolve, complete = model
    prompts = complete.call_args.args[1]
    text = "\n".join(message.content for message in prompts)
    assert "PRIVATE-REQUEST-SENTINEL" not in text
    assert "hunter22" not in text


@pytest.mark.asyncio
async def test_sdk_finalization_triggers_tip_only_for_managed_completed_execution(monkeypatch):
    from artemis.sdk.agent import Agent

    agent = object.__new__(Agent)
    generate = AsyncMock()
    monkeypatch.setattr(tips, "ensure_tip", generate)
    context = SimpleNamespace(
        data_engine=SimpleNamespace(session_id="run", end_session=Mock()), execution_setup=None
    )
    monkeypatch.setenv("ARTEMIS_TASK_WORKER", "1")
    await agent._finalize_tracing_safely(
        SimpleNamespace(status="completed", get_name=lambda: "test"), context
    )
    generate.assert_awaited_once_with("run")
    for status in ("failed", "interrupted", "cancelled"):
        await agent._finalize_tracing_safely(
            SimpleNamespace(status=status, get_name=lambda: "test"), context
        )
    assert generate.await_count == 1
    monkeypatch.delenv("ARTEMIS_TASK_WORKER")
    await agent._finalize_tracing_safely(
        SimpleNamespace(status="completed", get_name=lambda: "test"), context
    )
    assert generate.await_count == 1
