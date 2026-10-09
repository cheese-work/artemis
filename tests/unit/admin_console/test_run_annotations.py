"""Run annotations: data model, anchor validation and evidence resolver (CHE-1464).

Contract: docs/board-api.md "Notes" and the error catalog; migrations follow
docs/board-operations.md "Migration contract". No routes yet: the API seam is
the error each outcome maps to.
"""

from pathlib import Path
import sqlite3
import uuid

import pytest

from apps.admin_console.core.access_control import AdminAPIError, _error_response
from apps.admin_console.database.repositories import annotation_repository as notes
from apps.admin_console.services import evidence_resolver
from artemis.data_engine import schema_revisions

DOCS = "https://github.com/cheese-work/artemis/blob/main/docs/board-api.md"
AUTHOR = "prn_dev"


@pytest.fixture
def run(library):
    notes.migrate(library.db)
    return library.seed("open settings")


def _conn(library) -> sqlite3.Connection:
    conn = sqlite3.connect(library.db)
    conn.row_factory = sqlite3.Row
    return conn


def _session_start(library, sid: str) -> float:
    with _conn(library) as conn:
        return conn.execute(
            "SELECT start_time FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()[0]


def _recording(
    library, sid, *, start=5.0, end=65.0, clock=True, status="ready"
) -> tuple[str, Path]:
    """A recording that starts ``start`` s and ends ``end`` s into the session."""
    session_start = _session_start(library, sid)
    path = library.traces / sid / f"{uuid.uuid4().hex}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mp4")
    video_id = f"rec_{uuid.uuid4().hex[:8]}"
    with _conn(library) as conn:
        conn.execute(
            "INSERT INTO video_recordings (video_id, session_id, start_time, end_time, "
            "local_video_path, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                video_id,
                sid,
                session_start + start if clock else None,
                session_start + end if clock else None,
                str(path),
                status,
            ),
        )
    return video_id, path


def _add(library, sid, anchor, body="Spinner never stops here."):
    with _conn(library) as conn:
        return notes.add_annotation(conn, sid, anchor, body=body, author_principal_id=AUTHOR)


def _resolve(library, annotation):
    with _conn(library) as conn:
        return evidence_resolver.resolve(conn, annotation, library.images)


def _envelope(exc: AdminAPIError) -> dict:
    import json

    return json.loads(_error_response(exc).body)


def _count(library) -> int:
    with _conn(library) as conn:
        return conn.execute("SELECT count(*) FROM run_annotations").fetchone()[0]


# -- resolver outcomes -----------------------------------------------------------------


def test_step_anchor_with_its_screenshot_resolves_exact(library, run):
    library.image("shot-1")
    step_id = library.step(run, 12, pre="shot-1")

    annotation = _add(library, run, notes.StepAnchor(step_id))
    resolution = _resolve(library, annotation)

    assert resolution.status == "exact"
    assert resolution.evidence == {"kind": "step", "step_id": step_id, "image_names": ["shot-1"]}
    assert annotation.step_number == 12 and annotation.author_principal_id == AUTHOR
    assert annotation.resolved is False and annotation.legacy_fallback is False


def test_step_whose_screenshot_is_gone_resolves_missing_without_media(library, run):
    shot = library.image("shot-1")
    step_id = library.step(run, 1, pre="shot-1")
    annotation = _add(library, run, notes.StepAnchor(step_id))
    shot.unlink()

    resolution = _resolve(library, annotation)

    assert resolution.status == "missing" and resolution.evidence is None


def test_step_never_borrows_a_neighbouring_steps_screenshot(library, run):
    library.image("shot-2")
    step_id = library.step(run, 1)  # no screenshot of its own
    library.step(run, 2, pre="shot-2")

    resolution = _resolve(library, _add(library, run, notes.StepAnchor(step_id)))

    assert resolution.status == "missing" and resolution.evidence is None


def test_recording_anchor_resolves_exact_at_the_session_relative_moment(library, run):
    video_id, _ = _recording(library, run, start=5.0, end=65.0)

    annotation = _add(library, run, notes.RecordingAnchor(video_id, 42_000))
    resolution = _resolve(library, annotation)

    assert annotation.legacy_fallback is False
    assert resolution.status == "exact"
    assert resolution.evidence == {
        "kind": "recording",
        "recording_id": video_id,
        "position_ms": 37_000,
    }


def test_recording_moment_outside_a_refinalized_recording_is_missing_not_clamped(library, run):
    video_id, _ = _recording(library, run, start=5.0, end=65.0)
    annotation = _add(library, run, notes.RecordingAnchor(video_id, 60_000))
    with _conn(library) as conn:  # the file was re-finalized shorter after the note was saved
        conn.execute(
            "UPDATE video_recordings SET end_time = start_time + 30 WHERE video_id = ?", (video_id,)
        )

    resolution = _resolve(library, annotation)

    assert resolution.status == "missing" and resolution.evidence is None


@pytest.mark.parametrize("change", ["file_deleted", "failed"])
def test_recording_without_a_ready_file_resolves_missing(library, run, change):
    video_id, path = _recording(library, run)
    annotation = _add(library, run, notes.RecordingAnchor(video_id, 10_000))
    if change == "file_deleted":
        path.unlink()
    else:
        with _conn(library) as conn:
            conn.execute(
                "UPDATE video_recordings SET status = 'failed' WHERE video_id = ?", (video_id,)
            )

    assert _resolve(library, annotation).status == "missing"


def test_expired_evidence_resolves_expired_and_keeps_the_note(library, run):
    library.image("shot-1")
    step_id = library.step(run, 3, pre="shot-1")
    video_id, path = _recording(library, run)
    step_note = _add(library, run, notes.StepAnchor(step_id), body="Banner covers the button.")
    rec_note = _add(library, run, notes.RecordingAnchor(video_id, 10_000))
    with _conn(library) as conn:
        notes.tombstone_evidence(conn, run, "step", step_id)
        notes.tombstone_evidence(conn, run, "recording", video_id)
        conn.execute("DELETE FROM steps WHERE step_id = ?", (step_id,))
    path.unlink()

    assert _resolve(library, step_note).status == "expired"
    assert _resolve(library, rec_note).status == "expired"
    with _conn(library) as conn:
        kept = notes.get_annotation(conn, step_note.annotation_id)
    assert kept.body == "Banner covers the button." and kept.step_number == 3


def test_expired_wins_over_a_file_that_is_still_on_disk(library, run):
    video_id, _ = _recording(library, run)
    annotation = _add(library, run, notes.RecordingAnchor(video_id, 10_000))
    with _conn(library) as conn:
        notes.tombstone_evidence(conn, run, "recording", video_id)

    assert _resolve(library, annotation).status == "expired"


def test_expired_maps_to_evidence_expired_410_at_the_api_seam(library, run):
    video_id, _ = _recording(library, run)
    annotation = _add(library, run, notes.RecordingAnchor(video_id, 10_000))
    assert evidence_resolver.raise_for_status(_resolve(library, annotation)) is None
    with _conn(library) as conn:
        notes.tombstone_evidence(conn, run, "recording", video_id)

    with pytest.raises(AdminAPIError) as caught:
        evidence_resolver.raise_for_status(_resolve(library, annotation))

    assert caught.value.status_code == 410
    body = _envelope(caught.value)
    assert body["code"] == "evidence_expired" and body["fix"]
    assert body["docs_url"] == f"{DOCS}#evidence_expired"


# -- explicit legacy fallback ----------------------------------------------------------


def test_recording_without_a_session_clock_is_flagged_legacy_and_uncertain(library, run):
    video_id, _ = _recording(library, run, clock=False)

    annotation = _add(library, run, notes.RecordingAnchor(video_id, 42_000))
    resolution = _resolve(library, annotation)

    assert annotation.legacy_fallback is True
    with _conn(library) as conn:
        stored = conn.execute(
            "SELECT legacy_fallback FROM run_annotations WHERE annotation_id = ?",
            (annotation.annotation_id,),
        ).fetchone()[0]
    assert stored == 1
    assert resolution.status == "legacy_uncertain" and resolution.evidence is None


def test_legacy_flag_is_kept_after_the_recording_gains_a_clock(library, run):
    video_id, _ = _recording(library, run, clock=False)
    annotation = _add(library, run, notes.RecordingAnchor(video_id, 42_000))
    with _conn(library) as conn:
        conn.execute(
            "UPDATE video_recordings SET start_time = 1, end_time = 1e12 WHERE video_id = ?",
            (video_id,),
        )

    assert _resolve(library, annotation).status == "legacy_uncertain"


# -- anchor validation -----------------------------------------------------------------


def _invalid(library, sid, anchor) -> dict:
    with pytest.raises(AdminAPIError) as caught:
        _add(library, sid, anchor)
    assert caught.value.status_code == 422
    body = _envelope(caught.value)
    assert body["code"] == "annotation_anchor_invalid" and body["fix"]
    assert body["docs_url"] == f"{DOCS}#annotation_anchor_invalid"
    return body


def test_step_of_another_run_or_unknown_step_is_rejected(library, run):
    other = library.seed("other run")
    foreign_step = library.step(other, 1)

    _invalid(library, run, notes.StepAnchor(foreign_step))
    _invalid(library, run, notes.StepAnchor("stp_unknown"))
    assert _count(library) == 0


def test_recording_of_another_run_is_rejected(library, run):
    other = library.seed("other run")
    foreign, _ = _recording(library, other)

    _invalid(library, run, notes.RecordingAnchor(foreign, 10_000))
    assert _count(library) == 0


@pytest.mark.parametrize("offset_ms", [-1, 4_999, 65_001])
def test_moment_outside_the_recording_is_rejected(library, run, offset_ms):
    video_id, _ = _recording(library, run, start=5.0, end=65.0)

    _invalid(library, run, notes.RecordingAnchor(video_id, offset_ms))
    assert _count(library) == 0


@pytest.mark.parametrize("offset_ms", [5_000, 65_000])
def test_recording_bounds_are_inclusive(library, run, offset_ms):
    video_id, _ = _recording(library, run, start=5.0, end=65.0)

    assert (
        _resolve(library, _add(library, run, notes.RecordingAnchor(video_id, offset_ms))).status
        == "exact"
    )


def test_negative_moment_is_rejected_for_a_legacy_recording_too(library, run):
    video_id, _ = _recording(library, run, clock=False)

    _invalid(library, run, notes.RecordingAnchor(video_id, -1))


def test_an_anchor_to_expired_evidence_of_this_run_is_kept_as_expired(library, run):
    """Saving a note that races media expiry keeps the text; it never fails into a neighbour."""
    step_id = library.step(run, 4)
    with _conn(library) as conn:
        notes.tombstone_evidence(conn, run, "step", step_id)
        conn.execute("DELETE FROM steps WHERE step_id = ?", (step_id,))

    annotation = _add(library, run, notes.StepAnchor(step_id))

    assert _resolve(library, annotation).status == "expired"


# -- migration -------------------------------------------------------------------------


def _schema(db: Path) -> dict[str, str]:
    with sqlite3.connect(db) as conn:
        return {name: sql for name, sql in conn.execute("SELECT name, sql FROM sqlite_master")}


def test_migration_is_additive_and_records_its_revision(library):
    before = _schema(library.db)

    first = notes.migrate(library.db)
    second = notes.migrate(library.db)

    after = _schema(library.db)
    assert {name: after[name] for name in before} == before  # nothing existing changed
    assert {"schema_revisions", "run_annotations", "evidence_tombstones"} <= set(after)
    with sqlite3.connect(library.db) as conn:
        assert conn.execute(
            "SELECT revision FROM schema_revisions WHERE module = 'notes'"
        ).fetchone()[0] == len(notes.REVISIONS)
    assert first.applied == tuple(range(1, len(notes.REVISIONS) + 1)) and second.applied == ()


def test_migration_backs_up_once_including_uncheckpointed_wal(library):
    sid = library.seed("before upgrade")
    live = sqlite3.connect(library.db)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("UPDATE sessions SET initial_goal = 'only in wal' WHERE session_id = ?", (sid,))
    live.commit()

    first = notes.migrate(library.db)
    second = notes.migrate(library.db)
    live.close()

    assert first.backup_path is not None and second.backup_path is None
    with sqlite3.connect(first.backup_path) as backup:
        assert backup.execute("SELECT initial_goal FROM sessions").fetchone()[0] == "only in wal"
        assert "run_annotations" not in {
            r[0] for r in backup.execute("SELECT name FROM sqlite_master")
        }


def test_a_failed_migration_rolls_back_and_a_rerun_applies_it(library):
    good = ("CREATE TABLE probe_a (id INTEGER)",)
    broken = ("CREATE TABLE probe_b (id INTEGER)", "THIS IS NOT SQL")
    fixed = ("CREATE TABLE probe_b (id INTEGER)",)

    with pytest.raises(sqlite3.Error):
        schema_revisions.apply(library.db, "probe", [good, broken])
    assert not {"probe_a", "probe_b"} & set(_schema(library.db))
    with sqlite3.connect(library.db) as conn:
        assert (
            conn.execute("SELECT 1 FROM schema_revisions WHERE module = 'probe'").fetchone() is None
        )

    report = schema_revisions.apply(library.db, "probe", [good, fixed])
    assert report.applied == (1, 2) and {"probe_a", "probe_b"} <= set(_schema(library.db))


def test_previous_binary_works_on_the_upgraded_schema(library, run):
    """Old code paths (startup schema, catalog migrate, run purge) ignore the notes tables."""
    from apps.admin_console.services import run_purge
    from artemis.data_engine import run_catalog
    from artemis.data_engine.storage import StorageManager

    library.image("shot-1")
    step_id = library.step(run, 1, pre="shot-1")
    _add(library, run, notes.StepAnchor(step_id))
    kept = library.seed("kept run")
    kept_note = _add(library, kept, notes.StepAnchor(library.step(kept, 1)))

    StorageManager(library.db, library.traces)  # old startup bootstrap
    run_catalog.migrate(library.db)
    run_purge.purge_run(library.db, library.traces, run, vacuum=False)  # old retention delete

    assert library.count("sessions", run) == 0
    assert library.count("run_annotations", run) == 1  # the old binary leaves it behind

    notes.migrate(library.db)  # the next upgrade drops notes whose run is gone

    assert library.count("run_annotations", run) == 0
    with _conn(library) as conn:
        assert notes.get_annotation(conn, kept_note.annotation_id) is not None
