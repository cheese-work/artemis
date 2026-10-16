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

"""Deployed commit and deploy time, written by the deploy host (CHE-1146).

The deploy script writes ``DEPLOYED_SHA`` at the repo root:
line 1 is the git sha, optional line 2 is the ISO-8601 UTC deploy time.
``ARTEMIS_DEPLOYED_SHA`` / ``ARTEMIS_DEPLOYED_AT`` take precedence, and
``ARTEMIS_DEPLOYED_SHA_FILE`` moves the file. Anything missing or malformed
reads as unknown: a version badge must never break the app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC
import os
from pathlib import Path
import re
from typing import Any

_SHA = re.compile(r"[0-9a-f]{7,40}")
_DEFAULT_FILE = Path(__file__).resolve().parents[3] / "DEPLOYED_SHA"
_UNKNOWN: dict[str, Any] = {
    "status": "unknown",
    "sha": None,
    "short_sha": None,
    "deployed_at": None,
    "build": None,
}


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return _utc_text(parsed)
    except (ValueError, OverflowError):
        return None


def _build_stamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone(timedelta(hours=7))
        )
        return f"{moment.year:04d}{moment:%m%d-%H%M}"
    except (ValueError, OverflowError):
        return None


def read_deploy_version() -> dict[str, Any]:
    path = Path(os.getenv("ARTEMIS_DEPLOYED_SHA_FILE") or _DEFAULT_FILE)
    sha = os.getenv("ARTEMIS_DEPLOYED_SHA")
    when = os.getenv("ARTEMIS_DEPLOYED_AT")
    mtime: str | None = None
    if not sha:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            mtime = _utc_text(datetime.fromtimestamp(path.stat().st_mtime, UTC))
        except (OSError, UnicodeDecodeError):
            return dict(_UNKNOWN)
        sha = lines[0] if lines else ""
        when = when or (lines[1] if len(lines) > 1 else None)

    sha = sha.strip().lower()
    if not _SHA.fullmatch(sha):
        return dict(_UNKNOWN)
    deployed_at = _normalize_time(when) or mtime
    return {
        "status": "known",
        "sha": sha,
        "short_sha": sha[:7],
        "deployed_at": deployed_at,
        "build": _build_stamp(deployed_at),
    }
