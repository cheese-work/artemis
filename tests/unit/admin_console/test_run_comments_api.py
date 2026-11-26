from datetime import UTC, datetime
import sqlite3
import uuid

from fastapi import Request
import pytest

from apps.admin_console.core import config
from apps.admin_console.core.access_control import AccessIdentity, public_tier
from apps.admin_console.database.repositories import annotation_repository as notes
from apps.admin_console.routers.run_annotations import initialize_notes
from apps.admin_console.server import app
from apps.admin_console.services.host_registry import host_registry
from tests.unit.admin_console.conftest import make_client


@pytest.fixture
def comment_run(library, monkeypatch):
    initialize_notes()

    def identity(request: Request):
        role = request.headers["x-test-role"]
        if role == "anonymous":
            return AccessIdentity(None, False, "cloudflare", "no_jwt")
        if role == "no_subject":
            return AccessIdentity(
                "qa@example.com", False, "cloudflare", issuer="https://access.example.com"
            )
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
    yield run
    host_registry.limiter.clear()


async def _call(role, method, run, **kwargs):
    async with make_client(role) as client:
        return await client.request(method, f"/api/runs/{run}/comments", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["qa", "qa2", "admin"])
async def test_owner_link_holder_and_admin_can_create_and_list(comment_run, role):
    empty = await _call(role, "GET", comment_run)
    assert empty.status_code == 200 and empty.json() == {"comments": []}

    created = await _call(role, "POST", comment_run, json={"body": "Plain text"})
    assert created.status_code == 201, created.text
    comment = created.json()
    assert comment == {
        "comment_id": comment["comment_id"],
        "session_id": comment_run,
        "author": f"{role}@example.com",
        "author_principal_id": comment["author_principal_id"],
        "body": "Plain text",
        "created_at": comment["created_at"],
        "edited_at": None,
    }
    assert comment["comment_id"].startswith("cmt_")
    assert comment["author_principal_id"] != comment["author"]
    assert datetime.fromisoformat(comment["created_at"]).tzinfo == UTC
    listed = await _call(role, "GET", comment_run)
    assert listed.status_code == 200 and listed.json() == {"comments": [comment]}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_signed_in_nonholder_prefix_and_deleted_run_match_unknown(
    comment_run, library, method
):
    assert (await _call("qa", "POST", comment_run, json={"body": "Private"})).status_code == 201
    payload = {"json": {"body": "Second comment"}} if method == "POST" else {}
    unknown = await _call("outsider", method, str(uuid.uuid4()), **payload)
    prefix = await _call("outsider", method, comment_run[:8], **payload)
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?", (comment_run,))
    deleted = await _call("outsider", method, comment_run, **payload)
    assert unknown.status_code == prefix.status_code == deleted.status_code == 404
    assert unknown.json() == prefix.json() == deleted.json()
    assert unknown.json()["code"] == "run_not_visible"
    assert library.count("run_comments", comment_run) == 1


@pytest.mark.asyncio
async def test_list_orders_by_creation_time_then_id_and_only_reads_this_run(comment_run, library):
    other_run = library.seed()
    with sqlite3.connect(library.db) as conn:
        conn.executemany(
            "INSERT INTO run_comments VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("cmt_a", comment_run, "open:local", "Last", 20, 30),
                ("cmt_c", comment_run, "open:local", "Middle", 10, None),
                ("cmt_b", comment_run, "open:local", "First", 10, None),
                ("cmt_d", other_run, "open:local", "Other run", 5, None),
            ],
        )
    response = await _call("qa2", "GET", comment_run)
    assert response.status_code == 200, response.text
    comments = response.json()["comments"]
    assert [comment["comment_id"] for comment in comments] == ["cmt_b", "cmt_c", "cmt_a"]
    assert all(comment["author"] is None for comment in comments)
    assert comments[-1]["edited_at"] == "1970-01-01T00:00:30+00:00"


def _assert_invalid(response, max_chars):
    assert response.status_code == 422, response.text
    error = response.json()
    assert error["code"] == "request_invalid"
    assert error["detail"] and f"1 to {max_chars} text characters" in error["fix"]
    assert error["docs_url"] == f"{notes.DOCS}#request_invalid"


@pytest.mark.asyncio
async def test_comment_body_default_limit(comment_run):
    at_cap = "x" * 4000
    response = await _call("qa", "POST", comment_run, json={"body": at_cap})
    assert response.status_code == 201 and response.json()["body"] == at_cap
    _assert_invalid(await _call("qa", "POST", comment_run, json={"body": at_cap + "x"}), 4000)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{}, {"body": ""}, {"body": "a\x00b"}, {"body": 12}, {"body": "text", "author": "forged"}],
)
async def test_invalid_comment_body_is_not_stored(comment_run, library, payload):
    _assert_invalid(await _call("qa", "POST", comment_run, json=payload), 4000)
    assert library.count("run_comments", comment_run) == 0


@pytest.mark.asyncio
async def test_configured_cap_applies_to_body_and_validation_hints(comment_run, monkeypatch):
    monkeypatch.setattr(config, "COMMENT_BODY_MAX_CHARS", 3)
    accepted = await _call("qa", "POST", comment_run, json={"body": "abc"})
    assert accepted.status_code == 201 and accepted.json()["body"] == "abc"
    _assert_invalid(await _call("qa", "POST", comment_run, json={"body": "abcd"}), 3)
    for content in (b'{"body":', b'{"body":"\xff"}', rb'{"body":"\ud800"}'):
        malformed = await _call(
            "qa", "POST", comment_run, content=content, headers={"Content-Type": "application/json"}
        )
        _assert_invalid(malformed, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["<b>x</b>", "**x**", "  <script>\n"])
async def test_comment_body_round_trips_verbatim(comment_run, body):
    created = await _call("qa", "POST", comment_run, json={"body": body})
    assert created.status_code == 201 and created.json()["body"] == body
    listed = await _call("qa2", "GET", comment_run)
    assert listed.status_code == 200 and listed.json() == {"comments": [created.json()]}


@pytest.mark.asyncio
async def test_comment_and_annotation_share_the_write_budget(comment_run, library):
    step = library.step(comment_run, 12)
    async with make_client("qa") as client:
        for _attempt in range(15):
            annotation = await client.post(
                f"/api/runs/{comment_run}/annotations",
                json={"anchor": {"kind": "step", "step_id": step}, "body": "Annotation"},
            )
            assert annotation.status_code == 201, annotation.text
            comment = await client.post(
                f"/api/runs/{comment_run}/comments", json={"body": "Comment"}
            )
            assert comment.status_code == 201, comment.text
    denied = await _call("qa", "POST", comment_run, json={"body": "Over the limit"})
    assert denied.status_code == 429 and denied.json()["code"] == "rate_limited"
    assert denied.headers["Retry-After"] == "60"
    assert library.count("run_comments", comment_run) == 15
    assert (
        await _call("qa2", "POST", comment_run, json={"body": "Other caller"})
    ).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["anonymous", "no_subject"])
async def test_post_requires_verified_actor_and_writes_nothing(comment_run, library, role):
    response = await _call(role, "POST", comment_run, json={"body": "Denied"})
    assert response.status_code == 401 and response.json()["code"] == "actor_required"
    assert library.count("run_comments", comment_run) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_missing_comments_table_returns_notes_not_ready(comment_run, library, method):
    with sqlite3.connect(library.db) as conn:
        conn.execute("DROP TABLE run_comments")
    payload = {"json": {"body": "Not ready"}} if method == "POST" else {}
    response = await _call("qa", method, comment_run, **payload)
    assert response.status_code == 503 and response.json()["code"] == "notes_not_ready"
    assert response.headers["Retry-After"] == "5"

    async with make_client("qa") as client:
        annotations = await client.get(f"/api/runs/{comment_run}/annotations")
    assert annotations.status_code == 200 and annotations.json() == {"annotations": []}


# -- edit and delete (CHE-7704) -----------------------------------------------


async def _item(role, method, run, comment_id, **kwargs):
    async with make_client(role) as client:
        return await client.request(method, f"/api/runs/{run}/comments/{comment_id}", **kwargs)


async def _create(role, run, body="Original"):
    created = await _call(role, "POST", run, json={"body": body})
    assert created.status_code == 201, created.text
    return created.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["Edited", "<b>x</b>\n  ", "x" * 4000])
async def test_author_edit_stores_body_verbatim_and_sets_edited_at(comment_run, body):
    comment = await _create("qa", comment_run)
    response = await _item("qa", "PATCH", comment_run, comment["comment_id"], json={"body": body})
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited == {**comment, "body": body, "edited_at": edited["edited_at"]}
    assert datetime.fromisoformat(edited["edited_at"]).tzinfo == UTC
    listed = await _call("qa2", "GET", comment_run)
    assert listed.json() == {"comments": [edited]}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["qa2", "admin"])
async def test_edit_by_non_author_is_refused_even_for_admin(comment_run, role):
    comment = await _create("qa", comment_run)
    response = await _item(
        role, "PATCH", comment_run, comment["comment_id"], json={"body": "Hijacked"}
    )
    assert response.status_code == 403, response.text
    error = response.json()
    assert error["code"] == "comment_not_permitted"
    assert error["docs_url"] == f"{notes.DOCS}#comment_not_permitted"
    assert (await _call("qa", "GET", comment_run)).json() == {"comments": [comment]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"body": ""},
        {"body": "a\x00b"},
        {"body": "x" * 4001},
        {"body": 12},
        {"body": "text", "author": "forged"},
    ],
)
async def test_invalid_edit_is_rejected_and_not_stored(comment_run, payload):
    comment = await _create("qa", comment_run)
    response = await _item("qa", "PATCH", comment_run, comment["comment_id"], json=payload)
    _assert_invalid(response, 4000)
    assert (await _call("qa", "GET", comment_run)).json() == {"comments": [comment]}


@pytest.mark.asyncio
async def test_author_delete_removes_the_comment(comment_run):
    comment = await _create("qa", comment_run)
    keep = await _create("qa", comment_run, "Keep")
    response = await _item("qa", "DELETE", comment_run, comment["comment_id"])
    assert response.status_code == 204 and response.content == b""
    assert (await _call("qa", "GET", comment_run)).json() == {"comments": [keep]}
    assert (await _item("qa", "DELETE", comment_run, comment["comment_id"])).status_code == 404


@pytest.mark.asyncio
async def test_delete_by_non_author_non_admin_is_refused(comment_run):
    comment = await _create("qa", comment_run)
    response = await _item("qa2", "DELETE", comment_run, comment["comment_id"])
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "comment_not_permitted"
    assert (await _call("qa", "GET", comment_run)).json() == {"comments": [comment]}


@pytest.mark.asyncio
async def test_admin_can_delete_another_principals_comment(comment_run):
    comment = await _create("qa", comment_run)
    response = await _item("admin", "DELETE", comment_run, comment["comment_id"])
    assert response.status_code == 204
    assert (await _call("qa", "GET", comment_run)).json() == {"comments": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_deleted_run_unknown_comment_and_other_run_comment_are_indistinguishable(
    comment_run, library, method
):
    other_run = library.seed()
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE run_meta SET requested_by = 'qa@example.com' WHERE session_id = ?",
            (other_run,),
        )
    mine = await _create("qa", comment_run)
    theirs = await _create("qa", other_run, "Other run")
    payload = {"json": {"body": "Edited"}} if method == "PATCH" else {}
    unknown = await _item("qa", method, comment_run, "cmt_unknown", **payload)
    cross_run = await _item("qa", method, comment_run, theirs["comment_id"], **payload)
    prefix = await _item("qa", method, comment_run[:8], mine["comment_id"], **payload)
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?", (comment_run,))
    deleted = await _item("qa", method, comment_run, mine["comment_id"], **payload)
    responses = (deleted, unknown, cross_run, prefix)
    assert [r.status_code for r in responses] == [404] * 4, [r.text for r in responses]
    assert all(r.json() == deleted.json() for r in responses)
    assert deleted.json()["code"] == "run_not_visible"
    assert library.count("run_comments", comment_run) == 1
    assert (await _call("qa", "GET", other_run)).json() == {"comments": [theirs]}


@pytest.mark.asyncio
async def test_edit_and_delete_count_against_the_shared_write_budget(comment_run, monkeypatch):
    monkeypatch.setattr(config, "NOTE_WRITE_LIMIT", 3)
    comment = await _create("qa", comment_run)
    cid = comment["comment_id"]
    edit = await _item("qa", "PATCH", comment_run, cid, json={"body": "Edited"})
    assert edit.status_code == 200
    # A refused non-author call spends nothing.
    assert (await _item("qa2", "DELETE", comment_run, cid)).status_code == 403
    assert (await _item("qa", "PATCH", comment_run, cid, json={"body": "Again"})).status_code == 200
    denied_edit = await _item("qa", "PATCH", comment_run, cid, json={"body": "Over"})
    denied_delete = await _item("qa", "DELETE", comment_run, cid)
    for denied in (denied_edit, denied_delete):
        assert denied.status_code == 429 and denied.json()["code"] == "rate_limited"
        assert denied.headers["Retry-After"] == "60"
    assert (await _call("qa", "GET", comment_run)).json()["comments"][0]["body"] == "Again"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize("role", ["anonymous", "no_subject"])
async def test_edit_and_delete_require_verified_actor(comment_run, role, method):
    comment = await _create("qa", comment_run)
    payload = {"json": {"body": "Denied"}} if method == "PATCH" else {}
    response = await _item(role, method, comment_run, comment["comment_id"], **payload)
    assert response.status_code == 401 and response.json()["code"] == "actor_required"
    assert (await _call("qa", "GET", comment_run)).json() == {"comments": [comment]}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_missing_comments_table_returns_notes_not_ready_on_item_routes(
    comment_run, library, method
):
    with sqlite3.connect(library.db) as conn:
        conn.execute("DROP TABLE run_comments")
    payload = {"json": {"body": "Not ready"}} if method == "PATCH" else {}
    response = await _item("qa", method, comment_run, "cmt_x", **payload)
    assert response.status_code == 503 and response.json()["code"] == "notes_not_ready"
