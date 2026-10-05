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

"""Durable run-library settings: a small key/value table in the sessions database.

Holds the retention flag and window, the last dry-run review and the last
insufficient-storage (507) sighting. It lives beside the run catalog so a
restart, a backup and a restore keep it with the runs it governs.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

_DDL = "CREATE TABLE IF NOT EXISTS run_library_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def read(db_path, defaults: dict[str, Any]) -> dict[str, Any]:
    """Stored values over ``defaults`` (the keys of ``defaults`` are the only ones returned)."""
    with db_session(db_path) as conn:
        _ensure(conn)
        rows = dict(conn.execute("SELECT key, value FROM run_library_settings").fetchall())
    return {
        key: json.loads(rows[key]) if key in rows else default for key, default in defaults.items()
    }


def write(db_path, values: dict[str, Any]) -> None:
    with db_session(db_path) as conn:
        _ensure(conn)
        conn.executemany(
            "INSERT INTO run_library_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(key, json.dumps(value)) for key, value in values.items()],
        )
        conn.commit()
