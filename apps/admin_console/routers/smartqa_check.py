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

"""APK upload and APK-bound reports for `smartqa check`."""

import asyncio
import hashlib
import hmac
import os
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from apps.admin_console.core.access_control import (
    AccessIdentity,
    AdminAPIError,
    require_admin_or_service_read,
    require_qa_or_service,
)
from apps.admin_console.core.config import TEST_DATA_DIR
from apps.admin_console.core.ownership import require_access_all, smartqa_run_scope
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services.agent_qa.gate import THEMES
from apps.admin_console.services.agent_qa.report import build_report
from apps.admin_console.services.agent_qa.session import Session, load_session
from apps.admin_console.services.run_artifacts import safe_file
from artemis.config import TRACES_PATH

router = APIRouter(tags=["smartqa"])

DEFAULT_MAX_APK_BYTES = 200 * 1024 * 1024
_ZIP_MAGIC = b"PK\x03\x04"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _enabled() -> bool:
    return os.getenv("ARTEMIS_SMARTQA_CHECK", "").strip().casefold() in {"1", "true", "yes", "on"}


def _evidence_invalid() -> AdminAPIError:
    return AdminAPIError(
        422, "SmartQA evidence is invalid.", "report_evidence_invalid", "Run the check again."
    )


def _capture_path(root: Path, directory: Path, name: str) -> str:
    relative = Path(name)
    path, reason = safe_file(directory, directory / relative)
    if relative.is_absolute() or relative.suffix.lower() != ".png" or reason:
        raise _evidence_invalid()
    return (path or directory / relative).relative_to(root).as_posix()


def _bound_session(root: Path, ids: list[str], apk_sha256: str) -> Session:
    package, screens, themes = None, (), {}
    for session_id in ids:
        directory = root / session_id / "smartqa"
        evidence, reason = safe_file(root, directory / "session.json")
        if reason:
            raise _evidence_invalid()
        if evidence is None:
            raise HTTPException(404, "SmartQA evidence is not available for this run")
        recorded = load_session(evidence.parent)
        if package is None:
            package, screens = recorded.package, recorded.declared_screens
        if (
            not isinstance(package, str)
            or not package
            or not all(isinstance(screen["id"], str) for screen in screens)
            or recorded.package != package
            or recorded.declared_screens != screens
            or len(recorded.themes) != 1
        ):
            raise _evidence_invalid()
        ((theme, run),) = recorded.themes.items()
        if theme not in THEMES or theme in themes or any(step.theme != theme for step in run.steps):
            raise _evidence_invalid()
        if run.installed_apk_sha256 != apk_sha256:
            raise AdminAPIError(
                409,
                "The evidence is not for this APK.",
                "report_apk_mismatch",
                "Run the check again with the intended APK.",
            )
        themes[theme] = replace(
            run,
            steps=tuple(
                replace(
                    step,
                    before_png=_capture_path(root, directory, step.before_png),
                    after_png=_capture_path(root, directory, step.after_png),
                )
                for step in run.steps
            ),
        )
    return Session(package or "", screens, themes, base_dir=root)


@router.get("/api/smartqa/report")
def get_report(
    apk_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    session_id: Annotated[list[uuid.UUID], Query(min_length=1, max_length=len(THEMES))],
    identity: AccessIdentity = Depends(require_qa_or_service),
):
    """Evaluate private worker evidence for the exact APK and runs named by the link."""
    if not _enabled():
        raise HTTPException(404, "Not found")
    ids = [str(value) for value in session_id]
    if len(set(ids)) != len(ids):
        raise _evidence_invalid()
    require_access_all(smartqa_run_scope(identity), set(ids))
    if run_catalog_repo.live_run_ids(ids) != set(ids):
        raise HTTPException(404, "SmartQA run not found")
    try:
        session = _bound_session(Path(TRACES_PATH), ids, apk_sha256)
        report = build_report(session)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _evidence_invalid() from exc
    return JSONResponse(
        {**report, "apk_sha256": apk_sha256}, headers={"Cache-Control": "private, no-store"}
    )


@router.post("/api/smartqa/apks", dependencies=[Depends(require_admin_or_service_read)])
async def upload_apk(request: Request):
    """Store an APK by SHA-256 and return where the run can install it from."""
    if not _enabled():
        raise HTTPException(404, "Not found")
    cap = int(os.getenv("ARTEMIS_SMARTQA_APK_MAX_BYTES") or DEFAULT_MAX_APK_BYTES)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise HTTPException(413, f"APK exceeds the {cap}-byte cap")
    expected = (request.headers.get("x-content-sha256") or "").strip().lower()
    if expected and not _SHA256.match(expected):
        raise HTTPException(422, "X-Content-SHA256 must be 64 hex characters")

    directory = Path(os.getenv("ARTEMIS_SMARTQA_APK_DIR") or TEST_DATA_DIR / "smartqa_apks")
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / f".{uuid.uuid4().hex}.partial"
    digest, size, head = hashlib.sha256(), 0, b""
    try:
        with partial.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > cap:
                    raise HTTPException(413, f"APK exceeds the {cap}-byte cap")
                head = (head + chunk)[:4]
                digest.update(chunk)
                await asyncio.to_thread(out.write, chunk)
        if head != _ZIP_MAGIC:
            raise HTTPException(415, "Body is not an APK (zip) file")
        sha256 = digest.hexdigest()
        if expected and not hmac.compare_digest(expected, sha256):
            raise HTTPException(422, "X-Content-SHA256 does not match the uploaded bytes")
        final = directory / f"{sha256}.apk"
        partial.replace(final)
    finally:
        partial.unlink(missing_ok=True)
    return {"sha256": sha256, "size_bytes": size, "path": str(final)}
