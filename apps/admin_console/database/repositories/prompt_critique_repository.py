"""Redacted, immutable prompt critiques; the ``prompt_critiques`` schema module."""

import json
import time
import uuid

from apps.admin_console.core.ownership import OwnerScope, run_not_visible
from apps.admin_console.core.redaction import redact_json

# The schema lives in the data engine; re-exported for callers and tests.
from artemis.data_engine.prompt_critiques import MODULE, REVISIONS, migrate  # noqa: F401


def _present(row):
    result = dict(row)
    result["changes"] = json.loads(result.pop("changes_json"))
    result["lessons"] = json.loads(result.pop("lessons_json"))
    return result


def session(conn, session_id):
    row = conn.execute(
        "SELECT s.* FROM sessions s JOIN run_meta m USING (session_id) "
        "WHERE s.session_id = ? AND m.deleted_at IS NULL",
        (session_id,),
    ).fetchone()
    if row is None:
        raise run_not_visible()
    return dict(row)


def add(conn, session_id, *, requested_by, model, original_prompt, improvement):
    data = redact_json(
        {
            "id": uuid.uuid4().hex,
            "session_id": session_id,
            "created_at": time.time(),
            "requested_by": requested_by,
            "model": model,
            "original_prompt": original_prompt,
            **improvement,
        }
    )
    # Recheck under a write lock: a run removed while the model was running stays removed.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        session(conn, session_id)
        conn.execute(
            "INSERT INTO prompt_critiques VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                session_id,
                data["created_at"],
                data["requested_by"],
                data["model"],
                data["original_prompt"],
                data["improved_prompt"],
                json.dumps(data["changes"], ensure_ascii=False),
                json.dumps(data["lessons"], ensure_ascii=False),
            ),
        )
    return data


def newest(conn, session_id):
    session(conn, session_id)
    row = conn.execute(
        "SELECT * FROM prompt_critiques WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return _present(row) if row else None


def list_critiques(conn, scope: OwnerScope, *, q: str, limit: int, offset: int):
    where = ["m.deleted_at IS NULL"]
    params = []
    if scope.enforced and not scope.include_all:
        owners = scope.owner_emails()
        shared = sorted(scope.shared_ids) if scope.available else []
        where.append(
            f"(m.requested_by IN ({','.join('?' for _ in owners)}) OR c.session_id IN ({','.join('?' for _ in shared)}))"
        )
        params.extend([*owners, *shared])
    if q.strip():
        where.append(
            "(c.original_prompt LIKE ? OR c.improved_prompt LIKE ? OR c.changes_json LIKE ? OR c.lessons_json LIKE ?)"
        )
        params.extend([f"%{q.strip()}%"] * 4)
    rows = conn.execute(
        "SELECT c.* FROM prompt_critiques c JOIN run_meta m USING (session_id) "
        "JOIN sessions s USING (session_id) WHERE "
        + " AND ".join(where)
        + " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_present(row) for row in rows]
