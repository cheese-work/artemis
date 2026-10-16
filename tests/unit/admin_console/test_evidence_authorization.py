"""Evidence routes follow the run visibility rule (CHE-1372, layer 3 of CHE-1362).

A signed-in caller who knows the full run id may read the run's evidence; media
follows the run. An unauthenticated caller is refused, and a run that is unknown,
removed, or has no owning run answers ``run_not_visible`` 404 without telling the
three apart. Every test is a direct HTTP call on the URL a copied link would use.
"""

from dataclasses import dataclass
import io

from PIL import Image
import pytest

from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.database.repositories.step_repository import step_repo
from apps.admin_console.routers import media as media_router
from apps.admin_console.routers import replay as replay_router
from apps.admin_console.services import media_service as media_service_module
from tests.unit.admin_console.conftest import RunLibrary

UNKNOWN = "00000000-0000-4000-8000-00000000dead"
OWNER = "qa@example.com"
TRACE_DIGEST = "ab" * 32


@dataclass
class Evidence:
    sid: str
    step_id: str
    trace_id: str
    image: str
    video: str
    video_path: str
    goal_image: str


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def evidence(library: RunLibrary, monkeypatch) -> Evidence:
    monkeypatch.setattr(media_router, "_allowed_media_roots", lambda: [library.traces.resolve()])
    monkeypatch.setattr(media_router, "IMAGES_DIR", library.images)
    monkeypatch.setattr(media_service_module, "TRACES_PATH", library.traces)
    monkeypatch.setattr(session_repo, "db_path", library.db)
    monkeypatch.setattr(step_repo, "db_path", library.db)
    # Replay data lives outside the run database; these tests only check who may ask.
    monkeypatch.setattr(replay_router.replay_manager, "get_replay_steps", lambda _sid: [])
    monkeypatch.setattr(replay_router.replay_manager, "get_step_replay_traces", lambda *_args: [])
    sid = library.seed("owned goal")
    run_catalog_repo.set_meta(sid, requested_by=OWNER)
    goal_image = library.traces / sid / "goal_images" / "0.png"
    goal_image.parent.mkdir(parents=True, exist_ok=True)
    goal_image.write_bytes(_png())
    (goal_image.parent / f"trace_{TRACE_DIGEST}.jpg").write_bytes(_png())  # trace-image cache
    library.image("shot1")
    video = library.video(sid)
    return Evidence(
        sid=sid,
        step_id=library.step(sid, 1, pre="shot1"),
        trace_id=library.trace(sid, "{}"),
        image="shot1",
        video=f"{video.parent.name}/{video.name}",
        video_path=str(video),
        goal_image=str(goal_image),
    )


def _run_keyed(sid: str) -> list[str]:
    return [
        f"/api/sessions/{sid}",
        f"/api/sessions/{sid}/video",
        f"/api/sessions/{sid}/plan",
        f"/api/sessions/{sid}/notes",
        f"/api/sessions/{sid}/checks",
        f"/api/sessions/{sid}/events",
        f"/api/sessions/{sid}/usage",
        f"/api/sessions/{sid}/tree",
        f"/api/sessions/{sid}/steps",
        f"/api/sessions/{sid}/background_tasks",
        f"/api/sessions/{sid}/startup_progress",
        f"/api/sessions/{sid}/replay_steps",
        f"/api/sessions/{sid}/steps/1/replay_traces",
        f"/api/sessions/{sid}/goal-images/0",
        f"/api/stream/{sid}",
    ]


def _path_keyed_media(e: Evidence) -> list[str]:
    """Evidence named by an image name, file path, trace id or step id: the URL has no run id."""
    return [
        f"/api/steps/{e.step_id}/traces",
        f"/api/traces/{e.trace_id}",
        f"/api/traces/{e.trace_id}/download",
        f"/images/{e.image}",
        f"/api/images/{e.image}",
        f"/videos/{e.video}",
        f"/local_file?path={e.video_path}",
        f"/local_file?path={e.goal_image}",
        f"/images/inline_{e.sid}_{TRACE_DIGEST}",
    ]


def _owned_keyed(e: Evidence) -> list[str]:
    return _path_keyed_media(e)


# Routes a full-id holder can read; the stream never ends, so it is tested separately.
def _readable(e: Evidence) -> list[str]:
    return [u for u in _run_keyed(e.sid) + _owned_keyed(e) if u != f"/api/stream/{e.sid}"]


def _all(e: Evidence) -> list[str]:
    return [
        *_run_keyed(e.sid),
        *_owned_keyed(e),
        f"/api/runs/{e.sid}",
        f"/api/runs/{e.sid}/bundle.zip",
    ]


def _is_not_visible(response) -> bool:
    return response.status_code == 404 and response.json().get("code") == "run_not_visible"


@pytest.mark.asyncio
async def test_anonymous_is_refused_on_every_evidence_route(evidence, anonymous):
    for url in _all(evidence):
        response = await anonymous.get(url)
        assert response.status_code == 401, url
        assert response.json()["code"] == "not_signed_in", url


@pytest.mark.asyncio
async def test_owner_and_admin_keep_access_to_every_readable_route(evidence, qa, admin):
    for client in (qa, admin):
        for url in _readable(evidence) + [f"/api/runs/{evidence.sid}/bundle.zip"]:
            response = await client.get(url)
            assert response.status_code == 200, (url, response.text)


@pytest.mark.asyncio
async def test_a_full_id_holder_reads_run_keyed_routes_at_once(evidence, qa2):
    run_keyed = [u for u in _run_keyed(evidence.sid) if u != f"/api/stream/{evidence.sid}"]
    for url in run_keyed + [f"/api/runs/{evidence.sid}/bundle.zip"]:
        response = await qa2.get(url)
        assert response.status_code == 200, (url, response.text)


@pytest.mark.asyncio
async def test_guessed_media_paths_are_refused_until_the_caller_opens_the_run_by_full_id(
    evidence, qa2
):
    for url in _path_keyed_media(evidence):
        assert _is_not_visible(await qa2.get(url)), url
    assert (await qa2.get(f"/api/sessions/{evidence.sid}")).status_code == 200  # the link
    for url in _path_keyed_media(evidence):
        response = await qa2.get(url)
        assert response.status_code == 200, (url, response.text)


@pytest.mark.asyncio
async def test_opening_another_run_does_not_unlock_this_runs_media(evidence, library, qa2):
    other = library.seed("other goal")
    run_catalog_repo.set_meta(other, requested_by=OWNER)
    assert (await qa2.get(f"/api/sessions/{other}")).status_code == 200
    for url in _path_keyed_media(evidence):
        assert _is_not_visible(await qa2.get(url)), url


@pytest.mark.asyncio
async def test_trace_image_caches_follow_the_evidence_rule_not_an_owner_only_exception(
    evidence, qa, qa2
):
    url = f"/images/inline_{evidence.sid}_{TRACE_DIGEST}"
    assert (await qa.get(url)).status_code == 200  # the owner
    assert _is_not_visible(await qa2.get(url))  # a guess: no link yet
    assert (await qa2.get(f"/api/sessions/{evidence.sid}")).status_code == 200
    linked = await qa2.get(url)
    assert linked.status_code == 200 and linked.content == _png()
    # Hidden and unknown runs answer the same 404 as any other missing evidence.
    missing = f"/images/inline_{UNKNOWN}_{TRACE_DIGEST}"
    run_catalog_repo.tombstone(evidence.sid, "admin_delete")
    for hidden_url in (url, missing):
        assert _is_not_visible(await qa2.get(hidden_url)), hidden_url


@pytest.mark.asyncio
async def test_removed_run_looks_like_an_unknown_run_on_every_run_keyed_route(
    evidence, library, qa, qa2
):
    run_catalog_repo.tombstone(evidence.sid, "admin_delete")
    for client in (qa, qa2):
        for template in _run_keyed("{}"):
            hidden = await client.get(template.format(evidence.sid))
            unknown = await client.get(template.format(UNKNOWN))
            assert _is_not_visible(hidden), template
            assert hidden.json() == unknown.json(), template


@pytest.mark.asyncio
async def test_media_and_traces_of_a_removed_run_are_not_visible(evidence, qa2):
    run_catalog_repo.tombstone(evidence.sid, "admin_delete")
    for url in _owned_keyed(evidence):
        assert _is_not_visible(await qa2.get(url)), url


@pytest.mark.asyncio
async def test_files_and_traces_that_belong_to_no_run_are_not_visible(evidence, library, qa, admin):
    stray = library.traces / "stray" / "clip.mp4"
    stray.parent.mkdir()
    stray.write_bytes(b"CLIP")
    library.image("orphan")
    orphan_trace = library.trace(UNKNOWN, "{}")
    urls = [
        "/videos/stray/clip.mp4",
        f"/local_file?path={stray}",
        "/images/orphan",
        f"/api/traces/{orphan_trace}",
        f"/api/traces/{orphan_trace}/download",
    ]
    for url in urls:
        assert _is_not_visible(await qa.get(url)), url
        assert (await admin.get(url)).status_code == 200, url


@pytest.mark.asyncio
async def test_named_stream_of_a_hidden_run_is_refused_before_streaming(evidence, qa2):
    assert _is_not_visible(await qa2.get(f"/api/stream/{UNKNOWN}"))
    run_catalog_repo.tombstone(evidence.sid, "admin_delete")
    assert _is_not_visible(await qa2.get(f"/api/stream/{evidence.sid}"))


@pytest.mark.asyncio
async def test_goal_images_are_readable_by_any_signed_in_holder_of_the_full_id(evidence, qa2):
    by_route = await qa2.get(f"/api/sessions/{evidence.sid}/goal-images/0")
    by_path = await qa2.get(f"/local_file?path={evidence.goal_image}")
    assert by_route.status_code == by_path.status_code == 200
    assert by_route.content == by_path.content == _png()


@pytest.mark.asyncio
async def test_hidden_ownerless_and_unknown_evidence_share_one_answer(
    evidence, library, qa2, admin
):
    # Ownerless: a file and a trace that no run owns.
    stray = library.traces / "stray" / "clip.mp4"
    stray.parent.mkdir()
    stray.write_bytes(b"CLIP")
    library.image("orphan")
    orphan_trace = library.trace(UNKNOWN, "{}")
    ownerless = [
        "/videos/stray/clip.mp4",
        f"/local_file?path={stray}",
        "/images/orphan",
        f"/api/traces/{orphan_trace}",
        f"/api/traces/{orphan_trace}/download",
    ]
    # Unknown: identifiers and paths that name nothing.
    absent = library.traces / "nowhere" / "none.mp4"
    unknown = [
        "/images/nope",
        "/api/images/nope",
        "/videos/nowhere/none.mp4",
        f"/local_file?path={absent}",
        f"/api/traces/{UNKNOWN}",
        f"/api/traces/{UNKNOWN}/download",
        f"/api/steps/{UNKNOWN}/traces",
    ]
    # Hidden: everything the removed run owned, goal images included.
    run_catalog_repo.tombstone(evidence.sid, "admin_delete")
    hidden = [*_owned_keyed(evidence), f"/api/sessions/{evidence.sid}/goal-images/0"]
    bodies = {}
    for url in ownerless + unknown + hidden:
        response = await qa2.get(url)
        assert _is_not_visible(response), (url, response.status_code, response.text)
        bodies[url] = response.json()
    assert len({str(body) for body in bodies.values()}) == 1, bodies


def _shared(email: str) -> frozenset[str]:
    return run_catalog_repo.shared_run_ids(email)


@pytest.mark.asyncio
async def test_a_successful_full_id_evidence_read_adds_the_run_to_available(evidence, qa2):
    assert evidence.sid not in _shared("qa2@example.com")
    assert (await qa2.get(f"/api/sessions/{evidence.sid}/steps")).status_code == 200
    assert evidence.sid in _shared("qa2@example.com")


@pytest.mark.asyncio
async def test_failed_prefix_hidden_and_anonymous_reads_never_add_a_share(
    evidence, library, qa2, anonymous
):
    sid = evidence.sid
    assert (await qa2.get(f"/api/sessions/{sid}/goal-images/9")).status_code == 404  # failed read
    assert (await qa2.get(f"/api/sessions/{sid[:8]}")).status_code == 404  # prefix
    assert (await qa2.get(f"/api/sessions/{sid[:8]}/steps")).status_code == 404
    assert (await anonymous.get(f"/api/sessions/{sid}")).status_code == 401
    assert _shared("qa2@example.com") == frozenset()
    run_catalog_repo.tombstone(sid, "admin_delete")
    assert _is_not_visible(await qa2.get(f"/api/sessions/{sid}"))
    assert _shared("qa2@example.com") == frozenset()


@pytest.mark.asyncio
async def test_opening_the_named_stream_by_full_id_adds_the_run_to_available(evidence):
    # The stream never ends over HTTP, so call the handler: it returns the open response.
    from apps.admin_console.core.ownership import OwnerScope
    from apps.admin_console.routers import tasks

    scope = OwnerScope(True, "qa2@example.com")
    response = await tasks.stream_events(session_id=evidence.sid, scope=scope)
    assert response.media_type == "text/event-stream"
    assert evidence.sid in _shared("qa2@example.com")
