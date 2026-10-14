"""Run annotations: the ``notes`` module tables and anchor validation (CHE-1464).

Contract: docs/board-api.md "Notes". An annotation anchors to a stable step id
or to a recording id plus session-relative milliseconds. A recording without a
session-relative clock is stored with ``legacy_fallback`` set, so it can never
resolve as exact. ``evidence_tombstones`` keeps a step's or recording's
identity after its media expires, so the note stays readable.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
import uuid

from apps.admin_console.core.access_control import AdminAPIError
from artemis.data_engine import schema_revisions

MODULE = "notes"
DOCS = "https://github.com/cheese-work/artemis/blob/main/docs/board-api.md"

REVISIONS: tuple[tuple[str, ...], ...] = (
    (
        """
CREATE TABLE run_annotations (
    annotation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    anchor_kind TEXT NOT NULL CHECK (anchor_kind IN ('step', 'recording')),
    step_id TEXT,
    step_number INTEGER,
    recording_id TEXT,
    offset_ms INTEGER,
    legacy_fallback INTEGER NOT NULL DEFAULT 0 CHECK (legacy_fallback IN (0, 1)),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    author_principal_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    edited_at REAL,
    CHECK (
        (anchor_kind = 'step' AND step_id IS NOT NULL
            AND recording_id IS NULL AND offset_ms IS NULL AND legacy_fallback = 0)
        OR (anchor_kind = 'recording' AND recording_id IS NOT NULL
            AND offset_ms IS NOT NULL AND step_id IS NULL)
    )
)""",
        "CREATE INDEX idx_run_annotations_session ON run_annotations (session_id, created_at)",
        """
CREATE TABLE evidence_tombstones (
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('step', 'recording')),
    evidence_id TEXT NOT NULL,
    expired_at REAL NOT NULL,
    PRIMARY KEY (session_id, kind, evidence_id)
)""",
    ),
)


@dataclass(frozen=True, slots=True)
class StepAnchor:
    step_id: str


@dataclass(frozen=True, slots=True)
class RecordingAnchor:
    recording_id: str
    offset_ms: int


@dataclass(frozen=True, slots=True)
class Annotation:
    annotation_id: str
    session_id: str
    anchor_kind: str
    step_id: str | None
    step_number: int | None
    recording_id: str | None
    offset_ms: int | None
    legacy_fallback: bool
    body: str
    resolved: bool
    author_principal_id: str
    created_at: float
    edited_at: float | None


def migrate(db_path: str | Path) -> schema_revisions.RevisionReport:
    """Apply pending ``notes`` revisions, then drop notes whose run is gone.

    The previous binary deletes whole runs without knowing these tables; the
    next upgrade finishes that deletion (docs/board-operations.md, "Rollback").
    """
    report = schema_revisions.apply(db_path, MODULE, REVISIONS)
    with closing(sqlite3.connect(db_path, timeout=30.0)) as conn, conn:
        for table in ("run_annotations", "evidence_tombstones"):
            conn.execute(
                f"DELETE FROM {table} WHERE session_id NOT IN (SELECT session_id FROM sessions)"
            )
    return report


def anchor_invalid(detail: str) -> AdminAPIError:
    return AdminAPIError(
        422,
        detail,
        "annotation_anchor_invalid",
        "Pick a step or a moment from this run's evidence.",
        docs_url=f"{DOCS}#annotation_anchor_invalid",
    )


def is_tombstoned(conn: sqlite3.Connection, session_id: str, kind: str, evidence_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM evidence_tombstones WHERE session_id = ? AND kind = ? AND evidence_id = ?",
            (session_id, kind, evidence_id),
        ).fetchone()
        is not None
    )


def tombstone_evidence(
    conn: sqlite3.Connection, session_id: str, kind: str, evidence_id: str
) -> None:
    """Record that this evidence's media expired; its notes then resolve ``expired``."""
    conn.execute(
        "INSERT OR IGNORE INTO evidence_tombstones (session_id, kind, evidence_id, expired_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, kind, evidence_id, time.time()),
    )
    conn.commit()


def recording_evidence(conn: sqlite3.Connection, session_id: str, recording_id: str):
    """The recording row and its session-relative ``(start_ms, end_ms)``.

    ``(None, None)`` for a recording of another run; a window of ``None`` when
    the recording has no session-relative clock. ``end_ms`` is ``None`` while
    the recording has no end time.
    """
    row = conn.execute(
        "SELECT v.start_time, v.end_time, v.local_video_path, v.status, "
        "s.start_time AS session_start FROM video_recordings v "
        "JOIN sessions s ON s.session_id = v.session_id "
        "WHERE v.video_id = ? AND v.session_id = ?",
        (recording_id, session_id),
    ).fetchone()
    if row is None:
        return None, None
    start, end, _path, _status, session_start = row
    if start is None or session_start is None:
        return row, None
    end_ms = None if end is None else round((end - session_start) * 1000)
    return row, (round((start - session_start) * 1000), end_ms)


def _validate(conn: sqlite3.Connection, session_id: str, anchor) -> tuple[int | None, bool]:
    """(step_number, legacy_fallback) for a valid anchor; raises ``annotation_anchor_invalid``."""
    if isinstance(anchor, StepAnchor):
        row = conn.execute(
            "SELECT step_number FROM steps WHERE step_id = ? AND session_id = ?",
            (anchor.step_id, session_id),
        ).fetchone()
        if row is not None:
            return row[0], False
        if is_tombstoned(conn, session_id, "step", anchor.step_id):
            return None, False
        raise anchor_invalid("step_id: not a step of this run.")
    if anchor.offset_ms < 0:
        raise anchor_invalid("offset_ms: outside the recording.")
    row, window = recording_evidence(conn, session_id, anchor.recording_id)
    if row is None:
        if is_tombstoned(conn, session_id, "recording", anchor.recording_id):
            return None, False
        raise anchor_invalid("recording_id: not a recording of this run.")
    if window is None:
        return None, True
    start_ms, end_ms = window
    if anchor.offset_ms < start_ms or (end_ms is not None and anchor.offset_ms > end_ms):
        raise anchor_invalid("offset_ms: outside the recording.")
    return None, False


def add_annotation(
    conn: sqlite3.Connection,
    session_id: str,
    anchor: StepAnchor | RecordingAnchor,
    *,
    body: str,
    author_principal_id: str,
) -> Annotation:
    """Validate the anchor against this run's evidence and store the note in one transaction."""
    with conn:  # validation and insert commit together, or not at all
        conn.execute("BEGIN IMMEDIATE")
        step_number, legacy = _validate(conn, session_id, anchor)
        annotation = Annotation(
            annotation_id=f"ann_{uuid.uuid4().hex}",
            session_id=session_id,
            anchor_kind="step" if isinstance(anchor, StepAnchor) else "recording",
            step_id=getattr(anchor, "step_id", None),
            step_number=step_number,
            recording_id=getattr(anchor, "recording_id", None),
            offset_ms=getattr(anchor, "offset_ms", None),
            legacy_fallback=legacy,
            body=body,
            resolved=False,
            author_principal_id=author_principal_id,
            created_at=time.time(),
            edited_at=None,
        )
        conn.execute(
            "INSERT INTO run_annotations (annotation_id, session_id, anchor_kind, step_id, "
            "step_number, recording_id, offset_ms, legacy_fallback, body, resolved, "
            "author_principal_id, created_at, edited_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                annotation.annotation_id,
                annotation.session_id,
                annotation.anchor_kind,
                annotation.step_id,
                annotation.step_number,
                annotation.recording_id,
                annotation.offset_ms,
                int(annotation.legacy_fallback),
                annotation.body,
                int(annotation.resolved),
                annotation.author_principal_id,
                annotation.created_at,
                annotation.edited_at,
            ),
        )
    return annotation


def get_annotation(conn: sqlite3.Connection, annotation_id: str) -> Annotation | None:
    row = conn.execute(
        "SELECT annotation_id, session_id, anchor_kind, step_id, step_number, recording_id, "
        "offset_ms, legacy_fallback, body, resolved, author_principal_id, created_at, edited_at "
        "FROM run_annotations WHERE annotation_id = ?",
        (annotation_id,),
    ).fetchone()
    if row is None:
        return None
    values = list(row)
    values[7], values[9] = bool(values[7]), bool(values[9])
    return Annotation(*values)
