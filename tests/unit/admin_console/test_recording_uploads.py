from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest

from apps.admin_console.services import recording_uploads
from apps.admin_console.services.recording_uploads import (
    ChecksumMismatch,
    ChunkTooLarge,
    DeclaredLengthExceeded,
    InvalidUpload,
    OffsetConflict,
    RecordingUploadStore,
    UploadConflict,
    UploadNotFound,
)

HOST = "host-a"
SESSION = "a5395d15-9fbe-4b3f-bc7a-d15202c02885"
RECORDING = "db39039d-a1d1-435a-a3da-06f89402fdbf"
DIGEST = sha256(b"recording").hexdigest()
CHUNK_LIMIT = 8 * 1024 * 1024


@pytest.fixture(autouse=True)
def synced_run_model() -> None:
    """Do not provision a provider catalog for storage-only tests."""


def _snapshot(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in root.iterdir()}


def _append(store: RecordingUploadStore, upload_id: str, offset: int, chunk: bytes) -> int:
    return store.append(HOST, upload_id, offset, chunk, sha256(chunk).hexdigest())


def test_create_is_idempotent_per_host_and_recording(tmp_path: Path) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, offset = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    parsed_id = UUID(hex=upload_id)
    assert parsed_id.hex == upload_id and parsed_id.version == 4
    assert offset == 0
    assert store.create(HOST, SESSION, RECORDING, 9, DIGEST) == (upload_id, 0)
    assert set(_snapshot(tmp_path)) == {f"{upload_id}.part", f"{upload_id}.json"}
    assert _append(store, upload_id, 0, b"rec") == 3

    new_session = "73c3e890-2a7e-498a-acc4-801949f2416e"
    assert store.create(HOST, new_session, RECORDING, 9, DIGEST) == (upload_id, 3)
    before = _snapshot(tmp_path)
    with pytest.raises(UploadConflict):
        store.create(HOST, SESSION, RECORDING, 10, DIGEST)
    with pytest.raises(UploadConflict):
        store.create(HOST, SESSION, RECORDING, 9, "0" * 64)
    assert _snapshot(tmp_path) == before
    assert store.create("host-b", SESSION, RECORDING, 9, DIGEST)[0] != upload_id


@pytest.mark.parametrize(
    ("session_id", "recording_id", "length", "digest"),
    [
        ("not-a-uuid", RECORDING, 9, DIGEST),
        (SESSION, "../recording", 9, DIGEST),
        (SESSION.upper(), RECORDING, 9, DIGEST),
        (SESSION, UUID(RECORDING).hex, 9, DIGEST),
        (SESSION, RECORDING, 9, "bad"),
        (SESSION, RECORDING, 9, DIGEST.upper()),
        (SESSION, RECORDING, 0, DIGEST),
        (SESSION, RECORDING, 10, DIGEST),
        (SESSION, RECORDING, True, DIGEST),
    ],
)
def test_invalid_create_writes_no_files(
    tmp_path: Path, session_id: str, recording_id: str, length: int, digest: str
) -> None:
    store = RecordingUploadStore(tmp_path, max_declared_bytes=9)
    with pytest.raises(InvalidUpload):
        store.create(HOST, session_id, recording_id, length, digest)
    assert not list(tmp_path.iterdir())


def test_append_advances_offset_and_persists_bytes_and_metadata(tmp_path: Path) -> None:
    times = iter((100.0, 101.0, 102.0))
    store = RecordingUploadStore(tmp_path, max_declared_bytes=9, clock=lambda: next(times))
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)

    assert _append(store, upload_id, 0, b"rec") == 3
    assert store.offset(HOST, upload_id) == 3
    assert _append(store, upload_id, 3, b"ording") == 9
    assert store.offset(HOST, upload_id) == 9
    assert (tmp_path / f"{upload_id}.part").read_bytes() == b"recording"
    assert json.loads((tmp_path / f"{upload_id}.json").read_text()) == {
        "upload_id": upload_id,
        "host_id": HOST,
        "session_id": SESSION,
        "recording_id": RECORDING,
        "declared_length": 9,
        "sha256": DIGEST,
        "offset": 9,
        "created_at": 100.0,
        "updated_at": 102.0,
    }


def test_bad_chunk_checksum_leaves_upload_unchanged(tmp_path: Path) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    before = _snapshot(tmp_path)

    with pytest.raises(ChecksumMismatch):
        store.append(HOST, upload_id, 0, b"rec", DIGEST)

    assert store.offset(HOST, upload_id) == 0
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("offset", [-1, 2, 4])
def test_wrong_offset_reports_durable_offset(tmp_path: Path, offset: int) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    assert _append(store, upload_id, 0, b"rec") == 3
    before = _snapshot(tmp_path)

    with pytest.raises(OffsetConflict) as conflict:
        _append(store, upload_id, offset, b"or")

    assert conflict.value.durable_offset == 3
    assert _snapshot(tmp_path) == before


def test_duplicate_range_must_match_stored_bytes(tmp_path: Path) -> None:
    times = iter((100.0, 101.0))
    store = RecordingUploadStore(tmp_path, clock=lambda: next(times))
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    assert _append(store, upload_id, 0, b"record") == 6
    before = _snapshot(tmp_path)

    assert _append(store, upload_id, 1, b"ec") == 6
    with pytest.raises(OffsetConflict) as conflict:
        _append(store, upload_id, 1, b"xx")

    assert conflict.value.durable_offset == 6
    assert _snapshot(tmp_path) == before


def test_chunk_and_declared_length_limits(tmp_path: Path) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, CHUNK_LIMIT + 1, DIGEST)
    chunk = b"x" * CHUNK_LIMIT
    before = _snapshot(tmp_path)

    with pytest.raises(ChunkTooLarge):
        _append(store, upload_id, 0, chunk + b"x")
    assert _snapshot(tmp_path) == before
    assert _append(store, upload_id, 0, chunk) == CHUNK_LIMIT
    with pytest.raises(DeclaredLengthExceeded):
        _append(store, upload_id, CHUNK_LIMIT, b"xx")
    assert store.offset(HOST, upload_id) == CHUNK_LIMIT
    assert (tmp_path / f"{upload_id}.part").stat().st_size == CHUNK_LIMIT


def test_foreign_or_unknown_upload_is_not_found(tmp_path: Path) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    before = _snapshot(tmp_path)
    for host, identifier in [("host-b", upload_id), (HOST, "0" * 32), (HOST, "../invalid")]:
        with pytest.raises(UploadNotFound):
            store.offset(host, identifier)
        with pytest.raises(UploadNotFound):
            store.append(host, identifier, 0, b"not checked", "bad checksum")
        with pytest.raises(UploadNotFound):
            store.abort(host, identifier)
    assert _snapshot(tmp_path) == before


def test_restart_truncates_uncommitted_bytes_and_removes_orphans(tmp_path: Path) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    assert _append(store, upload_id, 0, b"rec") == 3
    (tmp_path / f"{upload_id}.part").write_bytes(b"recording")
    (tmp_path / f"{'0' * 32}.part").write_bytes(b"orphan")
    (tmp_path / f".{upload_id}.json.uncommitted").write_bytes(b"uncommitted metadata")

    restarted = RecordingUploadStore(tmp_path)

    assert restarted.offset(HOST, upload_id) == 3
    assert restarted.create(HOST, SESSION, RECORDING, 9, DIGEST) == (upload_id, 3)
    assert set(_snapshot(tmp_path)) == {f"{upload_id}.part", f"{upload_id}.json"}
    assert (tmp_path / f"{upload_id}.part").read_bytes() == b"rec"
    assert _append(restarted, upload_id, 3, b"ording") == 9


def test_abort_removes_both_files_and_allows_a_new_upload(tmp_path: Path) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    assert _append(store, upload_id, 0, b"rec") == 3

    store.abort(HOST, upload_id)

    assert not list(tmp_path.iterdir())
    restarted = RecordingUploadStore(tmp_path)
    with pytest.raises(UploadNotFound):
        restarted.offset(HOST, upload_id)
    new_id, offset = restarted.create(HOST, SESSION, RECORDING, 9, DIGEST)
    assert new_id != upload_id and offset == 0


def test_append_io_failure_before_metadata_commit_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    assert _append(store, upload_id, 0, b"rec") == 3

    def interrupted_write(path: Path, contents: bytes) -> None:
        assert (tmp_path / f"{upload_id}.part").read_bytes() == b"recording"
        raise OSError("interrupted metadata write")

    with monkeypatch.context() as patch:
        patch.setattr(recording_uploads, "_atomic_write", interrupted_write)
        with pytest.raises(OSError, match="interrupted metadata write"):
            _append(store, upload_id, 3, b"ording")
    assert store.offset(HOST, upload_id) == 3
    assert _append(store, upload_id, 3, b"ord") == 6
    assert (tmp_path / f"{upload_id}.part").read_bytes() == b"record"


def test_retry_syncs_directory_after_metadata_replace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.admin_console.services import config_store

    store = RecordingUploadStore(tmp_path)
    upload_id, _ = store.create(HOST, SESSION, RECORDING, 9, DIGEST)
    original_sync = recording_uploads._fsync_directory
    synced: list[Path] = []

    def directory_sync_error(path: Path) -> None:
        assert json.loads((tmp_path / f"{upload_id}.json").read_bytes())["offset"] == 3
        raise OSError("interrupted directory sync")

    def track_sync(path: Path) -> None:
        original_sync(path)
        synced.append(path)

    with monkeypatch.context() as patch:
        patch.setattr(config_store, "_fsync_directory", directory_sync_error)
        with pytest.raises(OSError, match="interrupted directory sync"):
            _append(store, upload_id, 0, b"rec")

    monkeypatch.setattr(recording_uploads, "_fsync_directory", track_sync)
    assert _append(store, upload_id, 0, b"rec") == 3
    assert synced == [tmp_path]
