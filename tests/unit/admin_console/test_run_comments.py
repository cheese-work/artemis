from contextlib import closing
import sqlite3
from types import SimpleNamespace
from uuid import UUID

import pytest

from apps.admin_console.core import config
from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.database.repositories import annotation_repository as notes
from apps.admin_console.routers import run_annotations
from apps.admin_console.services.host_registry import HostRegistry
from artemis.data_engine import schema_revisions

AUTHOR = "prn_dev"


@pytest.fixture
def thread(library):
    run = library.seed("run with comments")
    notes.migrate(library.db)
    with closing(sqlite3.connect(library.db)) as conn:
        yield conn, run


def test_upgrade_from_revision_one_preserves_notes_for_the_old_reader(library):
    run = library.seed("existing run")
    step = library.step(run, 1)
    revision_one = notes.REVISIONS[:1]
    assert schema_revisions.apply(library.db, "notes", revision_one).applied == (1,)
    with closing(sqlite3.connect(library.db)) as conn:
        annotation = notes.add_annotation(
            conn, run, notes.StepAnchor(step), body="existing note", author_principal_id=AUTHOR
        )
        old_schema = dict(conn.execute("SELECT name, sql FROM sqlite_master"))

    assert notes.migrate(library.db).applied == tuple(range(2, len(notes.REVISIONS) + 1))
    assert schema_revisions.apply(library.db, "notes", revision_one).applied == ()

    with closing(sqlite3.connect(library.db)) as conn:
        new_schema = dict(conn.execute("SELECT name, sql FROM sqlite_master"))
        assert {name: new_schema[name] for name in old_schema} == old_schema
        assert conn.execute(
            "SELECT revision FROM schema_revisions WHERE module = 'notes'"
        ).fetchone() == (len(notes.REVISIONS),)
        index_columns = conn.execute("PRAGMA index_info(idx_run_comments_session)")
        assert [column[2] for column in index_columns] == ["session_id", "created_at"]
        assert notes.get_annotation(conn, annotation.annotation_id) == annotation


def test_list_is_scoped_to_one_run_and_sorted_by_time_then_id(thread, library, monkeypatch):
    conn, run = thread
    other_run = library.seed("other run")
    timestamps = iter([200.0, 200.0, 100.0, 50.0])
    ids = iter(UUID(int=value) for value in (3, 2, 4, 1))
    monkeypatch.setattr(notes, "time", SimpleNamespace(time=lambda: next(timestamps)))
    monkeypatch.setattr(notes, "uuid", SimpleNamespace(uuid4=lambda: next(ids)))

    assert notes.list_comments(conn, run) == []
    tie_later_id = notes.add_comment(conn, run, "later ID", AUTHOR)
    tie_earlier_id = notes.add_comment(conn, run, "earlier ID", AUTHOR)
    oldest = notes.add_comment(conn, run, "oldest", AUTHOR)
    notes.add_comment(conn, other_run, "other thread", AUTHOR)

    assert tie_earlier_id.comment_id < tie_later_id.comment_id
    assert oldest.session_id == run and oldest.author_principal_id == AUTHOR
    assert oldest.created_at == 100.0 and oldest.edited_at is None
    with closing(sqlite3.connect(library.db)) as reader:
        assert notes.list_comments(reader, run) == [oldest, tie_earlier_id, tie_later_id]


@pytest.mark.parametrize("body", ["\u00e9" * 4000, "  <b>x</b>\n**x**  "], ids=["cap", "markup"])
def test_stores_and_returns_plain_text_verbatim(thread, body):
    conn, run = thread
    comment = notes.add_comment(conn, run, body, AUTHOR)

    assert comment.body == body
    assert conn.execute("SELECT body FROM run_comments").fetchone() == (body,)
    assert notes.list_comments(conn, run) == [comment]


@pytest.mark.parametrize(
    "body", ["", "x" * 4001, "before\x00after"], ids=["empty", "over-cap", "nul"]
)
def test_rejects_invalid_bodies_without_storing_them(thread, body):
    conn, run = thread
    with pytest.raises(AdminAPIError) as caught:
        notes.add_comment(conn, run, body, AUTHOR)

    assert (caught.value.status_code, caught.value.code) == (422, "request_invalid")
    assert caught.value.fix and caught.value.docs_url == f"{notes.DOCS}#request_invalid"
    assert notes.list_comments(conn, run) == []


def test_add_comment_uses_configured_cap(thread, monkeypatch):
    conn, run = thread
    monkeypatch.setattr(config, "COMMENT_BODY_MAX_CHARS", 4)

    notes.add_comment(conn, run, "1234", AUTHOR)
    with pytest.raises(AdminAPIError) as caught:
        notes.add_comment(conn, run, "12345", AUTHOR)
    assert caught.value.code == "request_invalid"


def test_migrate_removes_only_comments_of_deleted_runs(thread, library):
    conn, run = thread
    other_run = library.seed("kept run")
    notes.add_comment(conn, run, "orphan", AUTHOR)
    kept = notes.add_comment(conn, other_run, "kept", AUTHOR)
    with conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (run,))

    assert notes.migrate(library.db).applied == ()
    assert notes.list_comments(conn, run) == []
    assert notes.list_comments(conn, other_run) == [kept]


@pytest.fixture
def note_limiter(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    registry = HostRegistry()
    registry.clock = lambda: clock.now
    monkeypatch.setattr(run_annotations, "host_registry", registry)
    return clock


def test_default_note_write_budget_is_30_per_principal_per_minute(note_limiter):
    for _ in range(30):
        run_annotations.require_note_write(AUTHOR)

    note_limiter.now += 59.0
    with pytest.raises(AdminAPIError) as caught:
        run_annotations.require_note_write(AUTHOR)
    assert (caught.value.status_code, caught.value.code, caught.value.retry_after) == (
        429,
        "rate_limited",
        60,
    )
    run_annotations.require_note_write("prn_other")
    note_limiter.now += 1.0
    run_annotations.require_note_write(AUTHOR)


def test_note_write_budget_uses_configured_limit(note_limiter, monkeypatch):
    monkeypatch.setattr(config, "NOTE_WRITE_LIMIT", 1)

    run_annotations.require_note_write(AUTHOR)
    with pytest.raises(AdminAPIError) as caught:
        run_annotations.require_note_write(AUTHOR)
    assert caught.value.code == "rate_limited"
