"""The ``prompt_critiques`` schema module: stored Improve prompt results per run."""

from contextlib import closing
from pathlib import Path
import sqlite3

from artemis.data_engine import schema_revisions

MODULE = "prompt_critiques"
REVISIONS = (
    (
        """CREATE TABLE prompt_critiques (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        requested_by TEXT,
        model TEXT NOT NULL,
        original_prompt TEXT NOT NULL,
        improved_prompt TEXT NOT NULL,
        changes_json TEXT NOT NULL,
        lessons_json TEXT NOT NULL
    )""",
        "CREATE INDEX idx_prompt_critiques_session ON prompt_critiques (session_id, created_at DESC)",
    ),
    ("ALTER TABLE prompt_critiques ADD COLUMN automatic_state TEXT",),
)


def migrate(db_path: str | Path) -> schema_revisions.RevisionReport:
    """Apply pending revisions, then drop critiques whose run is gone."""
    report = schema_revisions.apply(db_path, MODULE, REVISIONS)
    with closing(sqlite3.connect(db_path, timeout=30.0)) as conn, conn:
        conn.execute(
            "DELETE FROM prompt_critiques WHERE session_id NOT IN (SELECT session_id FROM sessions)"
        )
    return report
