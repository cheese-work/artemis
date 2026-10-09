"""Resolve an annotation anchor to its evidence (CHE-1464; docs/board-api.md "Notes").

Four outcomes: ``exact``, ``missing``, ``expired``, ``legacy_uncertain``. Only
``exact`` carries an evidence reference, and only for the anchored step or the
anchored moment of the anchored recording: the resolver never snaps to a
neighbouring step or frame. ``expired`` keeps the note readable; the API seam
answers ``evidence_expired`` 410 for its media.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Literal

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.database.repositories import annotation_repository as notes
from apps.admin_console.services.run_artifacts import image_file

Status = Literal["exact", "missing", "expired", "legacy_uncertain"]


@dataclass(frozen=True, slots=True)
class Resolution:
    status: Status
    evidence: dict | None = None


_MISSING = Resolution("missing")


def _step(conn: sqlite3.Connection, a: notes.Annotation, images: Path) -> Resolution:
    row = conn.execute(
        "SELECT pre_image_name, post_image_name FROM steps WHERE step_id = ? AND session_id = ?",
        (a.step_id, a.session_id),
    ).fetchone()
    if row is None:
        return _MISSING
    names = [n for n in row if n and (path := image_file(images, n)) is not None and path.is_file()]
    if not names:
        return _MISSING
    return Resolution("exact", {"kind": "step", "step_id": a.step_id, "image_names": names})


def _recording(conn: sqlite3.Connection, a: notes.Annotation) -> Resolution:
    row, window = notes.recording_evidence(conn, a.session_id, a.recording_id)
    if row is None or row[3] != "ready" or not row[2] or not Path(row[2]).is_file():
        return _MISSING
    if a.legacy_fallback:
        return Resolution("legacy_uncertain")
    if window is None:  # the clock this note was saved against is gone: no safe position
        return _MISSING
    start_ms, end_ms = window
    if a.offset_ms < start_ms or end_ms is None or a.offset_ms > end_ms:
        return _MISSING
    return Resolution(
        "exact",
        {
            "kind": "recording",
            "recording_id": a.recording_id,
            "position_ms": a.offset_ms - start_ms,
        },
    )


def resolve(conn: sqlite3.Connection, annotation: notes.Annotation, images: Path) -> Resolution:
    evidence_id = (
        annotation.step_id if annotation.anchor_kind == "step" else annotation.recording_id
    )
    if notes.is_tombstoned(conn, annotation.session_id, annotation.anchor_kind, evidence_id):
        return Resolution("expired")
    if annotation.anchor_kind == "step":
        return _step(conn, annotation, images)
    return _recording(conn, annotation)


def raise_for_status(resolution: Resolution) -> None:
    """The API seam: expired media answers ``evidence_expired`` 410; other outcomes are bodies."""
    if resolution.status == "expired":
        raise AdminAPIError(
            410,
            "The recording or screenshot for this note passed retention. The note is kept.",
            "evidence_expired",
            "None for the media; read the note text and the step list.",
            docs_url=f"{notes.DOCS}#evidence_expired",
        )
