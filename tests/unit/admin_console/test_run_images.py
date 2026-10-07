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

"""Image chat (CHE-1242, REQ-003): a goal can carry pictures from the message box.

The server validates every picture (type, content, size, count), stores it with
the run it belongs to, serves it back only to the run's owner or an admin, and
hands it to the worker so it reaches the model next to the goal text.

A test token *is* the email: the verifier fake echoes it back as the claim.
"""

import base64
import io
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from PIL import Image
import pytest

from apps.admin_console.core.access_control import AccessConfig
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import sessions as sessions_router
from apps.admin_console.routers import tasks as tasks_router
from apps.admin_console.server import app
from apps.admin_console.services import run_images
from apps.admin_console.services.task_queue_service import TaskQueueService, task_queue_service
from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store

QA1 = "qa1@example.com"
QA2 = "qa2@example.com"
ADMIN = "admin@example.com"
_SHARED_READY = SimpleNamespace(
    summary="Connected", metadata={"active_device": {"serial": "emulator-5554"}}
)


def _image_bytes(fmt: str = "PNG", size=(8, 6)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, fmt)
    return buffer.getvalue()


def _upload(fmt="PNG", media_type="image/png", name="shot.png", size=(8, 6), raw=None) -> dict:
    data = raw if raw is not None else _image_bytes(fmt, size)
    return {"name": name, "media_type": media_type, "data": base64.b64encode(data).decode()}


# -- the validation rules ----------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "media_type", "name", "extension"),
    [
        ("PNG", "image/png", "a.png", ".png"),
        ("JPEG", "image/jpeg", "a.jpg", ".jpg"),
        ("JPEG", "image/jpeg", "a.jpeg", ".jpg"),
        ("WEBP", "image/webp", "a.webp", ".webp"),
    ],
)
def test_the_four_allowed_types_are_accepted(fmt, media_type, name, extension):
    [image] = run_images.validate([_upload(fmt, media_type, name)])

    assert image.media_type == media_type
    assert image.extension == extension
    assert Image.open(io.BytesIO(image.content)).format == fmt


@pytest.mark.parametrize(
    ("upload", "code"),
    [
        (_upload("GIF", "image/gif", "a.gif"), "unsupported_image_type"),
        (
            _upload(raw=b"<svg xmlns='http://www.w3.org/2000/svg'/>", media_type="image/svg+xml"),
            "unsupported_image_type",
        ),
        (_upload(raw=b"plain text, not a picture"), "invalid_image"),
        (_upload(raw=_image_bytes()[:40]), "invalid_image"),
        (_upload("JPEG", "image/png", "a.png"), "invalid_image"),
        ({"name": "a.png", "media_type": "image/png", "data": "!!not base64!!"}, "invalid_image"),
    ],
)
def test_wrong_type_or_content_is_refused_with_a_code(upload, code):
    with pytest.raises(run_images.ImageRejected) as refused:
        run_images.validate([upload])

    assert refused.value.code == code


def test_one_oversize_image_is_refused(monkeypatch):
    monkeypatch.setattr(run_images, "MAX_IMAGE_BYTES", 10)

    with pytest.raises(run_images.ImageRejected) as refused:
        run_images.validate([_upload(size=(64, 64))])

    assert refused.value.code == "image_too_large"
    assert refused.value.status_code == 413


def test_too_many_images_and_too_many_total_bytes_are_refused(monkeypatch):
    with pytest.raises(run_images.ImageRejected) as many:
        run_images.validate([_upload()] * (run_images.MAX_IMAGES + 1))
    monkeypatch.setattr(run_images, "MAX_TOTAL_BYTES", len(_image_bytes()) + 1)
    with pytest.raises(run_images.ImageRejected) as heavy:
        run_images.validate([_upload(), _upload()])

    assert many.value.code == "too_many_images"
    assert heavy.value.code == "images_too_large"


def test_a_picture_with_too_many_pixels_is_refused_before_decoding(monkeypatch):
    monkeypatch.setattr(run_images, "MAX_PIXELS", 100)

    with pytest.raises(run_images.ImageRejected) as refused:
        run_images.validate([_upload(size=(50, 50))])

    assert refused.value.code == "image_too_large"


# -- the submit endpoint -----------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "data_engine.db"
    StorageManager(db, tmp_path)
    monkeypatch.setattr(session_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", tmp_path / "traces")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(sessions_router.media_service, "build_video_index", lambda: {})
    monkeypatch.setattr(sessions_router.media_service, "resolve_video_url", lambda *_a, **_k: None)
    lock = tasks_router.DeviceExecutionLock
    monkeypatch.setattr(lock, "get_queued_tasks", staticmethod(lambda: []))
    monkeypatch.setattr(lock, "get_active_owners", staticmethod(lambda: {}))
    monkeypatch.setattr(lock, "get_active_owner", staticmethod(lambda *_a, **_k: None))
    monkeypatch.setattr(lock, "has_owner_record", staticmethod(lambda *_a, **_k: False))
    monkeypatch.setattr(task_queue_service, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(
        tasks_router.readiness_engine,
        "run_device_submission_probe",
        AsyncMock(return_value=_SHARED_READY),
    )
    state.queue_items.clear()
    state.is_shutting_down = False
    state.shutdown_event.clear()
    yield tmp_path
    state.queue_items.clear()


@pytest.fixture
def cloudflare(env, monkeypatch):
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({ADMIN}),
        ),
    )
    verifier = MagicMock()
    verifier.verify = AsyncMock(side_effect=lambda token, _config: {"email": token})
    monkeypatch.setattr(app.state, "access_verifier", verifier)
    return env


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 51000)),
        base_url="http://localhost",
    )


async def _post(email, path, **kwargs):
    async with _client() as client:
        return await client.post(path, headers={"Cf-Access-Jwt-Assertion": email}, **kwargs)


async def _get(email, path):
    async with _client() as client:
        return await client.get(path, headers={"Cf-Access-Jwt-Assertion": email})


@pytest.fixture
def enqueue(monkeypatch):
    mock = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", mock)
    return mock


@pytest.mark.asyncio
async def test_text_and_image_in_one_message_reach_the_queue_validated(cloudflare, enqueue):
    response = await _post(
        QA1, "/api/run", json={"goal": "what is on this screen?", "images": [_upload()]}
    )

    assert response.status_code == 200
    kwargs = enqueue.await_args.kwargs
    [image] = kwargs["goal_images"]
    assert image.media_type == "image/png"
    assert enqueue.await_args.args[0] == ["what is on this screen?"]
    assert kwargs["requested_by"] == QA1


@pytest.mark.asyncio
async def test_a_message_without_images_is_unchanged(cloudflare, enqueue):
    assert (await _post(QA1, "/api/run", json={"goal": "x"})).status_code == 200

    assert enqueue.await_args.kwargs["goal_images"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (
            {"goal": "x", "images": [_upload("GIF", "image/gif", "a.gif")]},
            415,
            "unsupported_image_type",
        ),
        ({"goal": "x", "images": [_upload(raw=b"not an image")]}, 400, "invalid_image"),
        ({"goal": "x", "images": [_upload()] * 9}, 413, "too_many_images"),
        ({"goals": ["a", "b"], "images": [_upload()]}, 400, "images_need_one_goal"),
    ],
)
async def test_a_bad_image_is_refused_before_anything_is_queued(
    cloudflare, enqueue, body, status, code
):
    response = await _post(QA1, "/api/run", json=body)

    assert response.status_code == status
    assert response.json()["code"] == code
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_request_body_over_the_cap_is_refused_before_it_is_parsed(
    cloudflare, enqueue, monkeypatch
):
    monkeypatch.setattr(run_images, "MAX_REQUEST_BYTES", 50)

    response = await _post(QA1, "/api/run", json={"goal": "x", "images": [_upload()]})

    assert response.status_code == 413
    assert response.json()["code"] == "image_request_too_large"
    enqueue.assert_not_awaited()


# -- storing, serving and delivering ------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_stores_the_images_with_the_run_and_names_them_on_the_item(
    cloudflare, monkeypatch
):
    queue_module = __import__(
        "apps.admin_console.services.task_queue_service", fromlist=["task_queue_service"]
    )
    reserve = patch.object(queue_module.DeviceExecutionLock, "reserve", return_value="t")
    monkeypatch.setattr(queue_module, "session_repo", session_repo)
    sid = str(uuid.uuid4())
    images = run_images.validate([_upload(), _upload("JPEG", "image/jpeg", "b.jpg")])

    with (
        reserve,
        patch(
            "artemis.runtime.device_pool.device_pool.select_device_async",
            new=AsyncMock(return_value="emulator-5554"),
        ),
    ):
        result = await task_queue_service.enqueue_tasks(
            ["g"], session_id=sid, requested_by=QA1, goal_images=images
        )

    folder = cloudflare / "traces" / sid / "goal_images"
    assert sorted(p.name for p in folder.iterdir()) == ["0.png", "1.jpg"]
    assert (folder / "0.png").read_bytes() == images[0].content
    item = result["tasks"][0]
    assert [i["media_type"] for i in item["goal_images"]] == ["image/png", "image/jpeg"]
    assert all("path" not in i for i in item["goal_images"]), "no server paths in the response"


def _stored(root, sid, count=1):
    folder = root / "traces" / sid / "goal_images"
    folder.mkdir(parents=True)
    for i in range(count):
        (folder / f"{i}.png").write_bytes(_image_bytes())
    return folder


def _owned_run(db_root, owner, sid=None):
    sid = sid or str(uuid.uuid4())
    assert session_repo.create_queued_session(sid, "g", "flash", None, 1.0, None, owner)
    return sid


@pytest.mark.asyncio
async def test_only_the_owner_or_an_admin_can_fetch_an_image(cloudflare):
    sid = _owned_run(cloudflare, QA1)
    _stored(cloudflare, sid)

    mine = await _get(QA1, f"/api/sessions/{sid}/goal-images/0")
    admin = await _get(ADMIN, f"/api/sessions/{sid}/goal-images/0")
    other = await _get(QA2, f"/api/sessions/{sid}/goal-images/0")

    assert mine.status_code == admin.status_code == 200
    assert mine.headers["content-type"] == "image/png"
    assert mine.headers["x-content-type-options"] == "nosniff"
    assert mine.content == _image_bytes()
    assert other.status_code == 403
    assert other.content != _image_bytes()


@pytest.mark.asyncio
async def test_a_missing_or_malformed_image_reference_is_a_404(cloudflare):
    sid = _owned_run(cloudflare, QA1)
    _stored(cloudflare, sid)

    assert (await _get(QA1, f"/api/sessions/{sid}/goal-images/7")).status_code == 404
    assert (await _get(QA1, f"/api/sessions/{sid}/goal-images/x")).status_code == 404
    assert (await _get(QA1, f"/api/sessions/{sid}/goal-images/..%2F0")).status_code == 404


@pytest.mark.asyncio
async def test_the_session_list_names_each_runs_images_for_its_owner_only(cloudflare):
    mine, theirs = _owned_run(cloudflare, QA1), _owned_run(cloudflare, QA2)
    _stored(cloudflare, mine, 2)
    _stored(cloudflare, theirs, 1)

    rows = {r["session_id"]: r for r in (await _get(QA1, "/api/sessions")).json()}

    assert set(rows) == {mine}
    assert rows[mine]["goal_images"] == [
        {"index": 0, "media_type": "image/png", "url": f"/api/sessions/{mine}/goal-images/0"},
        {"index": 1, "media_type": "image/png", "url": f"/api/sessions/{mine}/goal-images/1"},
    ]


def _manifest(count: int) -> list[dict]:
    return [{"index": i, "media_type": "image/png", "url": f"/u/{i}"} for i in range(count)]


def _launch(sid: str, manifest: list[dict]):
    target = MagicMock()
    target.endpoint.apply_to_environment = lambda env: None
    target.lock_scope = "scope"
    item = {"session_id": sid, "goal": "g", "goal_images": manifest}
    return TaskQueueService._build_worker_invocation(item, "k", sid, "g", "flash", target, {})


def test_the_worker_is_told_where_its_images_are_and_nothing_else_changes(cloudflare):
    sid = _owned_run(cloudflare, QA1)
    folder = _stored(cloudflare, sid, 2)
    target = MagicMock()
    target.endpoint.apply_to_environment = lambda env: None
    target.lock_scope = "scope"
    with_images = {"session_id": sid, "goal": "g", "goal_images": _manifest(2)}

    _, env = TaskQueueService._build_worker_invocation(
        with_images, "k", sid, "g", "flash", target, {}
    )
    _, plain = TaskQueueService._build_worker_invocation(
        {"session_id": sid, "goal": "g"}, "k", sid, "g", "flash", target, {}
    )

    assert json.loads(env["ARTEMIS_GOAL_IMAGES"]) == [str(folder / "0.png"), str(folder / "1.png")]
    assert "ARTEMIS_GOAL_IMAGES" not in plain


@pytest.mark.asyncio
async def test_deleting_a_run_deletes_its_images(cloudflare):
    from apps.admin_console.services.run_purge import purge_run

    sid = _owned_run(cloudflare, QA1)
    folder = _stored(cloudflare, sid, 2)

    purge_run(cloudflare / "data_engine.db", cloudflare / "traces", sid)

    assert not folder.exists()
    assert (await _get(QA1, f"/api/sessions/{sid}/goal-images/0")).status_code in (403, 404)


# -- a client-chosen session id never reaches the filesystem unchecked ---------------

_UNSAFE_IDS = ["../../evil", "a/b", "..", "x/../../y", "/abs", "a" * 200]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", _UNSAFE_IDS)
@pytest.mark.parametrize("with_images", [True, False])
async def test_an_unsafe_session_id_is_refused_before_anything_is_queued_or_written(
    cloudflare, enqueue, session_id, with_images
):
    body = {"goal": "x", "session_id": session_id}
    if with_images:
        body["images"] = [_upload()]

    response = await _post(QA1, "/api/run", json=body)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_session_id"
    enqueue.assert_not_awaited()
    assert not list(cloudflare.rglob("goal_images"))
    assert not (cloudflare.parent / "evil").exists()


@pytest.mark.asyncio
async def test_enqueue_refuses_an_unsafe_session_id_before_any_side_effect(cloudflare):
    images = run_images.validate([_upload()])

    with pytest.raises(ValueError, match="session id"):
        await task_queue_service.enqueue_tasks(
            ["g"], session_id="../../evil", requested_by=QA1, goal_images=images
        )

    assert not list(cloudflare.rglob("goal_images"))
    assert not (cloudflare.parent / "evil").exists()
    assert not (cloudflare / "traces").exists() or not list((cloudflare / "traces").iterdir())


def test_the_image_store_itself_refuses_an_unsafe_session_id(cloudflare):
    images = run_images.validate([_upload()])

    with pytest.raises(ValueError):
        run_images.store("../../evil", images)

    assert run_images.describe("../../evil") == []
    assert run_images.stored_paths("../../evil") == []


# -- an accepted picture is never silently lost before the worker starts --------------


def test_launch_fails_when_one_recorded_picture_is_gone(cloudflare):
    sid = _owned_run(cloudflare, QA1)
    folder = _stored(cloudflare, sid, 2)
    (folder / "1.png").unlink()

    with pytest.raises(run_images.GoalImagesMissing) as lost:
        _launch(sid, _manifest(2))

    assert "1" in str(lost.value)


def test_launch_fails_when_every_recorded_picture_is_gone(cloudflare):
    sid = _owned_run(cloudflare, QA1)
    folder = _stored(cloudflare, sid, 2)
    for entry in folder.iterdir():
        entry.unlink()

    with pytest.raises(run_images.GoalImagesMissing):
        _launch(sid, _manifest(2))


def test_launch_fails_when_the_whole_image_folder_is_gone(cloudflare):
    sid = _owned_run(cloudflare, QA1)
    import shutil

    shutil.rmtree(_stored(cloudflare, sid, 1))

    with pytest.raises(run_images.GoalImagesMissing):
        _launch(sid, _manifest(1))


def test_launch_hands_the_worker_exactly_the_recorded_pictures_in_order(cloudflare):
    sid = _owned_run(cloudflare, QA1)
    folder = _stored(cloudflare, sid, 3)

    _, env = _launch(sid, _manifest(2))

    assert json.loads(env["ARTEMIS_GOAL_IMAGES"]) == [str(folder / "0.png"), str(folder / "1.png")]


def test_a_recorded_picture_that_became_a_link_is_not_trusted(cloudflare, tmp_path):
    sid = _owned_run(cloudflare, QA1)
    folder = _stored(cloudflare, sid, 1)
    (folder / "0.png").unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_image_bytes())
    (folder / "0.png").symlink_to(outside)

    with pytest.raises(run_images.GoalImagesMissing):
        _launch(sid, _manifest(1))


@pytest.mark.asyncio
async def test_a_run_with_a_lost_picture_ends_failed_and_never_starts_a_worker(
    cloudflare, monkeypatch
):
    sid = _owned_run(cloudflare, QA1)
    folder = _stored(cloudflare, sid, 1)
    (folder / "0.png").unlink()
    spawned = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawned)
    monkeypatch.setattr(TaskQueueService, "_begin_task_run", MagicMock(), raising=True)
    monkeypatch.setattr(TaskQueueService, "_task_target", MagicMock(return_value=MagicMock()))
    persisted = AsyncMock()
    monkeypatch.setattr(TaskQueueService, "_persist_terminal_session_status", persisted)
    monkeypatch.setattr(TaskQueueService, "_deliver_outcome", MagicMock())
    monkeypatch.setattr(TaskQueueService, "_release_run_slot", MagicMock())
    store = MagicMock()
    store.snapshot_for_spawn = AsyncMock(
        return_value=MagicMock(environment={}, config_path=MagicMock())
    )
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: store)
    item = {"session_id": sid, "goal": "g", "goal_images": _manifest(1)}

    await TaskQueueService._execute_task_item(item)

    spawned.assert_not_called()
    persisted.assert_awaited_once()
    assert persisted.await_args.kwargs["returncode"] == 1  # failed, not a text-only run
