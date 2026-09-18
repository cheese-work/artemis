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

"""Read-only access to a session's native ``llm_usage`` trace receipts.

Gate 1 reconciliation (:mod:`artemis.config.attempt_reconciliation`) needs the
raw ``llm_usage`` payloads :func:`artemis.services.token_meter.record_llm_usage`
already writes into the DataEngine SQLite store during a live run. That store
only exposes trace lookup scoped to a step
(:meth:`artemis.data_engine.storage.StorageManager.get_steps_with_traces`),
but ``llm_usage`` traces are not guaranteed to carry a ``step_id`` (background
lens/utility calls may record with ``step_id=None``); walking steps would
silently miss those. This module queries the ``traces`` table directly by
``session_id``, following the same read-only-offline-reader pattern already
established by :class:`artemis.data_engine.history_reader.OfflineHistoryReader`
(``StorageManager(db_path, traces_dir, read_only=True)``), without touching
any write path or the live in-process engine.

Never mutates, summarizes, or drops rows: every ``llm_usage`` payload found
for the session is returned as-is, so reconciliation always sees the original
receipts rather than a derived view.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from artemis.data_engine.storage import StorageManager


def read_llm_usage_events(
    db_path: str | Path,
    traces_dir: str | Path,
    session_id: str,
) -> list[dict[str, Any]]:
    """Returns every stored ``llm_usage`` trace payload for ``session_id``.

    Read-only: opens the DataEngine database with ``read_only=True`` (refuses
    to create a missing database or table, matching
    :class:`~artemis.data_engine.history_reader.OfflineHistoryReader`). Rows
    with unparsable JSON payloads are skipped rather than raising, since a
    single corrupt trace row must not hide every other attempt's evidence;
    the caller (reconciliation) already treats a missing/absent event as
    ``not_invoked``/``unverified_identity`` rather than inferring identity.

    Returns an empty list, never raises, if the database does not exist yet
    (e.g. a session that recorded no traces at all).
    """
    resolved_db_path = Path(db_path)
    if not resolved_db_path.exists():
        return []

    storage = StorageManager(resolved_db_path, traces_dir, read_only=True)
    events: list[dict[str, Any]] = []
    with storage._get_connection() as conn:
        cursor = conn.execute(
            "SELECT payload FROM traces WHERE session_id = ? AND name = 'llm_usage' "
            "ORDER BY timestamp ASC",
            (str(session_id),),
        )
        for row in cursor.fetchall():
            raw_payload = row["payload"]
            if not raw_payload:
                continue
            try:
                events.append(json.loads(raw_payload))
            except (json.JSONDecodeError, TypeError):
                continue
    return events
