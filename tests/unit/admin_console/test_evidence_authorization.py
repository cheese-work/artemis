"""Evidence routes follow the run visibility rule (CHE-1372, layer 3 of CHE-1362).

A signed-in caller who knows the full run id may read the run's evidence; media
follows the run. An unauthenticated caller is refused, and a run that is unknown,
removed, or has no owning run answers ``run_not_visible`` 404 without telling the
three apart. Every test is a direct HTTP call on the URL a copied link would use.
"""

from dataclasses import dataclass

import pytest

from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.database.repositories.step_repository import step_repo
from apps.admin_console.routers import media as media_router
from apps.admin_console.services import media_service as media_service_module
from tests.unit.admin_console.conftest import RunLibrary

UNKNOWN = "00000000-0000-4000-8000-00000000dead"
OWNER = "qa@example.com"


@dataclass
class Evidence:
    sid: str
    step_id: str
    trace_id: str
    image: str
    video: str
    video_path: str


@pytest.fixture
def evidence(library: RunLibrary, monkeypatch) -> Evidence:
    monkeypatch.setattr(media_router, "_allowed_media_roots", lambda: [library.traces.resolve()])
    monkeypatch.setattr(media_router, "IMAGES_DIR", library.images)
    monkeypatch.setattr(media_service_module, "TRACES_PATH", library.traces)
    monkeypatch.setattr(session_repo, "db_path", library.db)
    monkeypatch.setattr(step_repo, "db_path", library.db)
    sid = library.seed("owned goal")
    run_catalog_repo.set_meta(sid, requested_by=OWNER)
    library.image("shot1")
    video = library.video(sid)
    return Evidence(
        sid=sid,
        step_id=library.step(sid, 1, pre="shot1"),
        trace_id=library.trace(sid, "{}"),
        image="shot1",
        video=f"{video.parent.name}/{video.name}",
        video_path=str(video),
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


def _owned_keyed(e: Evidence) -> list[str]:
    return [
        f"/api/steps/{e.step_id}/traces",
        f"/api/traces/{e.trace_id}",
        f"/api/traces/{e.trace_id}/download",
        f"/images/{e.image}",
        f"/api/images/{e.image}",
        f"/videos/{e.video}",
        f"/local_file?path={e.video_path}",
    ]


# Routes a full-id holder can read; the stream never ends, goal images stay owner-only.
def _readable(e: Evidence) -> list[str]:
    skipped = (f"/api/stream/{e.sid}", f"/api/sessions/{e.sid}/goal-images/0")
    return [u for u in _run_keyed(e.sid) + _owned_keyed(e) if u not in skipped]


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
async def test_owner_admin_and_other_signed_in_holder_of_the_full_id_keep_access(
    evidence, qa, qa2, admin
):
    for client in (qa, qa2, admin):
        for url in _readable(evidence) + [f"/api/runs/{evidence.sid}/bundle.zip"]:
            response = await client.get(url)
            assert response.status_code == 200, (url, response.text)


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
async def test_goal_images_stay_owner_or_admin_only_and_hide_unknown_runs(evidence, qa, qa2):
    assert _is_not_visible(await qa.get(f"/api/sessions/{UNKNOWN}/goal-images/0"))
    assert (await qa2.get(f"/api/sessions/{evidence.sid}/goal-images/0")).status_code == 403


@pytest.mark.asyncio
async def test_named_stream_of_a_hidden_run_is_refused_before_streaming(evidence, qa2):
    assert _is_not_visible(await qa2.get(f"/api/stream/{UNKNOWN}"))
    run_catalog_repo.tombstone(evidence.sid, "admin_delete")
    assert _is_not_visible(await qa2.get(f"/api/stream/{evidence.sid}"))
