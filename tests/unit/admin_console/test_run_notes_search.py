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

"""Run search over comment and annotation text (CHE-1478).

The catalog index (`runs_fts.notes`) is kept current by triggers on the notes tables, so the
comment and annotation routes need no change. Snippets are built after the visibility join.
"""

from contextlib import closing
import sqlite3

import pytest

from apps.admin_console.database.repositories import annotation_repository as notes
from artemis.data_engine import run_catalog, schema_revisions

AUTHOR = "prn_dev"
QA = "qa@example.com"


def _own(library, goal="plain goal", owner=QA, **kwargs) -> str:
    run = library.seed(goal, **kwargs)
    with sqlite3.connect(library.db) as conn:
        conn.execute("UPDATE run_meta SET requested_by = ? WHERE session_id = ?", (owner, run))
    return run


def _comment(library, run, body):
    with closing(sqlite3.connect(library.db)) as conn:
        return notes.add_comment(conn, run, body, AUTHOR)


def _annotate(library, run, body):
    step = library.step(run, 1)
    with closing(sqlite3.connect(library.db)) as conn:
        return notes.add_annotation(
            conn, run, notes.StepAnchor(step), body=body, author_principal_id=AUTHOR
        )


def _sql(library, statement, *params):
    with sqlite3.connect(library.db) as conn:
        conn.execute(statement, params)


async def _search(qa, term, scope="mine"):
    response = await qa.get("/api/runs", params={"q": term, "scope": scope})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def indexed(library):
    notes.migrate(library.db)
    return library


@pytest.mark.asyncio
async def test_a_word_only_in_a_comment_finds_the_run_with_a_snippet(indexed, qa):
    run = _own(indexed)
    _own(indexed, "another goal")
    _comment(indexed, run, "The keyboard covers the zebrafish button on small screens")

    body = await _search(qa, "zebrafish")

    assert [r["session_id"] for r in body["runs"]] == [run] and body["total"] == 1
    snippet = body["runs"][0]["snippet"]
    assert snippet["field"] == "comment" and "zebrafish" in snippet["text"]


@pytest.mark.asyncio
async def test_a_word_only_in_an_annotation_finds_the_run_with_a_snippet(indexed, qa):
    run = _own(indexed)
    _annotate(indexed, run, "Spinner never stops: narwhalbug")

    body = await _search(qa, "narwhalbug")

    assert [r["session_id"] for r in body["runs"]] == [run]
    assert body["runs"][0]["snippet"] == {
        "field": "annotation",
        "text": "Spinner never stops: narwhalbug",
    }


@pytest.mark.asyncio
async def test_goal_matches_rank_before_note_matches(indexed, qa):
    by_note = _own(indexed, "unrelated", age_days=0)
    by_goal = _own(indexed, "open the okapi screen", age_days=5)
    _comment(indexed, by_note, "okapi okapi okapi okapi")

    body = await _search(qa, "okapi")

    assert [r["session_id"] for r in body["runs"]] == [by_goal, by_note]
    assert body["runs"][0]["snippet"]["field"] == "prompt"


@pytest.mark.asyncio
async def test_a_hidden_run_never_leaks_note_text(indexed, qa):
    visible = _own(indexed)
    hidden = _own(indexed, owner="qa2@example.com")
    for run in (visible, hidden):
        _comment(indexed, run, "quokka observation")
        _annotate(indexed, run, "quokka-annotation-secret")

    for term in ("quokka", "quokka-annotation-secret"):
        response = await qa.get("/api/runs", params={"q": term, "scope": "mine"})
        body = response.json()
        assert [r["session_id"] for r in body["runs"]] == [visible]
        assert body["total"] == len(body["runs"])
        assert hidden not in response.text

    # Another viewer's private note text never reaches an everyone-scope search either.
    response = await qa.get("/api/runs", params={"q": "quokka", "scope": "everyone"})
    assert hidden not in response.text


@pytest.mark.asyncio
async def test_a_removed_run_note_text_is_not_returned(indexed, qa):
    run = _own(indexed)
    _comment(indexed, run, "wombat sighting")
    _sql(indexed, "UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?", run)

    assert (await _search(qa, "wombat"))["runs"] == []


@pytest.mark.asyncio
async def test_a_read_only_run_note_snippet_is_redacted(indexed, qa):
    run = _own(indexed, owner="someone@example.com")
    _comment(indexed, run, "leaked sk-abcdefghijklmnop1234 gnu")

    response = await qa.get("/api/runs", params={"q": "gnu", "scope": "everyone"})

    assert "sk-abcdefghijklmnop1234" not in response.text


def test_editing_and_deleting_notes_keeps_the_index_current(indexed):
    run = _own(indexed)
    comment = _comment(indexed, run, "ibex first")
    annotation = _annotate(indexed, run, "lynx first")

    def found(term):
        with sqlite3.connect(indexed.db) as conn:
            return run_catalog.search_session_ids(conn, term)

    assert found("ibex") == [run] and found("lynx") == [run]
    _sql(
        indexed,
        "UPDATE run_comments SET body = 'puma second' WHERE comment_id = ?",
        comment.comment_id,
    )
    _sql(
        indexed,
        "UPDATE run_annotations SET body = 'serval second' WHERE annotation_id = ?",
        annotation.annotation_id,
    )
    assert found("ibex") == [] and found("lynx") == []
    assert found("puma") == [run] and found("serval") == [run]
    _sql(indexed, "DELETE FROM run_comments WHERE comment_id = ?", comment.comment_id)
    assert found("puma") == [] and found("serval") == [run]
    _sql(indexed, "DELETE FROM run_annotations WHERE annotation_id = ?", annotation.annotation_id)
    assert found("serval") == []


@pytest.mark.parametrize("how", ["session_delete", "tombstone"])
def test_removing_a_run_removes_its_notes_from_the_index(indexed, how):
    run = _own(indexed)
    _comment(indexed, run, "tapir remark")
    _annotate(indexed, run, "tapir mark")
    statement = {
        "session_delete": "DELETE FROM sessions WHERE session_id = ?",
        "tombstone": "UPDATE run_meta SET deleted_at = 1 WHERE session_id = ?",
    }[how]

    _sql(indexed, statement, run)

    with sqlite3.connect(indexed.db) as conn:
        assert run_catalog.search_session_ids(conn, "tapir") == []
        assert conn.execute(
            "SELECT count(*) FROM runs_fts WHERE runs_fts MATCH 'tapir'"
        ).fetchone() == (0,)


def test_existing_database_upgrades_once_with_a_backup_and_a_rebuilt_index(library):
    run = _own(library)
    # Notes written before the index exists (old binary): tables only, no notes triggers.
    schema_revisions.apply(library.db, "notes", notes.REVISIONS)
    _comment(library, run, "pangolin legacy comment")
    with sqlite3.connect(library.db) as conn:
        assert run_catalog.search_session_ids(conn, "pangolin") == []

    first = run_catalog.migrate_notes(library.db)
    second = run_catalog.migrate_notes(library.db)

    assert first.applied == (1,) and first.backup_path is not None and first.backup_path.exists()
    assert second.applied == () and second.backup_path is None
    with sqlite3.connect(library.db) as conn:
        assert run_catalog.search_session_ids(conn, "pangolin") == [run]
        before = conn.execute("SELECT rowid, prompt, meta, notes FROM runs_fts").fetchall()
    run_catalog.migrate_notes(library.db)
    with sqlite3.connect(library.db) as conn:
        assert conn.execute("SELECT rowid, prompt, meta, notes FROM runs_fts").fetchall() == before


def test_notes_migration_is_a_noop_before_the_notes_tables_exist(library):
    _own(library)

    assert run_catalog.migrate_notes(library.db).applied == ()
    notes.migrate(library.db)
    with sqlite3.connect(library.db) as conn:
        assert conn.execute(
            "SELECT revision FROM schema_revisions WHERE module = ?", (run_catalog.NOTES_MODULE,)
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_substring_fallback_still_searches_everything_but_notes(library, qa, monkeypatch):
    run = _own(library, "fallback goal")
    notes.migrate(library.db)
    with sqlite3.connect(library.db) as conn:
        for trigger in (*run_catalog._TRIGGERS_FTS, *run_catalog._TRIGGERS_NOTES):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE runs_fts")
    monkeypatch.setattr(run_catalog, "fts5_available", lambda conn: False)
    run_catalog.migrate(library.db)
    _comment(library, run, "added in fallback mode")  # must not fail without runs_fts

    body = await _search(qa, "fallback goal")

    assert [r["session_id"] for r in body["runs"]] == [run]
    assert body["warnings"] == ["search_fallback_substring"]
