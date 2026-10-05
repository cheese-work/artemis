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

"""Storage view: disk used by runs, run counts, free disk and its warnings."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
from typing import Any

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

from apps.admin_console.services import run_retention, run_settings
from apps.admin_console.services.run_artifacts import library_paths, resolve_manifest

disk_usage = shutil.disk_usage  # a module attribute so tests can fake a full disk

WARN_FREE_PERCENT = 20.0
CRITICAL_FREE_PERCENT = 10.0
INSUFFICIENT_STORAGE_MEMORY_SECONDS = 24 * 3600.0
_SETTING = {"last_insufficient_storage_at": None}


def note_insufficient_storage(db_path) -> None:
    """Remember a 507 (a write failed for lack of space); the view stays critical for a day."""
    run_settings.write(db_path, {"last_insufficient_storage_at": time.time()})


def directory_bytes(root: Path) -> int:
    """Bytes of regular files under ``root``, links not followed."""
    total = 0
    for folder, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue  # removed while walking
    return total


def _warnings(free_percent: float, last_507: float | None) -> list[dict[str, str]]:
    warnings = []
    if free_percent < WARN_FREE_PERCENT:
        critical = free_percent < CRITICAL_FREE_PERCENT
        warnings.append(
            {
                "level": "critical" if critical else "warning",
                "code": "low_disk",
                "message": f"Only {free_percent:.1f}% of the disk is free."
                + (" Recordings may fail." if critical else ""),
            }
        )
    if last_507 is not None and time.time() - last_507 < INSUFFICIENT_STORAGE_MEMORY_SECONDS:
        warnings.append(
            {
                "level": "critical",
                "code": "insufficient_storage",
                "message": "A write failed for lack of disk space in the last 24 hours.",
            }
        )
    return warnings


def _pinned_bytes(ids: list[str]) -> int:
    return sum(resolve_manifest(sid, None, generated=False).total_bytes for sid in ids)


def storage_report() -> dict[str, Any]:
    db_path, traces = library_paths()
    with db_session(db_path) as conn:
        run_count = conn.execute(
            "SELECT COUNT(*) FROM run_meta WHERE deleted_at IS NULL"
        ).fetchone()[0]
        pinned = [
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM run_meta WHERE deleted_at IS NULL AND pinned = 1"
            )
        ]
    disk = disk_usage(traces)
    free_percent = round(disk.free / disk.total * 100, 1) if disk.total else 100.0
    last_507 = run_settings.read(db_path, _SETTING)["last_insufficient_storage_at"]
    return {
        "usage_bytes": directory_bytes(traces),
        "run_count": run_count,
        "clearable_count": run_retention.clearable_count(),
        "pinned_count": len(pinned),
        "pinned_bytes": _pinned_bytes(pinned),
        "disk": {"total_bytes": disk.total, "free_bytes": disk.free, "free_percent": free_percent},
        "warnings": _warnings(free_percent, last_507),
        "retention": run_retention.get_settings(),
    }
