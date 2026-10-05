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

import logging
from pathlib import Path

from artemis.config import DB_PATH
from artemis.data_engine.storage import StorageManager

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

from apps.admin_console.services.run_artifacts import (
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


def _stem(parts: tuple[str, ...]) -> tuple[str, ...]:
    """A recording and its converted copy (``recording.mkv`` / ``recording.mp4``) share this."""
    return (*parts[:-1], Path(parts[-1]).stem)


def _recording_targets(traces: Path, db_path, session_id: str) -> list[Path]:
    """Recording files that belong to this run alone.

    Every rerun of a named task records into the same task folder (often to the
    same file name), so only files no other run still points at are deleted,
    never the folder itself: ``purge_run`` removes a folder once it is empty.
    """
    targets: list[Path] = []
    for recorded in recorded_videos(db_path, session_id):
        found = relative_parts(traces, recorded)
        if found is None or found[1][0] == "images":
            continue
        parts = found[1]
        others = set()
        for owner, path in recordings_under(db_path, traces, parts[0]):
            other = relative_parts(traces, path)
            if owner != session_id and other is not None:
                others.add(_stem(other[1]))
        if _stem(parts) in others:
            continue  # a pinned, live or not-yet-purged sibling still uses this recording
        for suffix in (recorded.suffix, ".mp4", ".mkv"):
            target = recorded.with_suffix(suffix)
            if target not in targets:
                targets.append(target)
    return targets


def purge_run(db_path, traces: Path, session_id: str, *, vacuum: bool = True) -> None:
    """Remove a run's artifacts and rows. Safe to repeat: it only deletes what is still there."""
    images = traces / "images"
    with db_session(db_path) as conn:
        names = [row[0] for row in conn.execute(_OWN_IMAGES_SQL, {"sid": session_id})]
    for target in _recording_targets(traces, db_path, session_id):
        remove_under(traces, target, prune_empty_parents=True)
    remove_under(traces, traces / session_id)
    for name in names:
        candidate = image_file(images, name)  # None for a name that is not a plain file name
        if candidate is not None:
            remove_under(traces, candidate)

    StorageManager(db_path or DB_PATH, traces).delete_session(
        session_id, delete_files=False, vacuum=vacuum
    )
    if names:
        with db_session(db_path) as conn:
            conn.executemany("DELETE FROM images WHERE image_name = ?", [(n,) for n in names])
            conn.commit()


def compact(db_path) -> None:
    """Return freed pages to the filesystem (one VACUUM after a bulk delete)."""
    with db_session(db_path) as conn:
        conn.execute("VACUUM")
