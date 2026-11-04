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

"""APK upload for `smartqa check`. Off unless ARTEMIS_SMARTQA_CHECK is set."""

import asyncio
import hashlib
import hmac
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.admin_console.core.access_control import require_admin_or_service_read
from apps.admin_console.core.config import TEST_DATA_DIR

router = APIRouter(tags=["smartqa"])

DEFAULT_MAX_APK_BYTES = 200 * 1024 * 1024
_ZIP_MAGIC = b"PK\x03\x04"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _enabled() -> bool:
    return os.getenv("ARTEMIS_SMARTQA_CHECK", "").strip().casefold() in {"1", "true", "yes", "on"}


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
