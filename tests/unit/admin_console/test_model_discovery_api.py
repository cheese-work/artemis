import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.core.ownership import SYSTEM_PRINCIPAL
from apps.admin_console.routers import tasks
from apps.admin_console.schemas.task_schema import RunRequest


@pytest.mark.asyncio
async def test_models_endpoint_requires_a_signed_in_identity(qa, anonymous, monkeypatch):
    catalog = {"models": [], "default": {"provider": "openai", "model": "default"}, "errors": []}
    scan = AsyncMock(return_value=catalog)
    monkeypatch.setattr(tasks.model_discovery, "catalog", scan)
    async with anonymous:
        assert (await anonymous.get("/api/models")).status_code == 401
    scan.assert_not_awaited()
    async with qa:
        response = await qa.get("/api/models")
    assert response.status_code == 200
    assert response.json() == catalog


@pytest.mark.asyncio
async def test_invalid_model_is_rejected_before_device_probes(monkeypatch):
    monkeypatch.setattr(
        tasks.model_discovery,
        "resolve",
        AsyncMock(
            side_effect=AdminAPIError(400, "Unavailable", "model_not_available", "Choose a model")
        ),
    )
    probe = AsyncMock()
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", probe)
    enqueue = AsyncMock()
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue)
    with pytest.raises(AdminAPIError):
        await tasks.run_task(
            RunRequest(goal="Open Settings", model={"provider": "openai", "model": "free-text"}),
            SYSTEM_PRINCIPAL,
        )
    probe.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_resolved_choice_or_default_is_bound_to_the_queue(monkeypatch, explicit):
    selected = {"provider": "openai", "model": "selected" if explicit else "default"}
    resolve = AsyncMock(return_value=selected)
    monkeypatch.setattr(tasks.model_discovery, "resolve", resolve)
    monkeypatch.setattr(
        tasks.readiness_engine,
        "run_device_submission_probe",
        AsyncMock(return_value=SimpleNamespace(summary="Ready", metadata={})),
    )
    monkeypatch.setattr(
        tasks.device_pool, "validate_explicit_serial_async", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(tasks.task_queue_service, "require_admission_open", MagicMock())
    enqueue = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue)
    await tasks.run_task(
        RunRequest(
            goal="Open Settings",
            device_serial="fixture-phone",
            model=selected if explicit else None,
        ),
        SYSTEM_PRINCIPAL,
    )
    resolve.assert_awaited_once_with(selected if explicit else None)
    assert enqueue.await_args.kwargs["model"] == selected
    assert enqueue.await_args.kwargs["model_explicit"] is explicit


def test_queued_run_records_the_chosen_model_and_provider(library):
    from apps.admin_console.database.repositories.session_repository import SessionRepository

    repository = SessionRepository(library.db)
    assert repository.create_queued_session(
        "model-fixture",
        "Open Settings",
        "flash",
        "fixture-phone",
        model={"provider": "openai", "model": "chosen-model"},
    )
    info = json.loads(repository.get_session_by_id("model-fixture")["device_info"])
    assert info["agent_model"] == "chosen-model"
    assert info["agent_provider"] == "openai"
