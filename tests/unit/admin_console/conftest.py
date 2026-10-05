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

"""Fixtures for the run library tests: one isolated database plus traces directory."""

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
import uuid

from fastapi import Request
from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.access_control import AccessIdentity, public_tier
from apps.admin_console.server import app

DAY = 86400.0


@dataclass
class RunLibrary:
    db: Path
    traces: Path

    @property
    def images(self) -> Path:
        return self.traces / "images"

    def seed(
        self,
        goal: str = "goal",
        *,
        status: str = "completed",
        age_days: float = 0.0,
        sid: str | None = None,
        pinned: bool = False,
    ) -> str:
        """A finished run that ended ``age_days`` ago (no end time for live statuses)."""
        sid = sid or str(uuid.uuid4())
        ended = time.time() - age_days * DAY
        live = status in ("running", "queued")
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, initial_goal, start_time, end_time, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, goal, ended - 60, None if live else ended, status),
            )
            if pinned:
                conn.execute("UPDATE run_meta SET pinned = 1 WHERE session_id = ?", (sid,))
        (self.traces / sid).mkdir(parents=True, exist_ok=True)
        return sid

    def write(self, sid: str, rel: str, text: str) -> Path:
        path = self.traces / sid / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def image(self, name: str, data: bytes = b"\xff\xd8jpeg") -> Path:
        self.images.mkdir(parents=True, exist_ok=True)
        path = self.images / f"{name}.jpg"
        path.write_bytes(data)
        return path

    def step(
        self,
        sid: str,
        number: int,
        *,
        pre: str | None = None,
        post: str | None = None,
        action: object = None,
        trace_payload: object = None,
    ) -> str:
        step_id = str(uuid.uuid4())
        with sqlite3.connect(self.db) as conn:
            for name in (pre, post):
                if name:
                    conn.execute("INSERT OR IGNORE INTO images (image_name) VALUES (?)", (name,))
            conn.execute(
                "INSERT INTO steps (step_id, session_id, step_number, timestamp, pre_image_name, "
                "post_image_name, summary, action_taken) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (step_id, sid, number, 1.0, pre, post, f"step {number}", json.dumps(action)),
            )
            if trace_payload is not None:
                conn.execute(
                    "INSERT INTO traces (trace_id, session_id, step_id, type, name, timestamp, "
                    "status, payload) VALUES (?, ?, ?, 'tool', 'type_text', 1.0, 'ok', ?)",
                    (str(uuid.uuid4()), sid, step_id, json.dumps(trace_payload)),
                )
        return step_id

    def trace(self, sid: str, payload: str) -> str:
        trace_id = str(uuid.uuid4())
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO traces (trace_id, session_id, type, name, timestamp, status, payload) "
                "VALUES (?, ?, 'llm_call', 'call', 1.0, 'ok', ?)",
                (trace_id, sid, payload),
            )
        return trace_id

    def video(self, sid: str, data: bytes = b"mp4-bytes") -> Path:
        folder = self.traces / f"task_{sid[:8]}_PASS_2026"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "recording.mp4"
        path.write_bytes(data)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO video_recordings (video_id, session_id, local_video_path, status) "
                "VALUES (?, ?, ?, 'ready')",
                (str(uuid.uuid4()), sid, str(path)),
            )
        return path

    def count(self, table: str, sid: str) -> int:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (sid,)
            ).fetchone()[0]


@pytest.fixture
def library(tmp_path, monkeypatch) -> RunLibrary:
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from apps.admin_console.database.repositories.trace_repository import trace_repo
    from artemis.data_engine.storage import StorageManager

    traces = tmp_path / "traces"
    traces.mkdir()
    db = tmp_path / "data_engine.db"
    StorageManager(db, traces)  # full current schema plus the run catalog
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", traces)
    monkeypatch.setattr(trace_repo, "db_path", db)
    return RunLibrary(db, traces)


_ROLES = {
    "admin": AccessIdentity("admin@example.com", True, "cloudflare", None),
    "qa": AccessIdentity("qa@example.com", False, "cloudflare", "not_on_allowlist"),
    "anonymous": AccessIdentity(None, False, "cloudflare", "no_jwt"),
}


def _identity_from_header(request: Request) -> AccessIdentity:
    return _ROLES[request.headers["x-test-role"]]


def make_client(role: str) -> AsyncClient:
    """In-process client acting as ``role`` (admin, qa: signed in, or anonymous)."""
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
        headers={"x-test-role": role},
    )


@pytest.fixture
def _role_identities():
    app.dependency_overrides[public_tier] = _identity_from_header
    yield
    app.dependency_overrides.pop(public_tier, None)


@pytest.fixture
def admin(_role_identities) -> AsyncClient:
    return make_client("admin")


@pytest.fixture
def qa(_role_identities) -> AsyncClient:
    return make_client("qa")


@pytest.fixture
def anonymous(_role_identities) -> AsyncClient:
    return make_client("anonymous")
