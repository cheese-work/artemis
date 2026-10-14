"""On-click improvement and the searchable prompt lessons library."""

import asyncio
import json

from fastapi import APIRouter, Depends, Query

from apps.admin_console.core.access_control import AdminAPIError, require_qa
from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    evidence_scope,
    list_scope,
    present_session_data,
    require_access,
    require_actor,
    require_catalog_ready,
    require_signed_in,
)
from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories import prompt_critique_repository as repo
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import prompt_improvement
from apps.admin_console.services.run_retention import TERMINAL

router = APIRouter(tags=["prompt lessons"])
_in_flight: set[str] = set()


def _read_input(session_id, actor):
    require_catalog_ready()
    with db_session(run_catalog_repo.db_path) as conn:
        session = repo.session(conn, session_id)
        require_access(require_actor(actor), session_id)
        if session["status"] not in TERMINAL:
            raise AdminAPIError(
                409,
                "Finish the run before improving its prompt.",
                "run_active",
                "Wait for the run to finish.",
            )
        steps = [
            dict(row)
            for row in conn.execute(
                "SELECT step_number, action_taken, last_execution_result, extra_metadata FROM steps WHERE session_id = ? ORDER BY step_number",
                (session_id,),
            )
        ]
    outcome = {}
    path = run_catalog_repo.traces_dir / session_id / "run_outcome.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            outcome = loaded
    except (OSError, ValueError):
        pass
    return session, prompt_improvement.build_input(session, steps, outcome)


@router.post("/api/sessions/{session_id}/improve-prompt", dependencies=[Depends(require_qa)])
async def improve_prompt(session_id: str, actor: OwnerScope = Depends(actor_scope)):
    actor = require_actor(actor)
    session, input_text = await asyncio.to_thread(_read_input, session_id, actor)
    if session_id in _in_flight:
        raise AdminAPIError(
            409,
            "This prompt is already being improved.",
            "improve_in_flight",
            "Wait for the current improvement.",
        )
    _in_flight.add(session_id)
    try:
        model, improvement = await prompt_improvement.generate(input_text)

        def store():
            with db_session(run_catalog_repo.db_path) as conn:
                return repo.add(
                    conn,
                    session_id,
                    requested_by=actor.principal_id or actor.email,
                    model=model,
                    original_prompt=session["initial_goal"],
                    improvement=improvement.model_dump(),
                )

        return await asyncio.to_thread(store)
    finally:
        _in_flight.discard(session_id)


@router.get("/api/sessions/{session_id}/prompt-critique")
async def get_prompt_critique(session_id: str, scope: OwnerScope = Depends(evidence_scope)):
    def read():
        with db_session(run_catalog_repo.db_path) as conn:
            result = repo.newest(conn, session_id)
        if result is None:
            raise AdminAPIError(
                404,
                "No prompt critique yet.",
                "prompt_critique_not_found",
                "Click Improve prompt after the run finishes.",
            )
        return present_session_data(scope, session_id, result)

    return await asyncio.to_thread(read)


@router.get("/api/prompt-lessons")
async def prompt_lessons(
    q: str = Query("", max_length=1000),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: OwnerScope = Depends(list_scope),
):
    scope = require_actor(scope)
    require_signed_in(scope)

    def read():
        require_catalog_ready()
        with db_session(run_catalog_repo.db_path) as conn:
            rows = repo.list_critiques(conn, scope, q=q, limit=limit + 1, offset=offset)
        return {
            "critiques": [
                present_session_data(scope, row["session_id"], row) for row in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    return await asyncio.to_thread(read)
