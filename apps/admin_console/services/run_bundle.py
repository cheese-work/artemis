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

"""Run bundles: one zip of a run's prompt, steps, images, video and logs.

Built to a temp file (so the download has a Content-Length and a failure never
leaves half a response), capped, and limited to two builds at a time per server.
Text artifacts are redacted on the way in; media is copied untouched. A
download holds a lease on its run from the moment it starts until the temp file
is gone, see ``run_leases``.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import BinaryIO
import zipfile

from apps.admin_console.core.redaction import redact_json, redact_text

try:
    from admin_console.database.repositories.run_catalog_repository import (
        CatalogNotReady,
        run_catalog_repo,
    )
except ImportError:
    from apps.admin_console.database.repositories.run_catalog_repository import (
        CatalogNotReady,
        run_catalog_repo,
    )

from apps.admin_console.services import run_leases, run_retention, run_storage
from apps.admin_console.services.run_artifacts import (
    Artifact,
    Manifest,
    RunLibraryError,
    UnsafePath,
    library_paths,
    open_under,
    resolve_manifest,
)

MAX_BUNDLE_BYTES = 1 << 30  # 1 GiB of uncompressed content
MAX_CONCURRENT_BUILDS = 2
RETRY_AFTER_SECONDS = 5
_TEXT_LIMIT = 64 << 20  # a log longer than this is cut, with a marker
_CHUNK = 1 << 20
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)


class BundleError(RunLibraryError):
    def __init__(self, status: int, code: str, *, retry_after: int | None = None, **extra: object):
        super().__init__(status, code, **extra)
        self.retry_after = retry_after


@dataclass(slots=True)
class BundleFile:
    session_id: str
    path: Path
    size: int
    entries: int
    skipped: int
    lease_id: str
    started: float

    def finish(self) -> None:
        """Delete the temp file, release the lease, and finish a cleanup that waited on it."""
        try:
            self.path.unlink(missing_ok=True)
        finally:
            due = run_leases.release(library_paths()[0], self.lease_id)
            if due:
                run_retention.finish_cleanups_for(due)


def _redacted_text(arcname: str, raw: str) -> str:
    """JSON documents are redacted structurally, anything else (or bad JSON) as text."""
    from apps.admin_console.core.redaction import redact_image_data

    raw = redact_image_data(raw)
    if arcname.endswith(".json"):
        try:
            return json.dumps(redact_json(json.loads(raw)), ensure_ascii=False, indent=2)
        except ValueError:
            return redact_text(raw)
    if arcname.endswith(".jsonl"):
        lines = []
        for line in raw.splitlines():
            try:
                lines.append(json.dumps(redact_json(json.loads(line)), ensure_ascii=False))
            except ValueError:
                lines.append(redact_text(line))
        return "\n".join(lines) + "\n"
    return redact_text(raw)


def _read_text(artifact: Artifact) -> str:
    with _open_regular(artifact) as handle:
        data = handle.read(_TEXT_LIMIT + 1)
    text = data[:_TEXT_LIMIT].decode("utf-8", errors="replace")
    return text + "\n[truncated]\n" if len(data) > _TEXT_LIMIT else text


def _open_regular(artifact: Artifact) -> BinaryIO:
    """Open at write time, refusing a link in any component, whatever listing saw."""
    if artifact.path is None or artifact.root is None:
        raise ValueError(f"{artifact.arcname} has no file to open")
    return open_under(artifact.root, artifact.path)


class _Budget:
    def __init__(self, limit: int):
        self.left = limit

    def spend(self, size: int) -> None:
        self.left -= size
        if self.left < 0:
            raise BundleError(413, "bundle_too_large", limit_bytes=MAX_BUNDLE_BYTES)


def _write_artifact(
    archive: zipfile.ZipFile, artifact: Artifact, budget: _Budget, manifest: Manifest
) -> bool:
    """Add one entry; False when it was refused at open time (and is listed as skipped)."""
    if ".." in artifact.arcname.split("/") or artifact.arcname.startswith("/"):
        raise ValueError(f"unsafe bundle entry name: {artifact.arcname!r}")
    media = artifact.kind == "media"
    info = zipfile.ZipInfo(artifact.arcname, time.localtime(time.time())[:6])
    info.compress_type = zipfile.ZIP_STORED if media else zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    try:
        if artifact.path is None or not media:
            text = (
                artifact.text
                if artifact.text is not None
                else _redacted_text(artifact.arcname, _read_text(artifact))
            )
            data = text.encode("utf-8")
            budget.spend(len(data))
            archive.writestr(info, data)
            return True
        with _open_regular(artifact) as source, archive.open(info, "w", force_zip64=True) as sink:
            while chunk := source.read(_CHUNK):
                budget.spend(len(chunk))
                sink.write(chunk)
        return True
    except UnsafePath:
        manifest.skipped.append({"name": artifact.arcname, "reason": "symlink"})
    except FileNotFoundError:
        manifest.skipped.append({"name": artifact.arcname, "reason": "missing"})
    return False


def _build_zip(manifest: Manifest, dest: BinaryIO) -> int:
    """Write the manifest's artifacts and a manifest.json into ``dest``; returns entry count."""
    budget = _Budget(MAX_BUNDLE_BYTES)
    with zipfile.ZipFile(dest, "w", allowZip64=True) as archive:
        written = [a for a in manifest.artifacts if _write_artifact(archive, a, budget, manifest)]
        listing = {
            "session_id": manifest.session_id,
            "generated_at": time.time(),
            "entries": [
                {"name": a.arcname, "kind": a.kind, "redacted": a.kind == "text"} for a in written
            ],
            "skipped": manifest.skipped,
            "note": "Text is redacted. Screenshots and video are not.",
        }
        _write_artifact(
            archive,
            Artifact("manifest.json", "text", 0, text=json.dumps(listing, indent=2)),
            budget,
            manifest,
        )
    return len(written) + 1


def _lookup(session_id: str) -> dict:
    try:
        found = run_catalog_repo.get_run(session_id)
    except ValueError as exc:
        raise BundleError(400, "invalid_session_id") from exc
    except CatalogNotReady as exc:
        raise BundleError(
            503, "catalog_not_ready", retry_after=RETRY_AFTER_SECONDS, retryable=True
        ) from exc
    if found.run:
        return found.run
    if found.candidates:
        raise BundleError(409, "ambiguous_prefix", candidates=found.candidates)
    if found.removed:
        raise BundleError(
            410, "removed", **{k: v for k, v in found.removed.items() if k != "session_id"}
        )
    raise BundleError(404, "not_found")


def prepare(session_id: str) -> BundleFile:
    """Build the bundle for one run into a temp file, holding a lease on the run."""
    if not _slots.acquire(blocking=False):
        raise BundleError(429, "bundle_busy", retry_after=RETRY_AFTER_SECONDS)
    db_path, _ = library_paths()
    started = time.monotonic()
    lease_id: str | None = None
    temp: Path | None = None
    handed_over = False
    try:
        # Resolve first (an id or prefix that names no single run takes no lease),
        # lease the full id, then look again: a delete that wins the race either
        # sees the lease (and waits) or is seen by the second look (and we stop).
        full_id = _lookup(session_id)["session_id"]
        lease_id = run_leases.acquire(db_path, full_id)
        run = _lookup(full_id)
        manifest = resolve_manifest(full_id, run["prompt"])
        if manifest.total_bytes > MAX_BUNDLE_BYTES:
            raise BundleError(413, "bundle_too_large", limit_bytes=MAX_BUNDLE_BYTES)
        fd, name = tempfile.mkstemp(prefix="artemis-bundle-", suffix=".zip")
        temp = Path(name)
        with os.fdopen(fd, "wb") as dest:
            entries = _build_zip(manifest, dest)
        bundle = BundleFile(
            full_id, temp, temp.stat().st_size, entries, len(manifest.skipped), lease_id, started
        )
        handed_over = True
        return bundle
    except OSError as exc:
        if exc.errno != errno.ENOSPC:
            raise
        run_storage.note_insufficient_storage(db_path)
        raise BundleError(507, "insufficient_storage") from exc
    finally:
        _slots.release()
        if not handed_over:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if lease_id is not None:
                due = run_leases.release(db_path, lease_id)
                if due:
                    run_retention.finish_cleanups_for(due)
