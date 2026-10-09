"""Annotation API over the notes model and evidence resolver (CHE-1465)."""

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from apps.admin_console.core.access_control import (
    AccessIdentity,
    AdminAPIError,
    _error_response,
    public_tier,
)
from apps.admin_console.core.ownership import (
    evidence_scope,
    require_visible_run,
    run_not_visible,
    scope_for,
)
from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories import annotation_repository as notes
from apps.admin_console.database.repositories.principal_repository import (
    PrincipalRepository,
    PrincipalStoreNotReady,
)
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import evidence_resolver
from apps.admin_console.services.host_registry import host_registry
from apps.admin_console.services.run_artifacts import library_paths, relative_parts, safe_file


class AnnotationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def checked(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return _error_response(
                    AdminAPIError(
                        422,
                        "Invalid annotation request.",
                        "request_invalid",
                        "Use a valid anchor, 1 to 4000 text characters, or a boolean resolved value.",
                        docs_url=f"{notes.DOCS}#request_invalid",
                    )
                )

        return checked


router = APIRouter(tags=["annotations"], route_class=AnnotationRoute)


class StepAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["step"]
    step_id: StrictStr = Field(min_length=1, max_length=200)


class RecordingAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["recording"]
    recording_id: StrictStr = Field(min_length=1, max_length=200)
    offset_ms: StrictInt = Field(le=2**63 - 1)


class AnnotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: Annotated[StepAnchor | RecordingAnchor, Field(discriminator="kind")]
    body: StrictStr = Field(min_length=1, max_length=4000)


class AnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: StrictStr = Field(default=None, min_length=1, max_length=4000)
    resolved: StrictBool = Field(default=None)


@contextmanager
def _database(session_id: str, identity: AccessIdentity):
    require_visible_run(scope_for(identity), [session_id])
    with db_session(run_catalog_repo.db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM sessions s JOIN run_meta m USING (session_id) "
            "WHERE s.session_id = ? AND m.deleted_at IS NULL",
            (session_id,),
        ).fetchone():
            raise run_not_visible()
        if (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('run_annotations', 'evidence_tombstones')"
            ).fetchone()[0]
            != 2
        ):
            raise AdminAPIError(
                503,
                "The notes store is not ready.",
                "notes_not_ready",
                "Retry shortly; restart the console so the notes migration can run.",
                retry_after=5,
                docs_url=f"{notes.DOCS}#notes_not_ready",
            )
        yield conn


def note_principal(identity: AccessIdentity) -> str:
    """Stable authorship; preview identities never reserve production email history."""
    if identity.auth_mode == "open":
        return "open:local"
    if not identity.issuer or not identity.subject:
        raise AdminAPIError(
            401,
            "A verified issuer and subject are required to write notes.",
            "actor_required",
            "Sign in through Cloudflare Access before writing notes.",
        )
    if identity.auth_mode == "preview":
        key = json.dumps([identity.issuer, identity.subject]).encode()
        return "preview:" + hashlib.sha256(key).hexdigest()
    try:
        return (
            PrincipalRepository(run_catalog_repo.db_path)
            .ensure_user(identity.issuer, identity.subject, identity.email)
            .id
        )
    except PrincipalStoreNotReady as exc:
        raise AdminAPIError(
            503,
            "The principal store is not ready.",
            "principal_store_not_ready",
            "Retry shortly; restart the console so the principal migration can run.",
            retry_after=5,
        ) from exc


def require_note_write(principal_id: str) -> None:
    """Shared per-caller budget for annotations and the forthcoming comment API."""
    if not host_registry.allow(f"notes:{principal_id}", 30, 60):
        raise AdminAPIError(
            429,
            "Too many note writes in one minute.",
            "rate_limited",
            "Wait a minute before writing another note.",
            retry_after=60,
            docs_url=f"{notes.DOCS}#rate_limited",
        )


def annotation_changed(session_id: str, annotation_id: str, change: str) -> None:
    """run.annotated hook point: no emission until a visibility-checked SSE registry exists."""


def _annotation(conn, session_id: str, annotation_id: str) -> notes.Annotation:
    annotation = notes.get_annotation(conn, annotation_id)
    if annotation is None or annotation.session_id != session_id:
        raise run_not_visible()
    return annotation


def _timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def _present(conn, annotation: notes.Annotation) -> dict:
    if annotation.anchor_kind == "step":
        anchor = {"kind": "step", "step_id": annotation.step_id}
        anchor["label"] = (
            f"Step {annotation.step_number}" if annotation.step_number is not None else "Step"
        )
    else:
        seconds = annotation.offset_ms // 1000
        anchor = {
            "kind": "recording",
            "recording_id": annotation.recording_id,
            "offset_ms": annotation.offset_ms,
            "label": f"Recording · {seconds // 60:02d}:{seconds % 60:02d}",
        }
    author = conn.execute(
        "SELECT email FROM principals WHERE id = ?", (annotation.author_principal_id,)
    ).fetchone()
    return {
        "annotation_id": annotation.annotation_id,
        "session_id": annotation.session_id,
        "author": author[0] if author else None,
        "author_principal_id": annotation.author_principal_id,
        "anchor": anchor,
        "body": annotation.body,
        "resolved": annotation.resolved,
        "created_at": _timestamp(annotation.created_at),
        "edited_at": _timestamp(annotation.edited_at),
        "deep_link": f"/runs/{quote(annotation.session_id, safe='')}?annotation={annotation.annotation_id}",
    }


def _require_author(annotation: notes.Annotation, principal_id: str) -> None:
    if annotation.author_principal_id != principal_id:
        raise AdminAPIError(
            403,
            "Only the author can edit this note's text or delete it.",
            "comment_not_permitted",
            "Add your own note, or ask the author to edit this note.",
            docs_url=f"{notes.DOCS}#comment_not_permitted",
        )


@router.get("/api/runs/{session_id}/annotations", dependencies=[Depends(evidence_scope)])
def list_annotations(session_id: str, identity: AccessIdentity = Depends(public_tier)):
    with _database(session_id, identity) as conn:
        ids = conn.execute(
            "SELECT annotation_id FROM run_annotations WHERE session_id = ? "
            "ORDER BY created_at, annotation_id",
            (session_id,),
        ).fetchall()
        return {
            "annotations": [_present(conn, _annotation(conn, session_id, row[0])) for row in ids]
        }


@router.post(
    "/api/runs/{session_id}/annotations", status_code=201, dependencies=[Depends(evidence_scope)]
)
def create_annotation(
    session_id: str, body: AnnotationCreate, identity: AccessIdentity = Depends(public_tier)
):
    with _database(session_id, identity) as conn:
        principal_id = note_principal(identity)
        require_note_write(principal_id)
        anchor = (
            notes.StepAnchor(body.anchor.step_id)
            if isinstance(body.anchor, StepAnchor)
            else notes.RecordingAnchor(body.anchor.recording_id, body.anchor.offset_ms)
        )
        annotation = notes.add_annotation(
            conn, session_id, anchor, body=body.body, author_principal_id=principal_id
        )
        result = _present(conn, annotation)
    annotation_changed(session_id, annotation.annotation_id, "created")
    return result


@router.get(
    "/api/runs/{session_id}/annotations/{annotation_id}", dependencies=[Depends(evidence_scope)]
)
def get_annotation(
    session_id: str, annotation_id: str, identity: AccessIdentity = Depends(public_tier)
):
    with _database(session_id, identity) as conn:
        return _present(conn, _annotation(conn, session_id, annotation_id))


@router.patch(
    "/api/runs/{session_id}/annotations/{annotation_id}", dependencies=[Depends(evidence_scope)]
)
def edit_annotation(
    session_id: str,
    annotation_id: str,
    body: AnnotationUpdate,
    identity: AccessIdentity = Depends(public_tier),
):
    if not body.model_fields_set:
        raise AdminAPIError(
            422, "No annotation change supplied.", "request_invalid", "Supply body or resolved."
        )
    principal_id = note_principal(identity)
    with _database(session_id, identity) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        annotation = _annotation(conn, session_id, annotation_id)
        if "body" in body.model_fields_set:
            _require_author(annotation, principal_id)
        require_note_write(principal_id)
        conn.execute(
            "UPDATE run_annotations SET body = ?, resolved = ?, edited_at = ? "
            "WHERE annotation_id = ? AND session_id = ?",
            (
                body.body if "body" in body.model_fields_set else annotation.body,
                body.resolved if "resolved" in body.model_fields_set else annotation.resolved,
                time.time(),
                annotation_id,
                session_id,
            ),
        )
        result = _present(conn, _annotation(conn, session_id, annotation_id))
    annotation_changed(
        session_id, annotation_id, "updated" if "body" in body.model_fields_set else "resolved"
    )
    return result


@router.delete(
    "/api/runs/{session_id}/annotations/{annotation_id}",
    status_code=204,
    dependencies=[Depends(evidence_scope)],
)
def delete_annotation(
    session_id: str, annotation_id: str, identity: AccessIdentity = Depends(public_tier)
):
    principal_id = note_principal(identity)
    with _database(session_id, identity) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        annotation = _annotation(conn, session_id, annotation_id)
        if not scope_for(identity).admin:
            _require_author(annotation, principal_id)
        require_note_write(principal_id)
        conn.execute(
            "DELETE FROM run_annotations WHERE annotation_id = ? AND session_id = ?",
            (annotation_id, session_id),
        )
    annotation_changed(session_id, annotation_id, "deleted")
    return Response(status_code=204)


@router.get(
    "/api/runs/{session_id}/annotations/{annotation_id}/evidence",
    dependencies=[Depends(evidence_scope)],
)
def annotation_evidence(
    session_id: str, annotation_id: str, identity: AccessIdentity = Depends(public_tier)
):
    with _database(session_id, identity) as conn:
        annotation = _annotation(conn, session_id, annotation_id)
        traces = library_paths()[1]
        resolution = evidence_resolver.resolve(conn, annotation, traces / "images")
        evidence_resolver.raise_for_status(resolution)
        evidence = dict(resolution.evidence) if resolution.evidence else None
        if evidence and annotation.anchor_kind == "step":
            evidence["image_urls"] = [
                f"/api/images/{quote(name, safe='')}" for name in evidence["image_names"]
            ]
        elif evidence:
            row, _window = notes.recording_evidence(conn, session_id, annotation.recording_id)
            path = Path(row[2])
            found = relative_parts(traces, path)
            if found is None or safe_file(traces, path)[0] is None:
                return {"status": "missing", "evidence": None}
            evidence["video_url"] = f"/videos/{quote('/'.join(found[1]), safe='/')}"
        return {"status": resolution.status, "evidence": evidence}
