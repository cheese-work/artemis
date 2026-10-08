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

import json
import traceback
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from artemis.config import TEST_OUTPUTS_DIR

from apps.admin_console.core.ownership import OwnerScope, actor_scope, present_session_data
from apps.admin_console.core.redaction import redact_json, redact_text

try:
    from admin_console.database.repositories.step_repository import step_repo
    from admin_console.database.repositories.trace_repository import trace_repo
    from admin_console.services.media_service import media_service
except ImportError:
    from apps.admin_console.database.repositories.step_repository import step_repo
    from apps.admin_console.database.repositories.trace_repository import trace_repo
    from apps.admin_console.services.media_service import media_service


router = APIRouter(tags=["steps"])


@router.get("/api/sessions/{session_id}/steps")
async def get_session_steps(
    session_id: str, client: str | None = None, actor: OwnerScope = Depends(actor_scope)
):
    try:
        steps = step_repo.get_session_steps(session_id, client=client)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return present_session_data(actor, session_id, steps)


@router.get("/api/steps/{step_id}/traces")
async def get_step_traces_endpoint(step_id: str, actor: OwnerScope = Depends(actor_scope)):
    try:
        session_id = step_repo.get_step_session_id(step_id)
        if not session_id:
            raise HTTPException(status_code=404, detail="Step not found")
        traces = trace_repo.get_step_traces_tree(session_id, step_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return present_session_data(actor, session_id, traces)


def _resolve_trace_db_path(session_id: str | None, step_number: int | None):
    if session_id and step_number is not None:
        sandbox_db_path = (
            TEST_OUTPUTS_DIR
            / f"{session_id}_step_{step_number:02d}"
            / "temp_traces"
            / "data_engine.db"
        )
        if sandbox_db_path.exists():
            return sandbox_db_path
    return None


@router.get("/api/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    session_id: str = None,
    step_number: int = None,
    actor: OwnerScope = Depends(actor_scope),
):
    try:
        db_path = _resolve_trace_db_path(session_id, step_number)
        trace_dict = trace_repo.get_trace_by_id(trace_id, db_path=db_path)
        if not trace_dict:
            raise HTTPException(status_code=404, detail="Trace not found")

        if trace_dict.get("payload"):
            try:
                payload_obj = json.loads(trace_dict["payload"])
                trace_dict["payload"] = media_service.unwrap_payload(payload_obj)
            except (ValueError, TypeError, KeyError, AttributeError):
                # Non-JSON or unexpectedly shaped payload: serve it raw.
                pass

    except HTTPException as e:
        raise e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    return present_session_data(actor, trace_dict.get("session_id"), trace_dict)


def _redacted_payload(payload: str) -> str:
    """A downloaded trace leaves the server: secrets are removed, JSON stays JSON."""
    try:
        return json.dumps(redact_json(json.loads(payload)), ensure_ascii=False)
    except (ValueError, TypeError):
        return redact_text(str(payload))


@router.get("/api/traces/{trace_id}/download")
async def download_trace(trace_id: str, session_id: str = None, step_number: int = None):
    try:
        db_path = _resolve_trace_db_path(session_id, step_number)
        trace_dict = trace_repo.get_trace_by_id(trace_id, db_path=db_path)
        if not trace_dict or not trace_dict.get("payload"):
            raise HTTPException(status_code=404, detail="Payload not found")

        return PlainTextResponse(
            _redacted_payload(trace_dict["payload"]),
            headers={"Content-Disposition": f'attachment; filename="trace_{trace_id}.json"'},
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
