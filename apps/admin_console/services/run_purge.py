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

"""Physical deletion of one run: files named by its manifest, then its database rows."""

from __future__ import annotations

import errno
import logging
from pathlib import Path

from artemis.config import DB_PATH
from artemis.data_engine.storage import StorageManager

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

from apps.admin_console.services import failure_ledger
from apps.admin_console.services.run_artifacts import (
    RemovalFailed,
    image_file,
    recorded_videos,
    relative_parts,
    recordings_under,
    remove_under,
)

logger = logging.getLogger(__name__)

_OWN_IMAGES_SQL = (
    "SELECT n FROM (SELECT pre_image_name AS n FROM steps WHERE session_id = :sid "
    "UNION SELECT post_image_name FROM steps WHERE session_id = :sid) WHERE n IS NOT NULL "
    "AND n NOT IN (SELECT pre_image_name FROM steps WHERE session_id != :sid "
    "AND pre_image_name IS NOT NULL UNION SELECT post_image_name FROM steps "
    "WHERE session_id != :sid AND post_image_name IS NOT NULL)"
)


def _variants(parts: tuple[str, ...]) -> set[tuple[str, ...]]:
    """A recording and its converted copy (``recording.mkv`` / ``recording.mp4``)."""
    stem = Path(parts[-1]).stem
    return {(*parts[:-1], stem + suffix) for suffix in (Path(parts[-1]).suffix, ".mp4", ".mkv")}


def _others_recordings(
    traces: Path, db_path, session_id: str, key: str
) -> frozenset[tuple[str, ...]]:
    """Recording files under ``traces/<key>`` that any other run still points at.

    Every rerun of a named task records into the same task folder, and a task
    name can even equal another run's id, so a folder is never owned by one run:
    only files no other run (pinned, live or not yet purged) names are deleted.
    """
    kept: set[tuple[str, ...]] = set()
    for owner, path in recordings_under(db_path, traces, key):
        found = relative_parts(traces, path)
        if owner != session_id and found is not None:
            kept |= _variants(found[1])
    return frozenset(kept)


def _recording_targets(traces: Path, db_path, session_id: str) -> list[Path]:
    """This run's recording files that no other run uses."""
    targets: list[Path] = []
    for recorded in recorded_videos(db_path, session_id):
        found = relative_parts(traces, recorded)
        if found is None or found[1][0] == "images":
            continue
        kept = _others_recordings(traces, db_path, session_id, found[1][0])
        for suffix in (recorded.suffix, ".mp4", ".mkv"):
            target = recorded.with_suffix(suffix)
            rel = relative_parts(traces, target)
            if rel is not None and rel[1] not in kept and target not in targets:
                targets.append(target)
    return targets


def purge_run(db_path, traces: Path, session_id: str, *, vacuum: bool = True) -> None:
    """Remove a run's artifacts and rows. Safe to repeat: it only deletes what is still there."""
    images = traces / "images"
    with db_session(db_path) as conn:
        names = [row[0] for row in conn.execute(_OWN_IMAGES_SQL, {"sid": session_id})]
    failures: list[str] = []

    def remove(path: Path, **kwargs) -> None:
        try:
            remove_under(traces, path, **kwargs)
        except RemovalFailed as exc:
            failures.append(str(exc))

    for target in _recording_targets(traces, db_path, session_id):
        remove(target, prune_empty_parents=True)
    # The run's own folder holds its logs, but another run's task name may equal this id.
    remove(traces / session_id, keep=_others_recordings(traces, db_path, session_id, session_id))
    for name in names:
        candidate = image_file(images, name)  # None for a name that is not a plain file name
        if candidate is not None:
            remove(candidate)
    if failures:  # rows stay, so the file names survive and the cleanup is retried
        raise RemovalFailed(errno.EIO, f"{len(failures)} deletion(s) failed: {failures[0]}")

    StorageManager(db_path or DB_PATH, traces).delete_session(
        session_id, delete_files=False, vacuum=vacuum
    )
    with db_session(db_path) as conn:
        if names:
            conn.executemany("DELETE FROM images WHERE image_name = ?", [(n,) for n in names])
        failure_ledger.forget(conn, session_id)
        conn.commit()


def compact(db_path) -> None:
    """Return freed pages to the filesystem (one VACUUM after a bulk delete)."""
    with db_session(db_path) as conn:
        conn.execute("VACUUM")
