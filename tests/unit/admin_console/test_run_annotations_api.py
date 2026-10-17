import sqlite3
import uuid

from fastapi import Request
import pytest

from apps.admin_console.core.access_control import AccessIdentity, public_tier, route_tier
from apps.admin_console.core.preview_routes import REAL, registered_routes, unclassified_routes
from apps.admin_console.database.repositories import annotation_repository as notes
from apps.admin_console.routers.run_annotations import initialize_notes
from apps.admin_console.server import app
from apps.admin_console.services.host_registry import host_registry
from tests.unit.admin_console.conftest import make_client


@pytest.fixture
def annotation_run(library, monkeypatch):
    initialize_notes()

    def identity(request: Request):
        role = request.headers["x-test-role"]
        if role == "anonymous":
            return AccessIdentity(None, False, "cloudflare", "no_jwt")
        return AccessIdentity(
            f"{role}@example.com",
            role == "admin",
            "cloudflare",
            issuer="https://access.example.com",
            subject=f"sub-{role}",
        )

    monkeypatch.setitem(app.dependency_overrides, public_tier, identity)
    host_registry.limiter.clear()
    run = library.seed()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE run_meta SET requested_by = 'qa@example.com' WHERE session_id = ?", (run,)
        )
    library.image("annotation-shot")
    step = library.step(run, 12, pre="annotation-shot")
    yield run, step
    host_registry.limiter.clear()


async def _call(role, method, run, suffix="", **kwargs):
    async with make_client(role) as client:
        return await client.request(method, f"/api/runs/{run}/annotations{suffix}", **kwargs)


async def _add(run, step, role="qa", **extra):
    return await _call(
        role,
        "POST",
        run,
        json={"anchor": {"kind": "step", "step_id": step}, "body": "<b>plain text</b>", **extra},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["qa", "qa2", "admin"])
async def test_owner_link_holder_and_admin_can_create_read_and_resolve(annotation_run, role):
    run, step = annotation_run
    response = await _add(run, step, role)
    assert response.status_code == 201, response.text
    annotation = response.json()
    assert annotation["body"] == "<b>plain text</b>"
    assert annotation["anchor"] == {"kind": "step", "step_id": step, "label": "Step 12"}
    assert annotation["resolved"] is False and annotation["edited_at"] is None
    assert annotation["deep_link"] == f"/runs/{run}?annotation={annotation['annotation_id']}"
    listed = await _call(role, "GET", run)
    assert listed.status_code == 200 and listed.json()["annotations"] == [annotation]
    selected = await _call(role, "GET", run, f"/{annotation['annotation_id']}")
    assert selected.json() == annotation
    evidence = await _call(role, "GET", run, f"/{annotation['annotation_id']}/evidence")
    assert evidence.status_code == 200 and evidence.json()["status"] == "exact"
    assert evidence.json()["evidence"]["step_id"] == step
    assert evidence.json()["evidence"]["image_urls"] == ["/api/images/annotation-shot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["qa2", "admin"])
async def test_body_edits_are_author_only_but_resolved_is_a_shared_toggle(annotation_run, role):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    suffix = f"/{annotation['annotation_id']}"
    denied = await _call(role, "PATCH", run, suffix, json={"body": "someone else's edit"})
    assert denied.status_code == 403 and denied.json()["code"] == "comment_not_permitted"
    combined = await _call(role, "PATCH", run, suffix, json={"body": "edit", "resolved": True})
    assert combined.status_code == 403
    selected = (await _call("qa", "GET", run, suffix)).json()
    assert selected["body"] == annotation["body"] and selected["resolved"] is False
    for resolved in (True, False):
        toggled = await _call(role, "PATCH", run, suffix, json={"resolved": resolved})
        assert toggled.status_code == 200 and toggled.json()["resolved"] is resolved
    edited = await _call("qa", "PATCH", run, suffix, json={"body": "author edit"})
    assert edited.status_code == 200 and edited.json()["body"] == "author edit"
    assert edited.json()["edited_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("role,expected", [("qa", 204), ("qa2", 403), ("admin", 204)])
async def test_author_and_admin_can_delete_but_link_holder_cannot(annotation_run, role, expected):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    suffix = f"/{annotation['annotation_id']}"
    deleted = await _call(role, "DELETE", run, suffix)
    assert deleted.status_code == expected
    fetched = await _call("qa", "GET", run, suffix)
    assert fetched.status_code == (404 if expected == 204 else 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
async def test_nonvisible_prefix_and_unknown_run_are_indistinguishable(annotation_run, method):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    suffix = f"/{annotation['annotation_id']}" if method in ("PATCH", "DELETE") else ""
    payload = {"json": {"body": "edit"}} if method == "PATCH" else {}
    if method == "POST":
        payload = {"json": {"anchor": {"kind": "step", "step_id": step}, "body": "note"}}
    prefix = await _call("qa2", method, run[:8], suffix, **payload)
    unknown = await _call("qa2", method, str(uuid.uuid4()), suffix, **payload)
    assert prefix.status_code == unknown.status_code == 404
    assert prefix.json() == unknown.json()
    assert prefix.json()["code"] == "run_not_visible"


@pytest.mark.asyncio
async def test_annotation_id_cannot_cross_runs_or_leak_expiry(annotation_run, library):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    with sqlite3.connect(library.db) as conn:
        notes.tombstone_evidence(conn, run, "step", step)
    suffix = f"/{annotation['annotation_id']}"
    kept = await _call("qa2", "GET", run, suffix)
    assert kept.status_code == 200 and kept.json()["body"] == annotation["body"]
    expired = await _call("qa2", "GET", run, suffix + "/evidence")
    assert expired.status_code == 410 and expired.json()["code"] == "evidence_expired"
    other = library.seed()
    for target in (run[:8], other, str(uuid.uuid4())):
        for tail in (suffix, suffix + "/evidence"):
            miss = await _call("qa2", "GET", target, tail)
            assert miss.status_code == 404 and miss.json()["code"] == "run_not_visible"
    denied = await _call("qa2", "PATCH", other, suffix, json={"resolved": True})
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_deleted_run_deep_link_does_not_reveal_retained_annotation(annotation_run, library):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?", (run,))
    for role in ("qa", "qa2", "admin"):
        response = await _call(role, "GET", run, f"/{annotation['annotation_id']}")
        assert response.status_code == 404 and response.json()["code"] == "run_not_visible"


@pytest.mark.asyncio
async def test_unsigned_caller_cannot_read_or_write_notes(annotation_run):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    for method, suffix, payload in (
        ("GET", "", {}),
        ("POST", "", {"anchor": {"kind": "step", "step_id": step}, "body": "text"}),
        ("PATCH", f"/{annotation['annotation_id']}", {"resolved": True}),
        ("DELETE", f"/{annotation['annotation_id']}", {}),
        ("GET", f"/{annotation['annotation_id']}/evidence", {}),
    ):
        response = await _call(
            "anonymous", method, run, suffix, **({"json": payload} if payload else {})
        )
        assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "x" * 4001, None, 12])
async def test_invalid_bodies_use_the_contract_error(annotation_run, body):
    run, step = annotation_run
    for method, suffix, payload in (
        ("POST", "", {"anchor": {"kind": "step", "step_id": step}, "body": body}),
        ("PATCH", "/ann_missing", {"body": body}),
    ):
        response = await _call("qa", method, run, suffix, json=payload)
        assert response.status_code == 422 and response.json()["code"] == "request_invalid"


@pytest.mark.asyncio
async def test_invalid_anchors_and_client_authorship_do_not_create_notes(annotation_run, library):
    run, step = annotation_run
    other_step = library.step(library.seed(), 1)
    invalid = await _add(run, other_step)
    assert invalid.status_code == 422 and invalid.json()["code"] == "annotation_anchor_invalid"
    forged = await _add(run, step, author_principal_id="another-user")
    assert forged.status_code == 422 and forged.json()["code"] == "request_invalid"
    assert (await _call("qa", "GET", run)).json()["annotations"] == []


@pytest.mark.asyncio
async def test_stable_subject_owns_notes_after_email_change_but_recycled_email_does_not(
    annotation_run, monkeypatch
):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    suffix = f"/{annotation['annotation_id']}"

    def changed_identity(_request: Request):
        return AccessIdentity(
            "new@example.com",
            False,
            "cloudflare",
            issuer="https://access.example.com",
            subject="sub-qa",
        )

    monkeypatch.setitem(app.dependency_overrides, public_tier, changed_identity)
    changed = await _call("qa", "PATCH", run, suffix, json={"body": "same subject"})
    assert changed.status_code == 200

    def recycled_identity(_request: Request):
        return AccessIdentity(
            "qa@example.com",
            False,
            "cloudflare",
            issuer="https://access.example.com",
            subject="new-subject",
        )

    monkeypatch.setitem(app.dependency_overrides, public_tier, recycled_identity)
    recycled = await _call("qa", "PATCH", run, suffix, json={"body": "different subject"})
    assert recycled.status_code == 403


@pytest.mark.asyncio
async def test_successful_full_id_read_records_link_share(annotation_run):
    run, step = annotation_run
    await _add(run, step)
    assert (await _call("qa2", "GET", run)).status_code == 200
    async with make_client("qa2") as client:
        available = await client.get("/api/runs", params={"scope": "available"})
    assert {row["session_id"] for row in available.json()["runs"]} == {run}


@pytest.mark.asyncio
async def test_note_write_limit_is_per_principal_not_run(annotation_run, library):
    run, step = annotation_run
    for _attempt in range(30):
        response = await _add(run, step)
        assert response.status_code == 201, response.text
    other_run = library.seed()
    other_step = library.step(other_run, 1)
    denied = await _add(other_run, other_step)
    assert denied.status_code == 429 and denied.json()["code"] == "rate_limited"
    assert denied.headers["Retry-After"] == "60"
    assert (await _add(run, step, "qa2")).status_code == 201


def test_every_annotation_route_has_access_and_preview_classification():
    expected = {
        ("GET", "/api/runs/{session_id}/annotations"),
        ("POST", "/api/runs/{session_id}/annotations"),
        ("GET", "/api/runs/{session_id}/annotations/{annotation_id}"),
        ("PATCH", "/api/runs/{session_id}/annotations/{annotation_id}"),
        ("DELETE", "/api/runs/{session_id}/annotations/{annotation_id}"),
        ("GET", "/api/runs/{session_id}/annotations/{annotation_id}/evidence"),
    }
    registered = {
        (method, route.path)
        for route in registered_routes(app)
        if "annotations" in route.path
        for method in route.methods
    }
    assert registered == expected
    assert not unclassified_routes(app)
    for method, path in expected:
        assert route_tier(path, {method}) == "public"
        assert f"{method} {path}" in REAL


@pytest.mark.asyncio
@pytest.mark.parametrize("clock,expected", [(True, "exact"), (False, "legacy_uncertain")])
async def test_recording_anchor_resolves_only_its_own_position(
    annotation_run, library, clock, expected
):
    run, _step = annotation_run
    path = library.video(run)
    with sqlite3.connect(library.db) as conn:
        started = conn.execute(
            "SELECT start_time FROM sessions WHERE session_id = ?", (run,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE video_recordings SET start_time = ?, end_time = ? WHERE session_id = ?",
            (started + 5 if clock else None, started + 65 if clock else None, run),
        )
        recording = conn.execute(
            "SELECT video_id FROM video_recordings WHERE session_id = ?", (run,)
        ).fetchone()[0]
    created = await _call(
        "qa2",
        "POST",
        run,
        json={
            "anchor": {"kind": "recording", "recording_id": recording, "offset_ms": 42000},
            "body": "recording moment",
        },
    )
    assert created.status_code == 201, created.text
    annotation = created.json()
    assert annotation["anchor"]["label"] == "Recording · 00:42"
    resolved = await _call("qa2", "GET", run, f"/{annotation['annotation_id']}/evidence")
    assert resolved.status_code == 200 and resolved.json()["status"] == expected
    if clock:
        assert resolved.json()["evidence"]["recording_id"] == recording
        assert resolved.json()["evidence"]["position_ms"] == 37000
        assert (
            resolved.json()["evidence"]["video_url"]
            == f"/videos/{path.relative_to(library.traces)}"
        )
    else:
        assert resolved.json()["evidence"] is None


@pytest.mark.asyncio
async def test_missing_evidence_does_not_borrow_the_next_step(annotation_run, library):
    run, _step = annotation_run
    step = library.step(run, 13)
    created = (await _add(run, step)).json()
    resolved = await _call("qa2", "GET", run, f"/{created['annotation_id']}/evidence")
    assert resolved.status_code == 200 and resolved.json() == {
        "status": "missing",
        "evidence": None,
    }


@pytest.mark.asyncio
async def test_expiry_before_save_keeps_the_note_but_never_returns_media(annotation_run, library):
    run, step = annotation_run
    assert (await _call("qa", "GET", run)).status_code == 200
    with sqlite3.connect(library.db) as conn:
        notes.tombstone_evidence(conn, run, "step", step)
    created = await _add(run, step)
    assert created.status_code == 201
    evidence = await _call("qa", "GET", run, f"/{created.json()['annotation_id']}/evidence")
    assert evidence.status_code == 410
    assert len((await _call("qa", "GET", run)).json()["annotations"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"resolved": None}, {"resolved": "true"}, {"step_id": "different"}]
)
async def test_invalid_edits_leave_the_annotation_unchanged(annotation_run, payload):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    suffix = f"/{annotation['annotation_id']}"
    invalid = await _call("qa", "PATCH", run, suffix, json=payload)
    assert invalid.status_code == 422 and invalid.json()["code"] == "request_invalid"
    assert (await _call("qa", "GET", run, suffix)).json() == annotation


@pytest.mark.asyncio
async def test_unknown_note_read_edit_delete_and_evidence_are_the_same_miss(annotation_run):
    run, _step = annotation_run
    bodies = []
    for method, suffix, kwargs in (
        ("GET", "/ann_missing", {}),
        ("PATCH", "/ann_missing", {"json": {"resolved": True}}),
        ("DELETE", "/ann_missing", {}),
        ("GET", "/ann_missing/evidence", {}),
    ):
        response = await _call("qa2", method, run, suffix, **kwargs)
        assert response.status_code == 404
        bodies.append(response.json())
    assert all(body == bodies[0] for body in bodies)


@pytest.mark.asyncio
async def test_missing_notes_schema_fails_closed(annotation_run, library):
    run, step = annotation_run
    await _add(run, step)
    with sqlite3.connect(library.db) as conn:
        conn.execute("DROP TABLE run_annotations")
    response = await _call("qa2", "GET", run)
    assert response.status_code == 503 and response.json()["code"] == "notes_not_ready"


@pytest.mark.asyncio
async def test_missing_stable_identity_cannot_write(annotation_run, monkeypatch):
    run, step = annotation_run

    def email_only(_request: Request):
        return AccessIdentity("qa@example.com", False, "cloudflare")

    monkeypatch.setitem(app.dependency_overrides, public_tier, email_only)
    response = await _add(run, step)
    assert response.status_code == 401 and response.json()["code"] == "actor_required"
    assert (await _call("qa", "GET", run)).json()["annotations"] == []


@pytest.mark.asyncio
async def test_preview_notes_use_isolated_subjects_without_reserving_email_history(
    annotation_run, library, monkeypatch
):
    run, step = annotation_run

    def preview_identity(_request: Request):
        return AccessIdentity(
            "qa@example.com", False, "preview", issuer="urn:artemis:preview", subject="preview:qa-a"
        )

    monkeypatch.setitem(app.dependency_overrides, public_tier, preview_identity)
    created = await _add(run, step)
    assert created.status_code == 201
    assert created.json()["author_principal_id"].startswith("preview:")
    with sqlite3.connect(library.db) as conn:
        assert conn.execute("SELECT count(*) FROM principal_emails").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_open_mode_can_use_the_annotation_api_without_a_login(annotation_run, monkeypatch):
    run, step = annotation_run

    def open_identity(_request: Request):
        return AccessIdentity(None, True, "open")

    monkeypatch.setitem(app.dependency_overrides, public_tier, open_identity)
    created = await _add(run, step)
    assert created.status_code == 201
    edited = await _call(
        "qa2", "PATCH", run, f"/{created.json()['annotation_id']}", json={"body": "local edit"}
    )
    assert edited.status_code == 200


@pytest.mark.asyncio
async def test_spaces_global_admin_cannot_delete_someone_elses_note(annotation_run, monkeypatch):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()

    def spaces_admin(_request: Request):
        return AccessIdentity(
            "admin@example.com",
            True,
            "cloudflare",
            issuer="https://access.example.com",
            subject="sub-admin",
            spaces=True,
            history_emails=frozenset(),
        )

    monkeypatch.setitem(app.dependency_overrides, public_tier, spaces_admin)
    deleted = await _call("admin", "DELETE", run, f"/{annotation['annotation_id']}")
    assert deleted.status_code == 403 and deleted.json()["code"] == "comment_not_permitted"
    toggled = await _call(
        "admin", "PATCH", run, f"/{annotation['annotation_id']}", json={"resolved": True}
    )
    assert toggled.status_code == 200


@pytest.mark.asyncio
async def test_missing_principal_schema_fails_closed_for_writes(annotation_run, library):
    run, step = annotation_run
    assert (await _call("qa", "GET", run)).status_code == 200
    with sqlite3.connect(library.db) as conn:
        conn.execute("DROP TABLE principal_emails")
    response = await _add(run, step)
    assert response.status_code == 503 and response.json()["code"] == "principal_store_not_ready"


@pytest.mark.asyncio
async def test_link_holder_cannot_read_deleted_runs_notes_even_when_author(annotation_run, library):
    run, step = annotation_run
    annotation = (await _add(run, step, "qa2")).json()
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?", (run,))
    suffix = f"/{annotation['annotation_id']}"
    for method, kwargs in (("GET", {}), ("PATCH", {"json": {"body": "author"}}), ("DELETE", {})):
        response = await _call("qa2", method, run, suffix, **kwargs)
        assert response.status_code == 404 and response.json()["code"] == "run_not_visible"


@pytest.mark.asyncio
async def test_shared_resolve_does_not_change_authorship(annotation_run):
    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    suffix = f"/{annotation['annotation_id']}"
    await _call("qa2", "PATCH", run, suffix, json={"resolved": True})
    blocked = await _call("qa2", "PATCH", run, suffix, json={"body": "claim note"})
    assert blocked.status_code == 403
    edited = await _call("qa", "PATCH", run, suffix, json={"body": "still mine"})
    assert edited.status_code == 200
    assert edited.json()["author_principal_id"] == annotation["author_principal_id"]


@pytest.mark.asyncio
async def test_deep_link_full_id_opens_run_and_selects_note(annotation_run):
    from urllib.parse import parse_qs, urlsplit

    run, step = annotation_run
    annotation = (await _add(run, step)).json()
    link = urlsplit(annotation["deep_link"])
    linked_run = link.path.removeprefix("/runs/")
    linked_note = parse_qs(link.query)["annotation"][0]
    async with make_client("qa2") as client:
        detail = await client.get(f"/api/runs/{linked_run}")
    assert detail.status_code == 200 and detail.json()["session_id"] == run
    selected = await _call("qa2", "GET", linked_run, f"/{linked_note}")
    assert selected.status_code == 200 and selected.json() == annotation


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 4999, 65001])
async def test_out_of_recording_window_is_anchor_invalid(annotation_run, library, offset):
    run, _step = annotation_run
    library.video(run)
    with sqlite3.connect(library.db) as conn:
        started = conn.execute(
            "SELECT start_time FROM sessions WHERE session_id = ?", (run,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE video_recordings SET start_time = ?, end_time = ? WHERE session_id = ?",
            (started + 5, started + 65, run),
        )
        recording = conn.execute(
            "SELECT video_id FROM video_recordings WHERE session_id = ?", (run,)
        ).fetchone()[0]
    response = await _call(
        "qa",
        "POST",
        run,
        json={
            "anchor": {"kind": "recording", "recording_id": recording, "offset_ms": offset},
            "body": "outside recording",
        },
    )
    assert response.status_code == 422 and response.json()["code"] == "annotation_anchor_invalid"
    assert (await _call("qa", "GET", run)).json()["annotations"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed", "path_cleared", "failed"])
async def test_recording_removed_during_resolution_is_missing(
    annotation_run, library, monkeypatch, change
):
    run, _step = annotation_run
    library.video(run)
    with sqlite3.connect(library.db) as conn:
        started = conn.execute(
            "SELECT start_time FROM sessions WHERE session_id = ?", (run,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE video_recordings SET start_time = ?, end_time = ? WHERE session_id = ?",
            (started, started + 60, run),
        )
        recording = conn.execute(
            "SELECT video_id FROM video_recordings WHERE session_id = ?", (run,)
        ).fetchone()[0]
    created = await _call(
        "qa",
        "POST",
        run,
        json={
            "anchor": {"kind": "recording", "recording_id": recording, "offset_ms": 42000},
            "body": "retention race",
        },
    )
    assert created.status_code == 201
    original = notes.recording_evidence
    calls = 0

    def expiring_evidence(conn, session_id, recording_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "removed":
                conn.execute("DELETE FROM video_recordings WHERE video_id = ?", (recording_id,))
            elif change == "path_cleared":
                conn.execute(
                    "UPDATE video_recordings SET local_video_path = NULL WHERE video_id = ?",
                    (recording_id,),
                )
            else:
                conn.execute(
                    "UPDATE video_recordings SET status = 'failed' WHERE video_id = ?",
                    (recording_id,),
                )
            conn.commit()
        return original(conn, session_id, recording_id)

    monkeypatch.setattr(notes, "recording_evidence", expiring_evidence)
    response = await _call("qa", "GET", run, f"/{created.json()['annotation_id']}/evidence")
    assert response.status_code == 200 and response.json() == {
        "status": "missing",
        "evidence": None,
    }
