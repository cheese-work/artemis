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
import os
from pathlib import Path
import shutil
import stat

from artemis.config import DB_PATH
from artemis.data_engine.storage import StorageManager

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

from apps.admin_console.services.run_artifacts import (
    image_file,
    no_symlink_parents,
    recorded_videos,
    relative_parts,
    safe_file,
)

logger = logging.getLogger(__name__)

_OWN_IMAGES_SQL = (
    "SELECT n FROM (SELECT pre_image_name AS n FROM steps WHERE session_id = :sid "
    "UNION SELECT post_image_name FROM steps WHERE session_id = :sid) WHERE n IS NOT NULL "
    "AND n NOT IN (SELECT pre_image_name FROM steps WHERE session_id != :sid "
    "AND pre_image_name IS NOT NULL UNION SELECT post_image_name FROM steps "
    "WHERE session_id != :sid AND post_image_name IS NOT NULL)"
)


def _remove(traces: Path, candidate: Path) -> None:
    """Delete a file, directory or link under ``traces``; a link is removed, never followed."""
    if not no_symlink_parents(traces, candidate):
        return  # outside storage, or reached through a link: never touch it
    try:
        mode = os.lstat(candidate).st_mode
        if stat.S_ISDIR(mode):
            shutil.rmtree(candidate)
        else:
            os.unlink(candidate)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("Could not delete %s", candidate)


def _recording_targets(traces: Path, db_path, session_id: str) -> list[Path]:
    """Recording files, or their own folder when it is a task folder directly under traces."""
    targets = []
    for recorded in recorded_videos(db_path, session_id):
        found = relative_parts(traces, recorded)
        if found is None:
            continue
        base, parts = found
        targets.append(base / parts[0] if len(parts) == 2 and parts[0] != "images" else recorded)
    return targets


def purge_run(db_path, traces: Path, session_id: str, *, vacuum: bool = True) -> None:
    """Remove a run's artifacts and rows. Safe to repeat: it only deletes what is still there."""
    images = traces / "images"
    with db_session(db_path) as conn:
        names = [row[0] for row in conn.execute(_OWN_IMAGES_SQL, {"sid": session_id})]
    for target in _recording_targets(traces, db_path, session_id):
        _remove(traces, target)
    _remove(traces, traces / session_id)
    for name in names:
        candidate = image_file(images, name)
        path = safe_file(images, candidate)[0] if candidate else None
        if path is not None:
            _remove(traces, path)

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
