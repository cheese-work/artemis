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

"""Download leases for single media files (a video, a screenshot).

A file is leased on every run that may own it, so deleting one of those runs
while the file streams defers its cleanup (see ``run_leases``) instead of
removing the file under the reader. A file in a task folder shared by several
reruns leases all of them.
"""

from __future__ import annotations

from pathlib import Path

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

from apps.admin_console.services import run_leases, run_retention
from apps.admin_console.services.run_artifacts import (
    image_file,
    library_paths,
    recordings_under,
    relative_parts,
)


def owners_of_file(path: Path) -> list[str]:
    """Runs that may own this file under traces; [] for a file outside every run."""
    db_path, traces = library_paths()
    found = relative_parts(traces, path)
    if found is None:
        return []
    key = found[1][0]
    if key == "images":
        return owners_of_image(found[1][-1])
    owners = {owner for owner, _ in recordings_under(db_path, traces, key)}
    with db_session(db_path) as conn:
        if conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (key,)).fetchone():
            owners.add(key)  # the run's own session folder
    return sorted(owners)


def owners_of_image(name: str) -> list[str]:
    """Runs whose steps show this screenshot."""
    candidate = image_file(Path("."), name)
    if candidate is None:
        return []
    names = (candidate.stem, candidate.name)
    with db_session(library_paths()[0]) as conn:
        rows = conn.execute(
            "SELECT session_id FROM steps WHERE pre_image_name IN (?, ?) "
            "OR post_image_name IN (?, ?)",
            (*names, *names),
        ).fetchall()
    return sorted({row[0] for row in rows})


def lease(owners: list[str]) -> list[str] | None:
    """Lease every owner; None (and nothing held) when all of them are already deleted.

    Leasing first and checking after closes the race with a delete: the delete
    either sees the lease and defers, or is seen here.
    """
    db_path = library_paths()[0]
    lease_ids = [run_leases.acquire(db_path, owner) for owner in owners]
    if owners:
        marks = ",".join("?" * len(owners))
        with db_session(db_path) as conn:
            live = conn.execute(
                f"SELECT 1 FROM sessions s LEFT JOIN run_meta m ON m.session_id = s.session_id "
                f"WHERE s.session_id IN ({marks}) AND m.deleted_at IS NULL LIMIT 1",
                owners,
            ).fetchone()
        if live is None:
            release(lease_ids)
            return None
    return lease_ids


def release(lease_ids: list[str]) -> None:
    """Drop the leases and finish any cleanup that was waiting on them."""
    db_path = library_paths()[0]
    for lease_id in lease_ids:
        due = run_leases.release(db_path, lease_id)
        if due:
            run_retention.finish_cleanups_for(due)
