import asyncio
import json
import logging
import sqlite3
import time

from apps.admin_console.core.redaction import redact_json, redact_text
from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories import prompt_critique_repository as critiques
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import prompt_improvement

logger = logging.getLogger(__name__)


def _claim(session_id):
    with db_session(run_catalog_repo.db_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        session = critiques.session(connection, session_id)
        if session["status"] != "completed":
            return None
        cursor = connection.execute(
            """INSERT OR IGNORE INTO prompt_critiques
            (id,session_id,created_at,model,original_prompt,improved_prompt,changes_json,lessons_json,automatic_state)
            VALUES (?,?,?,'server default (pending)',?,'','[]','[]','generating')""",
            (
                f"automatic:{session_id}",
                session_id,
                time.time(),
                redact_text(session.get("initial_goal") or ""),
            ),
        )
        if not cursor.rowcount:
            return None
        steps = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM steps WHERE session_id=? ORDER BY step_number", (session_id,)
            )
        ]
        return session, steps


def _settle(session_id, state, model=None, improvement=None):
    with db_session(run_catalog_repo.db_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        session = critiques.session(connection, session_id)
        if session["status"] != "completed":
            state, improvement = "failed", None
        if improvement is not None:
            data = redact_json(improvement.model_dump())
            connection.execute(
                "UPDATE prompt_critiques SET model=?, improved_prompt=?, changes_json=?, lessons_json=?, automatic_state=? WHERE id=?",
                (
                    model,
                    data["improved_prompt"],
                    json.dumps(data["changes"]),
                    json.dumps(data["lessons"]),
                    state,
                    f"automatic:{session_id}",
                ),
            )
        else:
            connection.execute(
                "UPDATE prompt_critiques SET automatic_state=? WHERE id=?",
                (state, f"automatic:{session_id}"),
            )


async def ensure_tip(session_id):
    state, selected_model, result = "failed", None, None
    claimed = None
    try:
        claimed = _claim(session_id)
        if claimed is None:
            return
        session, steps = claimed
        outcome = {}
        try:
            loaded = json.loads(
                (run_catalog_repo.traces_dir / session_id / "run_outcome.json").read_text(
                    encoding="utf-8"
                )
            )
            if isinstance(loaded, dict):
                outcome = loaded
        except (OSError, ValueError, UnicodeError):
            pass
        generated = (
            await asyncio.gather(
                prompt_improvement.generate(
                    prompt_improvement.build_input(session, steps, outcome)
                ),
                return_exceptions=True,
            )
        )[0]
        if not isinstance(generated, BaseException):
            selected_model, result = generated
            state = "complete"
        else:
            logger.warning("Automatic prompt tip unavailable for session %s", session_id)
    except (AdminAPIError, OSError, ValueError, sqlite3.Error):
        logger.warning("Automatic prompt tip unavailable for session %s", session_id)
    finally:
        try:
            if claimed is not None:
                _settle(session_id, state, selected_model, result)
        except (AdminAPIError, OSError, ValueError, sqlite3.Error):
            logger.warning("Automatic prompt tip could not be saved for session %s", session_id)


def read_tip(session_id):
    try:
        with db_session(run_catalog_repo.db_path) as connection:
            row = connection.execute(
                """SELECT c.automatic_state,c.improved_prompt,c.created_at FROM prompt_critiques c
                JOIN sessions s USING (session_id) JOIN run_meta m USING (session_id)
                WHERE c.id=? AND s.status='completed' AND m.deleted_at IS NULL""",
                (f"automatic:{session_id}",),
            ).fetchone()
            if row is None:
                return None
            state = row["automatic_state"]
            if (
                state == "generating"
                and time.time() - row["created_at"] > prompt_improvement.TIMEOUT_SECONDS + 5
            ):
                state = "failed"
            return {
                "state": state,
                "text": redact_text(row["improved_prompt"]) if state == "complete" else None,
            }
    except sqlite3.Error:
        return None
