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
import errno
import json
import logging
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO

from apps.admin_console.core.redaction import redact_json, redact_text

try:
    from admin_console.database.connection import db_session
    from admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from admin_console.database.repositories.step_repository import StepRepository
except ImportError:
    from apps.admin_console.database.connection import db_session
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from apps.admin_console.database.repositories.step_repository import StepRepository

logger = logging.getLogger(__name__)

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
    root: Path | None = None  # the storage root ``path`` must stay inside, re-checked on open


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
            Artifact(
                arcname, artifact.kind, artifact.size, artifact.path, artifact.text, artifact.root
            )
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


class UnsafePath(OSError):
    """A path component turned out to be a link (or the path left its root) at use time."""


class RemovalFailed(OSError):
    """Something under a run's storage could not be deleted; the run must be retried."""


# A link in the path, or a path that is not a directory, is refused for good (retrying
# cannot help). Anything else (I/O error, permissions, busy) is a failure to retry.
_REFUSED = (errno.ELOOP, errno.ENOTDIR)


def remove_under(
    root: Path,
    candidate: Path,
    *,
    keep: frozenset[tuple[str, ...]] = frozenset(),
    prune_empty_parents: bool = False,
) -> None:
    """Delete a file, link or directory tree under ``root`` without following any link.

    Everything is relative to a directory fd walked from the root with
    ``O_NOFOLLOW`` per component, so a parent swapped for a link at any moment
    (before or during the delete) is refused rather than followed. A link in the
    last position, or inside a tree, is unlinked itself. Entries listed in
    ``keep`` (paths relative to ``root``, owned by another run) are left alone,
    and so are the directories that hold them. ``prune_empty_parents`` removes
    the directories above the target that are left empty (never ``root``).
    Raises RemovalFailed when something that should have gone could not be deleted.
    """
    found = relative_parts(root, candidate)
    if found is None:
        return
    base, parts = found
    fds = []
    try:
        fds.append(os.open(base, os.O_RDONLY | os.O_DIRECTORY))
        for part in parts[:-1]:
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        _remove_entry(fds[-1], parts, keep)
        if prune_empty_parents:
            for depth in range(len(parts) - 1, 0, -1):
                os.rmdir(parts[depth - 1], dir_fd=fds[depth - 1])
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in _REFUSED:
            logger.warning("Refused to follow a link while deleting %s", candidate)
        elif exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise RemovalFailed(exc.errno, f"could not delete {candidate}: {exc}") from exc
    finally:
        for fd in fds:
            os.close(fd)


def _remove_entry(dir_fd: int, rel: tuple[str, ...], keep: frozenset[tuple[str, ...]]) -> None:
    """Remove ``rel[-1]`` inside ``dir_fd``; ``rel`` is its path from the storage root."""
    name = rel[-1]
    if rel in keep:
        return
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        if not stat.S_ISDIR(mode):
            os.unlink(name, dir_fd=dir_fd)
            return
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            with os.scandir(child) as entries:
                names = [entry.name for entry in entries]
            failure = None
            for entry_name in names:  # one entry failing must not strand the others
                try:
                    _remove_entry(child, (*rel, entry_name), keep)
                except OSError as exc:
                    failure = failure or exc
            if failure is not None:
                raise failure
        finally:
            os.close(child)
        if not any(kept[: len(rel)] == rel for kept in keep):
            os.rmdir(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def recordings_under(db_path, traces: Path, key: str) -> list[tuple[str, Path]]:
    """(run, recording path) for every recording at or inside the first-level entry ``key``.

    ``key`` is a task folder directly under ``traces`` (shared by every rerun of
    the same named task) or a file there. Purged runs have no rows left, so
    deleted runs whose cleanup is still pending are included.
    """
    clauses, params = [], {}
    for i, base in enumerate(_roots(traces)):
        path = str(base / key)
        clauses.append(f"{{col}} = :e{i} OR {{col}} LIKE :l{i} ESCAPE '\\'")
        params |= {f"e{i}": path, f"l{i}": _like(path) + "/%"}
    match = " OR ".join(clauses)
    sql = (
        "SELECT session_id, local_video_path FROM video_recordings "
        f"WHERE {match.format(col='local_video_path')} "
        "UNION SELECT session_id, video_filepath FROM sessions "
        f"WHERE {match.format(col='video_filepath')}"
    )
    with db_session(db_path) as conn:
        return [(row[0], Path(row[1])) for row in conn.execute(sql, params)]


def open_under(root: Path, candidate: Path) -> BinaryIO:
    """Open a regular file, refusing a link in ANY component at open time.

    Walks from the root with ``O_NOFOLLOW`` one component at a time, so a
    directory swapped for a link after the manifest was listed is refused
    instead of followed. Raises UnsafePath, or FileNotFoundError if it vanished.
    """
    found = relative_parts(root, candidate)
    if found is None:
        raise UnsafePath(errno.EXDEV, "outside storage")
    base, parts = found
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(exc.errno, "symlink in path") from exc
        raise
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        raise UnsafePath(errno.EINVAL, "not a regular file")
    return os.fdopen(file_fd, "rb")


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


def _size(path: Path) -> int | None:
    """File size, or None when it vanished after it was checked."""
    try:
        return os.lstat(path).st_size
    except FileNotFoundError:
        return None


def _add_file(manifest, root: Path, candidate: Path, arcname: str, kind: str, name: str) -> None:
    path, reason = safe_file(root, candidate)
    if reason:
        _skip(manifest, name, reason)
    elif path is not None and (size := _size(path)) is not None:
        manifest.add(Artifact(arcname, kind, size, path=path, root=root))


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
            elif path is not None and (size := _size(path)) is not None:
                manifest.add(Artifact(f"video/{path.name}", "media", size, path=path, root=traces))
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
