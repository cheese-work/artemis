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

"""Run catalog at the server API seam: `GET /api/runs` and `/api/runs/{id}` (CHE-1091).

Search, keyset paging, id resolution and tombstones are asserted through HTTP;
the catalog's storage rules are covered in tests/unit/data_engine.
"""

import json
from pathlib import Path
import sqlite3
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.server import app


@pytest.fixture
def env(tmp_path, monkeypatch):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from artemis.data_engine.storage import StorageManager

    db = tmp_path / "data_engine.db"
    StorageManager(db, tmp_path)  # full current schema plus the catalog
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", tmp_path / "traces")
    return db


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


def _seed(db: Path, goal="goal", start=1.0, status="completed", sid=None, device=None) -> str:
    sid = sid or str(uuid.uuid4())
    info = json.dumps({"device_id": device}) if device else None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status, device_info) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, goal, start, status, info),
        )
    return sid


def _meta(db: Path, sid: str, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with sqlite3.connect(db) as conn:
        conn.execute(f"UPDATE run_meta SET {sets} WHERE session_id = ?", (*fields.values(), sid))


async def _get(path: str, **params):
    async with _client() as client:
        return await client.get(path, params=params)


async def _ids(**params) -> list[str]:
    response = await _get("/api/runs", **params)
    assert response.status_code == 200, response.text
    return [run["session_id"] for run in response.json()["runs"]]


# -- listing, cursor ----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_is_newest_first_with_catalog_fields(env):
    old = _seed(env, "old", start=1.0, device="emu-1")
    new = _seed(env, "new", start=2.0)
    _meta(env, new, host_id="host-a", requested_by="dana@example.com", pinned=1)

    body = (await _get("/api/runs")).json()

    assert [r["session_id"] for r in body["runs"]] == [new, old]
    first = body["runs"][0]
    assert first["prompt"] == "new" and first["host_id"] == "host-a"
    assert first["requested_by"] == "dana@example.com" and first["pinned"] is True
    assert body["runs"][1]["device_ref"] == {"host_id": None, "serial": "emu-1"}
    assert body["next_cursor"] is None and body["warnings"] == []


@pytest.mark.asyncio
async def test_status_is_canonical_in_listing(env):
    done = _seed(env, "legacy success row", status="success")
    assert (await _get("/api/runs")).json()["runs"][0]["status"] == "completed"
    assert await _ids(status="completed") == [done]


@pytest.mark.asyncio
async def test_keyset_cursor_pages_every_run_once_even_with_equal_start_times(env):
    ids = [_seed(env, f"run {i}", start=float(i // 3)) for i in range(8)]  # ties on start_time
    null_start = _seed(env, "no start time", start=None)

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = (await _get("/api/runs", **params)).json()
        seen += [r["session_id"] for r in body["runs"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == sorted([*ids, null_start])
    assert len(seen) == len(set(seen))
    assert seen[-1] == null_start  # unstarted runs sort last
    starts = [0.0 if s == null_start else float(ids.index(s) // 3) for s in seen]
    assert starts[:-1] == sorted(starts[:-1], reverse=True)


@pytest.mark.asyncio
async def test_cursor_is_stable_when_newer_runs_arrive_between_pages(env):
    ids = [_seed(env, f"run {i}", start=float(i)) for i in range(1, 7)]
    page1 = (await _get("/api/runs", limit=3)).json()
    _seed(env, "arrives mid-paging", start=100.0)

    page2 = (await _get("/api/runs", limit=3, cursor=page1["next_cursor"])).json()

    got = [r["session_id"] for r in page1["runs"] + page2["runs"]]
    assert got == list(reversed(ids))  # no skip, no repeat


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-cursor", "e30=", "WzEsMl0=", "%%%"])
async def test_malformed_cursor_is_a_400(env, bad):
    _seed(env)
    assert (await _get("/api/runs", cursor=bad)).status_code == 400


@pytest.mark.asyncio
async def test_limit_is_clamped(env):
    for i in range(3):
        _seed(env, f"r{i}", start=float(i))
    assert len(await _ids(limit=1)) == 1
    assert (await _get("/api/runs", limit=0)).status_code == 422
    assert (await _get("/api/runs", limit=100000)).status_code == 422


@pytest.mark.asyncio
async def test_list_query_is_served_by_an_index(env):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    for i in range(20):
        _seed(env, f"run {i}", start=float(i))
    sql, params = run_catalog_repo.list_query(limit=5, cursor=(10.0, "z"))
    with sqlite3.connect(env) as conn:
        plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    assert "SEARCH s USING" in plan and "idx_sessions_start_order" in plan, (
        plan
    )  # seeks past the cursor
    assert "TEMP B-TREE" not in plan, plan  # served in index order, no sort


# -- filters and search -------------------------------------------------------


@pytest.mark.asyncio
async def test_filters_combine(env):
    a = _seed(env, "alpha", start=10.0, device="emu-1")
    b = _seed(env, "beta", start=20.0, status="failed", device="emu-2")
    c = _seed(env, "gamma", start=30.0, device="emu-1")
    _meta(env, a, host_id="host-a", requested_by="dana")
    _meta(env, b, host_id="host-b", requested_by="dana")
    _meta(env, c, requested_by="lee")

    assert await _ids(status="failed") == [b]
    assert await _ids(device="emu-1") == [c, a]
    assert await _ids(host="host-a") == [a]
    assert await _ids(host="local") == [c]  # runs from this server's own adb have no host
    assert await _ids(requester="dana") == [b, a]
    assert await _ids(since=15, until=25) == [b]
    assert await _ids(since="1970-01-01T00:00:25Z") == [c]
    assert await _ids(requester="dana", device="emu-1") == [a]
    assert (await _get("/api/runs", since="yesterday-ish")).status_code == 400


@pytest.mark.asyncio
async def test_search_finds_prompt_and_run_meta_text_and_pages(env):
    wifi = [_seed(env, f"enable wifi step {i}", start=float(i)) for i in range(5)]
    other = _seed(env, "send a message", start=50.0)
    _meta(env, other, requested_by="dana@example.com")

    assert await _ids(q="wifi") == list(reversed(wifi))
    assert await _ids(q="enab wif") == []  # terms are whole words (last one may be a prefix)
    assert await _ids(q="enable wif") == list(reversed(wifi))
    assert await _ids(q="dana") == [other]
    page = (await _get("/api/runs", q="wifi", limit=2)).json()
    assert len(page["runs"]) == 2 and page["next_cursor"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "q", ['"', "NEAR(", "col:x", "wifi OR", "(((", "'; DROP TABLE run_meta;--", "*"]
)
async def test_raw_fts_syntax_never_reaches_the_index(env, q):
    _seed(env, "enable wifi")
    response = await _get("/api/runs", q=q)
    assert response.status_code == 200, response.text
    with sqlite3.connect(env) as conn:
        assert conn.execute("SELECT count(*) FROM run_meta").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_search_terms_are_anded_not_or(env):
    both = _seed(env, "alpha and beta together")
    _seed(env, "alpha only")
    _seed(env, "beta only")
    # as raw FTS syntax this would be an OR over three runs
    assert await _ids(q="alpha OR beta") == []
    assert await _ids(q="alpha beta") == [both]


@pytest.mark.asyncio
async def test_punctuation_only_search_matches_nothing_not_everything(env):
    _seed(env, "something")
    assert await _ids(q="!!!") == []
    assert len(await _ids(q="")) == 1  # an empty box is no search at all


@pytest.mark.asyncio
async def test_substring_fallback_when_fts5_is_missing(tmp_path, monkeypatch):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from artemis.data_engine import run_catalog
    from artemis.data_engine.storage import StorageManager

    monkeypatch.setattr(run_catalog, "fts5_available", lambda conn: False)
    db = tmp_path / "data_engine.db"
    StorageManager(db, tmp_path)
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", tmp_path / "traces")
    sid = _seed(db, "reach 100% battery")
    _seed(db, "reach 1000 battery")

    body = (await _get("/api/runs", q="100% BATTERY")).json()

    assert [r["session_id"] for r in body["runs"]] == [sid]  # literal %, case-insensitive
    assert body["warnings"] == ["search_fallback_substring"]


# -- single run, ids ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_full_id_and_unknown_id(env):
    sid = _seed(env, "goal", device="emu-1")
    body = (await _get(f"/api/runs/{sid}")).json()
    assert body["session_id"] == sid and body["prompt"] == "goal"
    assert (await _get(f"/api/runs/{uuid.uuid4()}")).status_code == 404


@pytest.mark.asyncio
async def test_unique_eight_char_prefix_resolves(env):
    sid = _seed(env, "goal")
    response = await _get(f"/api/runs/{sid[:8]}")
    assert response.status_code == 200 and response.json()["session_id"] == sid


@pytest.mark.asyncio
async def test_ambiguous_eight_char_prefix_returns_candidates(env):
    a = _seed(env, "first", start=1.0, sid="abcd1234-0000-4000-8000-000000000001")
    b = _seed(env, "second", start=2.0, sid="abcd1234-0000-4000-8000-000000000002")
    _seed(env, "other", sid="ffff0000-0000-4000-8000-000000000003")

    response = await _get("/api/runs/abcd1234")

    assert response.status_code == 409
    detail = response.json()
    assert detail["error"] == "ambiguous_prefix"
    assert [c["session_id"] for c in detail["candidates"]] == [b, a]  # newest first
    assert {c["prompt"] for c in detail["candidates"]} == {"first", "second"}
    assert (await _get("/api/runs/abcd1235")).status_code == 404
    assert (await _get(f"/api/runs/{a}")).status_code == 200  # a full id is never ambiguous


@pytest.mark.asyncio
async def test_prefix_ignores_tombstoned_candidates(env):
    live = _seed(env, "live", sid="abcd1234-0000-4000-8000-000000000001")
    gone = _seed(env, "gone", sid="abcd1234-0000-4000-8000-000000000002")
    _meta(env, gone, deleted_at=5.0, deleted_reason="retention")
    response = await _get("/api/runs/abcd1234")
    assert response.status_code == 200 and response.json()["session_id"] == live


@pytest.mark.asyncio
async def test_legacy_ids_are_readable_and_hostile_ids_are_rejected(env):
    legacy = _seed(env, "old run", sid="2025-06-run_7.v2")
    assert (await _get(f"/api/runs/{legacy}")).json()["session_id"] == legacy
    for bad in ["has space", "a;b", "x" * 129, "%00", "..%5Cetc", "a%0Ab"]:
        response = await _get(f"/api/runs/{bad}")
        assert response.status_code == 400, (bad, response.status_code)
        assert response.json()["error"] == "invalid_session_id"


def test_id_validation_rules(tmp_path):
    from artemis.data_engine.run_catalog import validate_session_id

    traces = tmp_path / "traces"
    traces.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (traces / "escape").symlink_to(outside, target_is_directory=True)

    assert validate_session_id("legacy.run-1_x") == "legacy.run-1_x"
    sid = str(uuid.uuid4())
    assert validate_session_id(sid, strict=True, base_dir=traces) == sid
    for bad in ["", ".", "..", "a/b", "a\\b", "x" * 129, "bad id"]:
        with pytest.raises(ValueError):
            validate_session_id(bad)
    with pytest.raises(ValueError):
        validate_session_id("escape", base_dir=traces)  # realpath leaves the traces dir
    for non_canonical in [
        "legacy-1",
        sid.upper(),
        sid.replace("-", ""),
        f"{{{sid}}}",
        f"urn:uuid:{sid}",
    ]:
        with pytest.raises(ValueError):
            validate_session_id(non_canonical, strict=True)


def test_writes_accept_only_canonical_uuids(env):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    with pytest.raises(ValueError):
        run_catalog_repo.set_meta("legacy-1", requested_by="x")
    with pytest.raises(ValueError):
        run_catalog_repo.set_meta(str(uuid.uuid4()).upper(), requested_by="x")
    sid = _seed(env)
    assert run_catalog_repo.set_meta(sid, requested_by="dana", pinned=True)
    with sqlite3.connect(env) as conn:
        assert conn.execute(
            "SELECT requested_by, pinned FROM run_meta WHERE session_id = ?", (sid,)
        ).fetchone() == ("dana", 1)
    assert not run_catalog_repo.set_meta(str(uuid.uuid4()), requested_by="nobody")  # no such run
    with pytest.raises(ValueError):
        run_catalog_repo.set_meta(sid, deleted_at=1.0)  # tombstones go through tombstone()


# -- tombstones --------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_hides_the_run_everywhere_and_reads_as_removed(env):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    sid = _seed(env, "secret wifi password run")
    keep = _seed(env, "ordinary wifi run", start=0.5)
    assert run_catalog_repo.tombstone(sid, "retention")

    assert await _ids() == [keep]
    assert await _ids(q="secret") == []
    assert await _ids(q="wifi") == [keep]
    response = await _get(f"/api/runs/{sid}")
    assert response.status_code == 410
    assert response.json()["error"] == "removed" and response.json()["reason"] == "retention"
    assert (await _get(f"/api/runs/{sid[:8]}")).status_code == 410


@pytest.mark.asyncio
async def test_tombstone_wins_over_a_session_row_that_is_written_again(env, tmp_path):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from artemis.data_engine.models import SessionMetadata
    from artemis.data_engine.storage import StorageManager

    sid = uuid.uuid4()
    manager = StorageManager(env, tmp_path)
    meta = SessionMetadata(session_id=sid, initial_goal="resurrect me", start_time=1.0)
    manager.create_session(meta)
    assert run_catalog_repo.tombstone(str(sid), "user_delete")

    meta.initial_goal = "resurrect me again"
    manager.create_session(meta)  # a restarted worker upserting the same session id
    with sqlite3.connect(env) as conn:  # even a REPLACE-style rewrite of the row
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, initial_goal, start_time, status) "
            "VALUES (?, 'replaced', 2.0, 'running')",
            (str(sid),),
        )

    assert await _ids() == []
    assert await _ids(q="replaced") == []
    assert (await _get(f"/api/runs/{sid}")).status_code == 410


@pytest.mark.asyncio
async def test_first_tombstone_reason_is_kept_and_hard_delete_is_removed_not_missing(env):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    sid = _seed(env)
    assert run_catalog_repo.tombstone(sid, "user_delete")
    assert not run_catalog_repo.tombstone(sid, "retention")  # already removed: first reason stands
    assert (await _get(f"/api/runs/{sid}")).json()["reason"] == "user_delete"

    other = _seed(env)
    with sqlite3.connect(env) as conn:  # the legacy hard-delete endpoint's SQL
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (other,))
    response = await _get(f"/api/runs/{other}")
    assert response.status_code == 410 and response.json()["reason"] == "session_deleted"


@pytest.mark.asyncio
async def test_catalog_not_ready_is_a_503_not_a_crash(tmp_path, monkeypatch):
    from apps.admin_console.database import connection
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    db = tmp_path / "bare.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x)")
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(connection, "_initialized_dbs", {str(db)})  # skip schema bootstrap
    response = await _get("/api/runs")
    assert response.status_code == 503 and response.json()["error"] == "catalog_not_ready"


@pytest.mark.asyncio
async def test_recording_states_are_stored_per_recording_and_listed(env):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    sid = _seed(env, "with video")
    assert run_catalog_repo.set_recording_state(sid, "rec-1", capture="recording")
    assert run_catalog_repo.set_recording_state(sid, "rec-1", capture="stopped")
    assert run_catalog_repo.set_recording_state(sid, "rec-1", transfer="uploading")  # keeps capture
    assert run_catalog_repo.set_recording_state(sid, "rec-2", capture="missing:spool_full")
    assert not run_catalog_repo.set_recording_state(str(uuid.uuid4()), "rec-1", capture="pending")
    for bad in [{"capture": "ready"}, {"transfer": "ready"}, {"capture": "missing"}]:
        with pytest.raises(ValueError):
            run_catalog_repo.set_recording_state(sid, "rec-1", **bad)

    run = (await _get(f"/api/runs/{sid}")).json()
    assert run["recordings"] == [
        {"recording_id": "rec-1", "capture": "stopped", "transfer": "uploading"},
        {"recording_id": "rec-2", "capture": "missing:spool_full", "transfer": None},
    ]
    assert (await _get("/api/runs")).json()["runs"][0]["recordings"] == run["recordings"]


# -- review round 1 (Sol): R1, R2, R4 -----------------------------------------


@pytest.mark.asyncio
async def test_first_page_lists_an_unstarted_run_once(env):
    dated = _seed(env, "dated", start=1.0)
    unstarted = _seed(env, "unstarted", start=None)
    assert await _ids(limit=50) == [dated, unstarted]


@pytest.mark.asyncio
async def test_dated_and_unstarted_runs_page_without_repeats_at_every_limit(env):
    ids = [_seed(env, f"d{i}", start=float(i + 1)) for i in range(3)]
    ids += [_seed(env, f"u{i}", start=None) for i in range(3)]
    for limit in (1, 2, 3, 4, 6, 50):
        seen, cursor = [], None
        for _ in range(10):
            body = (
                await _get("/api/runs", limit=limit, **({"cursor": cursor} if cursor else {}))
            ).json()
            seen += [r["session_id"] for r in body["runs"]]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 6 and set(seen) == set(ids), (limit, seen)
        assert seen[:3] == list(reversed(ids[:3]))  # dated first, newest first


@pytest.mark.asyncio
async def test_prefix_resolves_live_rows_behind_many_newer_tombstones(env):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    live = [
        _seed(env, "live", start=1.0, sid=f"abcd1234-0000-4000-8000-{i:012d}") for i in range(2)
    ]
    for i in range(2, 30):  # more tombstones than any candidate cap, all newer
        gone = _seed(env, "gone", start=100.0, sid=f"abcd1234-0000-4000-8000-{i:012d}")
        assert run_catalog_repo.tombstone(gone, "retention")

    response = await _get("/api/runs/abcd1234")

    assert response.status_code == 409, response.json()
    assert {c["session_id"] for c in response.json()["candidates"]} == set(live)


@pytest.mark.asyncio
async def test_removed_response_after_hard_delete_names_the_run(env):
    sid = _seed(env)
    with sqlite3.connect(env) as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    response = await _get(f"/api/runs/{sid}")
    assert response.status_code == 410
    assert response.json()["session_id"] == sid
