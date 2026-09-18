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

"""Read-only ``llm_usage`` receipt lookup, direct from the DataEngine DB.

All tests write into a ``tmp_path`` SQLite file via a writable
``StorageManager`` (never the real traces directory), then read it back
through :func:`read_llm_usage_events` exactly as the read-only offline path
would.
"""

from __future__ import annotations

import json

from artemis.config.attempt_usage_reader import read_llm_usage_events
from artemis.data_engine.models import SessionMetadata, TraceRecord
from artemis.data_engine.storage import StorageManager


def _seeded_storage(tmp_path, session_id: str) -> tuple[StorageManager, str, str]:
    db_path = tmp_path / "data_engine.db"
    traces_dir = tmp_path / "traces"
    storage = StorageManager(db_path, traces_dir)
    storage.create_session(SessionMetadata(session_id=session_id, initial_goal="test goal"))
    return storage, str(db_path), str(traces_dir)


class TestReadLlmUsageEvents:
    def test_returns_only_llm_usage_rows_for_the_given_session(self, tmp_path):
        storage, db_path, traces_dir = _seeded_storage(tmp_path, "session-a")
        storage.create_trace(
            TraceRecord(
                trace_id="trace-1",
                session_id="session-a",
                type="llm_call",
                name="llm_usage",
                payload={"node": "planner", "source": "openai:gpt-5.6-sol", "prompt_tokens": 10},
            )
        )
        storage.create_trace(
            TraceRecord(
                trace_id="trace-2",
                session_id="session-a",
                type="tool",
                name="ask_explorer",
                payload={"args": {}, "result": "irrelevant, not an llm_usage trace"},
            )
        )
        # A different session's llm_usage row must never leak into session-a's read.
        storage.create_session(SessionMetadata(session_id="session-b", initial_goal="other"))
        storage.create_trace(
            TraceRecord(
                trace_id="trace-3",
                session_id="session-b",
                type="llm_call",
                name="llm_usage",
                payload={"node": "planner", "source": "google:gemini-3.5-flash-lite"},
            )
        )

        events = read_llm_usage_events(db_path, traces_dir, "session-a")

        assert len(events) == 1
        assert events[0]["node"] == "planner"
        assert events[0]["source"] == "openai:gpt-5.6-sol"

    def test_events_with_no_step_id_are_still_found(self, tmp_path):
        """The whole point of querying `traces` directly instead of walking
        steps: a background/utility llm_usage call recorded with no step_id
        must not be silently missed."""
        storage, db_path, traces_dir = _seeded_storage(tmp_path, "session-a")
        storage.create_trace(
            TraceRecord(
                trace_id="trace-1",
                session_id="session-a",
                step_id=None,
                type="llm_call",
                name="llm_usage",
                payload={"node": "hopper", "source": "openai:gpt-5.6-sol"},
            )
        )

        events = read_llm_usage_events(db_path, traces_dir, "session-a")

        assert len(events) == 1
        assert events[0]["node"] == "hopper"

    def test_missing_database_returns_empty_list_not_an_error(self, tmp_path):
        events = read_llm_usage_events(
            tmp_path / "does-not-exist.db", tmp_path / "traces", "session-a"
        )
        assert events == []

    def test_corrupt_payload_row_is_skipped_not_raised(self, tmp_path):
        storage, db_path, traces_dir = _seeded_storage(tmp_path, "session-a")
        storage.create_trace(
            TraceRecord(
                trace_id="trace-1",
                session_id="session-a",
                type="llm_call",
                name="llm_usage",
                payload={"node": "planner", "source": "openai:gpt-5.6-sol"},
            )
        )
        # Directly corrupt one row's payload to simulate a partially-written
        # or truncated record, bypassing the Pydantic-validated write path.
        with storage._get_connection() as conn:
            conn.execute(
                "INSERT INTO traces (trace_id, session_id, type, name, timestamp, status, payload) "
                "VALUES ('trace-2', 'session-a', 'llm_call', 'llm_usage', 2.0, 'success', ?)",
                ("{not valid json",),
            )
            conn.commit()

        events = read_llm_usage_events(db_path, traces_dir, "session-a")

        assert len(events) == 1
        assert events[0]["node"] == "planner"

    def test_returned_payload_matches_original_bytes_exactly(self, tmp_path):
        """Reconciliation must see the original receipt, not a derived summary."""
        storage, db_path, traces_dir = _seeded_storage(tmp_path, "session-a")
        original_payload = {
            "node": "operator",
            "source": "openai:gpt-5.6-sol",
            "prompt_tokens": 123,
            "completion_tokens": 45,
            "session_cached_ratio": 0.5,
        }
        storage.create_trace(
            TraceRecord(
                trace_id="trace-1",
                session_id="session-a",
                type="llm_call",
                name="llm_usage",
                payload=original_payload,
            )
        )

        events = read_llm_usage_events(db_path, traces_dir, "session-a")

        assert len(events) == 1
        assert events[0] == json.loads(json.dumps(original_payload))
