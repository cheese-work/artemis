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

"""The artifact manifest of one run: what a bundle holds and what a deletion removes.

Artifacts are never found by joining a client or database string onto a
directory. Every candidate goes through ``safe_file``: it must sit lexically
under its storage root with no ``..``, no component from the root down may be
a symlink, and it must be a regular file. Anything else is listed in
``Manifest.skipped`` with the reason, never followed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat

from apps.admin_console.core.redaction import redact_json, redact_text

try:
    from admin_console.database.connection import db_session
    from admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from admin_console.database.repositories.step_repository import StepRepository
except ImportError:
    from apps.admin_console.database.connection import db_session
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from apps.admin_console.database.repositories.step_repository import StepRepository

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")
_NOTE_SUFFIXES = {".md", ".txt", ".json", ".yaml"}
_SESSION_FILES = {
    "stdout.log": "logs",
    "stderr.log": "logs",
    "check_ledger.jsonl": "checks",
    "check_streams.jsonl": "checks",
    "run_outcome.json": "checks",
    "status.json": "",
}


class RunLibraryError(Exception):
    """A refusal the API reports as ``{"error": code, ...}`` with this HTTP status."""

    def __init__(self, status: int, code: str, **extra: object):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


def library_paths() -> tuple[Path | None, Path]:
    """(database path, traces directory) the run catalog is configured with."""
    return run_catalog_repo.db_path, Path(run_catalog_repo.traces_dir)


@dataclass(frozen=True, slots=True)
class Artifact:
    arcname: str
    kind: str  # "text" (redacted on the way out) or "media" (never redacted)
    size: int
    path: Path | None = None  # a real file under storage, or...
    text: str | None = None  # ...generated content, already redacted


@dataclass(slots=True)
class Manifest:
    session_id: str
    artifacts: list[Artifact] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(a.size for a in self.artifacts)

    def add(self, artifact: Artifact) -> None:
        names = {a.arcname for a in self.artifacts}
        stem, dot, suffix = artifact.arcname.rpartition(".")
        n = 1
        arcname = artifact.arcname
        while arcname in names:
            n += 1
            arcname = f"{stem}-{n}{dot}{suffix}" if dot else f"{artifact.arcname}-{n}"
        self.artifacts.append(
            Artifact(arcname, artifact.kind, artifact.size, artifact.path, artifact.text)
        )


def _roots(root: Path) -> list[Path]:
    return [root, Path(os.path.realpath(root))]


def relative_parts(root: Path, candidate: Path) -> tuple[Path, tuple[str, ...]] | None:
    for base in _roots(root):
        try:
            rel = candidate.relative_to(base)
        except ValueError:
            continue
        if rel.parts and ".." not in rel.parts and "." not in rel.parts:
            return base, rel.parts
    return None


def safe_file(root: Path, candidate: Path) -> tuple[Path | None, str | None]:
    """(path, None) for a regular file under ``root``; (None, reason) when refused.

    A missing file is (None, None): absent, not suspicious.
    """
    found = relative_parts(root, candidate)
    if found is None:
        return None, "outside_storage"
    base, parts = found
    current = base
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return None, None
        except OSError:
            return None, "unreadable"
        if stat.S_ISLNK(mode):
            return None, "symlink"
        if index == len(parts) - 1 and not stat.S_ISREG(mode):
            return None, "not_a_file"
    real_root = os.path.realpath(base)
    if os.path.commonpath([real_root, os.path.realpath(current)]) != real_root:
        return None, "outside_storage"
    return current, None


def safe_dir(root: Path, candidate: Path) -> tuple[Path | None, str | None]:
    """Like ``safe_file`` for a directory (the leaf must be a real directory)."""
    found = relative_parts(root, candidate)
    if found is None:
        return None, "outside_storage"
    base, parts = found
    current = base
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return None, None
        except OSError:
            return None, "unreadable"
        if stat.S_ISLNK(mode):
            return None, "symlink"
    return (current, None) if stat.S_ISDIR(mode) else (None, "not_a_directory")


def recorded_videos(db_path, session_id: str) -> list[Path]:
    """Recording paths the database names for this run (not yet checked for safety)."""
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT local_video_path AS p FROM video_recordings WHERE session_id = ? "
            "UNION SELECT video_filepath FROM sessions WHERE session_id = ?",
            (session_id, session_id),
        ).fetchall()
    return [Path(row[0]) for row in rows if row[0]]


def resolve_manifest(session_id: str, prompt: str | None, *, generated: bool = True) -> Manifest:
    """Artifacts of a run, resolved from the database and the allowed directories only.

    ``generated=False`` skips the prompt and steps documents (sizing a run needs
    only its files).
    """
    db_path, traces = library_paths()
    manifest = Manifest(session_id)
    if generated:
        manifest.add(_generated("prompt.txt", redact_text(prompt or "")))
        steps = StepRepository(db_path).get_session_steps(session_id)
        steps_json = json.dumps(redact_json(steps), ensure_ascii=False, indent=2, default=str)
        manifest.add(_generated("steps.json", steps_json))

    _add_images(manifest, traces / "images", image_names(db_path, session_id))
    _add_videos(manifest, traces, db_path, session_id)
    _add_session_files(manifest, traces, session_id)
    return manifest


def _generated(arcname: str, text: str) -> Artifact:
    return Artifact(arcname, "text", len(text.encode("utf-8")), text=text)


def _skip(manifest: Manifest, name: str, reason: str) -> None:
    manifest.skipped.append({"name": name, "reason": reason})


def _add_file(manifest, root: Path, candidate: Path, arcname: str, kind: str, name: str) -> None:
    path, reason = safe_file(root, candidate)
    if reason:
        _skip(manifest, name, reason)
    elif path is not None:
        manifest.add(Artifact(arcname, kind, os.lstat(path).st_size, path=path))


def image_names(db_path, session_id: str) -> list[str]:
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT pre_image_name FROM steps WHERE session_id = ? "
            "UNION SELECT post_image_name FROM steps WHERE session_id = ?",
            (session_id, session_id),
        ).fetchall()
    return sorted(row[0] for row in rows if isinstance(row[0], str) and row[0])


def image_file(images: Path, name: str) -> Path | None:
    base = name[:-4] if name.endswith(".jpg") else name
    return images / f"{base}.jpg" if _SAFE_NAME.fullmatch(base) and base.strip(".") else None


def _add_images(manifest: Manifest, images: Path, names: list[str]) -> None:
    for name in names:
        candidate = image_file(images, name)
        if candidate is None:
            _skip(manifest, f"images/{name}", "outside_storage")
            continue
        _add_file(manifest, images, candidate, f"images/{candidate.name}", "media", candidate.name)


def _add_videos(manifest: Manifest, traces: Path, db_path, session_id: str) -> None:
    for recorded in recorded_videos(db_path, session_id):
        # Prefer the browser-playable mp4 next to a raw recording.
        candidates = [recorded]
        if recorded.suffix.lower() != ".mp4":
            candidates.insert(0, recorded.with_suffix(".mp4"))
        for candidate in candidates:
            path, reason = safe_file(traces, candidate)
            if reason:
                _skip(manifest, f"video/{candidate.name}", reason)
            elif path is not None:
                manifest.add(
                    Artifact(f"video/{path.name}", "media", os.lstat(path).st_size, path=path)
                )
                break


def _add_session_files(manifest: Manifest, traces: Path, session_id: str) -> None:
    session_dir, reason = safe_dir(traces, traces / session_id)
    if reason:
        _skip(manifest, session_id, reason)
        return
    if session_dir is None:
        return
    for name, folder in _SESSION_FILES.items():
        arcname = f"{folder}/{name}" if folder else name
        _add_file(manifest, traces, session_dir / name, arcname, "text", arcname)
    notes, reason = safe_dir(traces, session_dir / "notes")
    if reason:
        _skip(manifest, "notes", reason)
    elif notes is not None:
        with os.scandir(notes) as entries:
            names = sorted(e.name for e in entries if Path(e.name).suffix in _NOTE_SUFFIXES)
        for name in names:
            _add_file(manifest, traces, notes / name, f"notes/{name}", "text", f"notes/{name}")
