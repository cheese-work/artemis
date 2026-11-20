"""Durable staging for resumable host recordings, with one store per private root."""

from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from hashlib import sha256 as sha256_digest
import json
import os
from pathlib import Path
import re
from threading import Lock
import time
from uuid import uuid4

from apps.admin_console.services.config_store import _atomic_write, _fsync_directory

MAX_CHUNK_BYTES = 8 * 1024 * 1024
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class InvalidUpload(ValueError):
    pass


class UploadConflict(Exception):
    pass


class UploadNotFound(Exception):
    pass


class ChunkTooLarge(Exception):
    pass


class DeclaredLengthExceeded(Exception):
    pass


class ChecksumMismatch(Exception):
    pass


class OffsetConflict(Exception):
    def __init__(self, durable_offset: int) -> None:
        super().__init__(durable_offset)
        self.durable_offset = durable_offset


@dataclass(frozen=True)
class _Upload:
    upload_id: str
    host_id: str
    session_id: str
    recording_id: str
    declared_length: int
    sha256: str
    offset: int
    created_at: float
    updated_at: float


class RecordingUploadStore:
    def __init__(
        self,
        staging_root: Path,
        max_declared_bytes: int = 1024**3,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        self._root = staging_root
        self._max_declared_bytes = max_declared_bytes
        self._lock = Lock()
        self._directory_sync_pending = False
        self._root.mkdir(mode=0o700, exist_ok=True)
        _fsync_directory(self._root.parent)
        self._recover()

    def create(
        self, host_id: str, session_id: str, recording_id: str, declared_length: int, sha256: str
    ) -> tuple[str, int]:
        if not (
            isinstance(session_id, str)
            and _UUID.fullmatch(session_id)
            and isinstance(recording_id, str)
            and _UUID.fullmatch(recording_id)
            and isinstance(sha256, str)
            and _SHA256.fullmatch(sha256)
            and type(declared_length) is int
            and 0 < declared_length <= self._max_declared_bytes
        ):
            raise InvalidUpload("invalid recording identifiers, checksum, or declared length")

        with self._lock:
            self._sync_pending_directory()
            for upload in self._uploads():
                if (upload.host_id, upload.recording_id) != (host_id, recording_id):
                    continue
                if (upload.declared_length, upload.sha256) != (declared_length, sha256):
                    raise UploadConflict
                return upload.upload_id, upload.offset

            upload_id = uuid4().hex
            now = self.clock()
            upload = _Upload(
                upload_id, host_id, session_id, recording_id, declared_length, sha256, 0, now, now
            )
            part_path = self._root / f"{upload_id}.part"
            descriptor = os.open(part_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._write_metadata(upload)
            return upload_id, 0

    def offset(self, host_id: str, upload_id: str) -> int:
        with self._lock:
            return self._owned_upload(host_id, upload_id).offset

    def append(
        self, host_id: str, upload_id: str, offset: int, chunk: bytes, chunk_sha256: str
    ) -> int:
        with self._lock:
            upload = self._owned_upload(host_id, upload_id)
            if len(chunk) > MAX_CHUNK_BYTES:
                raise ChunkTooLarge
            if sha256_digest(chunk).hexdigest() != chunk_sha256:
                raise ChecksumMismatch
            if type(offset) is not int or offset < 0:
                raise OffsetConflict(upload.offset)
            end = offset + len(chunk)
            if end > upload.declared_length:
                raise DeclaredLengthExceeded

            part_path = self._root / f"{upload_id}.part"
            if offset != upload.offset:
                if end <= upload.offset:
                    with part_path.open("rb") as part:
                        part.seek(offset)
                        if sha256_digest(part.read(len(chunk))).hexdigest() == chunk_sha256:
                            return upload.offset
                raise OffsetConflict(upload.offset)

            with part_path.open("r+b") as part:
                part.seek(offset)
                part.write(chunk)
                part.truncate(end)
                part.flush()
                os.fsync(part.fileno())
            self._write_metadata(replace(upload, offset=end, updated_at=self.clock()))
            return end

    def abort(self, host_id: str, upload_id: str) -> None:
        with self._lock:
            self._owned_upload(host_id, upload_id)
            (self._root / f"{upload_id}.json").unlink()
            self._directory_sync_pending = True
            self._sync_pending_directory()
            (self._root / f"{upload_id}.part").unlink()
            _fsync_directory(self._root)

    def _owned_upload(self, host_id: str, upload_id: str) -> _Upload:
        if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
            raise UploadNotFound
        try:
            upload = self._read_metadata(self._root / f"{upload_id}.json")
        except FileNotFoundError:
            raise UploadNotFound from None
        if upload.host_id != host_id:
            raise UploadNotFound
        self._sync_pending_directory()
        return upload

    def _uploads(self) -> Iterator[_Upload]:
        for path in self._root.glob("*.json"):
            yield self._read_metadata(path)

    @staticmethod
    def _read_metadata(path: Path) -> _Upload:
        upload = _Upload(**json.loads(path.read_bytes()))
        if not _UPLOAD_ID.fullmatch(path.stem) or upload.upload_id != path.stem:
            raise ValueError("recording upload metadata does not match its filename")
        if not 0 <= upload.offset <= upload.declared_length:
            raise ValueError("recording upload metadata has an invalid offset")
        return upload

    def _write_metadata(self, upload: _Upload) -> None:
        self._directory_sync_pending = True
        _atomic_write(self._root / f"{upload.upload_id}.json", json.dumps(asdict(upload)).encode())
        self._directory_sync_pending = False

    def _sync_pending_directory(self) -> None:
        """Resolve an interrupted metadata replace before acknowledging a retry."""
        if self._directory_sync_pending:
            _fsync_directory(self._root)
            self._directory_sync_pending = False

    def _recover(self) -> None:
        for upload in self._uploads():
            part_path = self._root / f"{upload.upload_id}.part"
            size = part_path.stat().st_size
            if size < upload.offset:
                raise OSError("staged recording is shorter than its durable offset")
            if size > upload.offset:
                with part_path.open("r+b") as part:
                    part.truncate(upload.offset)
                    os.fsync(part.fileno())

        for part_path in self._root.glob("*.part"):
            if not part_path.with_suffix(".json").exists():
                part_path.unlink()
        for path in self._root.glob(".*.json.*"):
            if _UPLOAD_ID.fullmatch(path.name.split(".")[1]):
                path.unlink()
        _fsync_directory(self._root)
