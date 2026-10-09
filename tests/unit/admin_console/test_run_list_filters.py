"""Run-list filters, visibility and counts at the HTTP seam (CHE-1470)."""

import json
import sqlite3

import pytest

from apps.admin_console.core.access_control import AccessIdentity, public_tier
from apps.admin_console.server import app
from artemis.data_engine import run_catalog, run_snapshot
from artemis.data_engine.storage import StorageManager


def _run(library, *, owner="qa@example.com", start=10, status="completed", **metadata):
    session_id = library.seed(status=status)
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE sessions SET start_time = ? WHERE session_id = ?", (start, session_id))
        values = {"requested_by": owner, **metadata}
        assignments = ", ".join(f"{name} = ?" for name in values)
        conn.execute(
            f"UPDATE run_meta SET {assignments} WHERE session_id = ?",
            (*values.values(), session_id),
        )
    return session_id


async def _page(client, **params):
    response = await client.get("/api/runs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(page):
    return [run["session_id"] for run in page["runs"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parameter", "column", "value"),
    [
        ("app_build", "app_build", "build-a"),
        ("model", "agent_model", "model-a"),
        ("suite", "suite_version", "suite-a"),
        ("review", "review", "pending"),
    ],
)
async def test_value_filters_are_independent_and_do_not_guess_unknowns(
    library, qa, parameter, column, value
):
    matching = _run(library, **{column: value})
    _run(library, **{column: "other"})
    _run(library)

    page = await _page(qa, **{parameter: value})

    assert _ids(page) == [matching]
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_execution_and_review_are_separate_without_a_verdict(library, qa):
    pending = _run(library, status="success", review="pending", start=30)
    accepted = _run(library, status="completed", review="accepted", start=20)
    running = _run(library, status="running", review="pending", start=10)

    assert _ids(await _page(qa, status="completed")) == [pending, accepted]
    assert _ids(await _page(qa, review="pending")) == [pending, running]
    page = await _page(qa, status="completed", review="pending")
    assert _ids(page) == [pending]
    assert page["runs"][0]["status"] == "completed"
    assert page["runs"][0]["review"] == "pending"
    assert "verdict" not in page["runs"][0]
    assert _ids(await _page(qa, status="passed")) == []


@pytest.mark.asyncio
async def test_combined_filters_intersect_and_models_are_repeatable(library, qa):
    common = dict(
        app_build="build-a",
        suite_version="suite-a",
        review="pending",
        device_ref=json.dumps({"host_id": "host-a", "serial": "usb-a"}),
    )
    first = _run(library, start=20, agent_model="model-a", **common)
    second = _run(library, start=15, agent_model="model-b", **common)
    _run(library, start=25, agent_model="model-c", **common)
    _run(library, start=30, agent_model="model-a", **common)
    _run(library, start=20, agent_model="model-a", **{**common, "app_build": "build-b"})
    _run(library, start=20, agent_model="model-a", **{**common, "suite_version": "suite-b"})
    _run(library, start=20, agent_model="model-a", **{**common, "review": "accepted"})
    _run(library, start=20, agent_model="model-a", **{**common, "device_ref": None})

    page = await _page(
        qa,
        app_build="build-a",
        suite="suite-a",
        review="pending",
        model=["model-a", "model-b"],
        device="usb-a",
        status="completed",
        requester="qa@example.com",
        since=15,
        until=30,
    )
    assert _ids(page) == [first, second]
    assert page["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["mine", "available"])
async def test_visibility_is_applied_before_filtered_counts_and_each_page(library, qa, scope):
    mine = [_run(library, start=start, app_build="build-a") for start in (10, 20)]
    shared = _run(library, owner="qa2@example.com", start=30, app_build="build-a")
    hidden = _run(library, owner="qa2@example.com", start=40, app_build="build-a")
    unowned = _run(library, owner=None, start=50, app_build="build-a")
    deleted = _run(library, start=60, app_build="build-a", deleted_at=1)
    _run(library, start=70, app_build="build-b")
    assert (await qa.get(f"/api/runs/{shared}")).status_code == 200
    expected = [shared, *reversed(mine)] if scope == "available" else list(reversed(mine))
    seen = []
    cursor = None
    for _ in expected:
        page = await _page(
            qa, scope=scope, app_build="build-a", limit=1, **({"cursor": cursor} if cursor else {})
        )
        assert page["total"] == len(expected)
        seen.extend(_ids(page))
        cursor = page["next_cursor"]
    assert seen == expected
    assert cursor is None
    assert not {hidden, unowned, deleted}.intersection(seen)
    assert (await _page(qa, scope=scope, requester="qa2@example.com", app_build="build-a"))[
        "total"
    ] == (1 if scope == "available" else 0)


@pytest.mark.asyncio
async def test_counts_include_unknown_start_times_but_dates_do_not(library, qa):
    dated = _run(library, start=20, app_build="build-a")
    undated = _run(library, start=None, app_build="build-a")
    first = await _page(qa, app_build="build-a", limit=1)
    assert _ids(first) == [dated] and first["total"] == 2
    last = await _page(qa, app_build="build-a", limit=1, cursor=first["next_cursor"])
    assert _ids(last) == [undated] and last["total"] == 2 and last["next_cursor"] is None
    assert (await _page(qa, app_build="build-a", since=20))["total"] == 1
    assert (await _page(qa, app_build="absent"))["total"] == 0


@pytest.mark.asyncio
async def test_new_filters_are_parameterized(library, qa):
    _run(library, app_build="build-a", agent_model="model-a", suite_version="suite-a")
    for parameter in ("app_build", "model", "suite", "review"):
        page = await _page(qa, **{parameter: "' OR 1=1 --"})
        assert page["runs"] == [] and page["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["nan", "inf", "-inf", "1e9999"])
async def test_non_finite_dates_are_rejected(library, qa, bound):
    for parameter in ("since", "until"):
        response = await qa.get("/api/runs", params={parameter: bound})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_time"


@pytest.mark.asyncio
async def test_scopes_keep_admin_and_anonymous_rules_for_counts(library, qa, admin, anonymous):
    _run(library, app_build="build-a")
    _run(library, owner=None, app_build="build-a")
    _run(library, owner="qa2@example.com", app_build="build-a")
    assert (await qa.get("/api/runs", params={"scope": "all"})).status_code == 403
    assert (await _page(admin, scope="all", app_build="build-a"))["total"] == 3
    assert (await _page(anonymous, scope="available", app_build="build-a"))["total"] == 0


@pytest.mark.asyncio
async def test_spaces_apply_stable_history_ownership_before_counts(library, qa, monkeypatch):
    old = _run(library, owner="old@example.com", app_build="build-a")
    _run(library, app_build="build-a")
    identity = AccessIdentity(
        "qa@example.com",
        True,
        "cloudflare",
        principal_id="principal-a",
        history_emails=frozenset({"old@example.com"}),
        spaces=True,
    )
    monkeypatch.setitem(app.dependency_overrides, public_tier, lambda: identity)
    page = await _page(qa, app_build="build-a")
    assert _ids(page) == [old] and page["total"] == 1
    assert (await qa.get("/api/runs", params={"scope": "all"})).status_code == 403
    assert (await qa.get("/api/runs", params={"scope": "everyone"})).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_search_counts_intersect_filters_and_visibility(library, qa, monkeypatch, fallback):
    if fallback:
        with sqlite3.connect(library.db) as conn:
            triggers = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'run_catalog_%'"
            ).fetchall()
            for (name,) in triggers:
                conn.execute(f'DROP TRIGGER "{name}"')
            conn.execute("DROP TABLE runs_fts")
        monkeypatch.setattr(run_catalog, "fts5_available", lambda conn: False)
        run_catalog.migrate(library.db)
    matching = _run(library, app_build="build-a")
    _run(library, app_build="build-b")
    _run(library, owner="qa2@example.com", app_build="build-a")
    page = await _page(qa, q="goal", app_build="build-a", limit=1)
    assert _ids(page) == [matching] and page["total"] == 1
    assert page["next_cursor"] is None
    assert page["warnings"] == (["search_fallback_substring"] if fallback else [])
    assert (await _page(qa, q="no match"))["total"] == 0


@pytest.mark.asyncio
async def test_review_migration_is_nullable_and_preserves_the_snapshot(library, qa):
    session_id = _run(library, app_build="build-a")
    with sqlite3.connect(library.db) as conn:
        conn.execute("ALTER TABLE run_meta DROP COLUMN review")
        conn.execute("UPDATE schema_revisions SET revision = 1 WHERE module = 'run_meta_ext'")
    StorageManager(library.db, library.traces)
    page = await _page(qa, app_build="build-a")
    assert _ids(page) == [session_id]
    assert page["runs"][0]["review"] is None
    assert (await _page(qa, review="pending"))["total"] == 0
    report = run_snapshot.migrate(library.db)
    assert report.backup_path is None and report.backfilled == 0


@pytest.mark.asyncio
async def test_missing_review_migration_is_retryable_not_an_unscoped_fallback(library, qa):
    await _page(qa)
    with sqlite3.connect(library.db) as conn:
        conn.execute("ALTER TABLE run_meta DROP COLUMN review")
    response = await qa.get("/api/runs", params={"app_build": "build-a"})
    assert response.status_code == 503
    assert response.json()["error"] == "catalog_not_ready"
    assert response.json()["retryable"] is True
