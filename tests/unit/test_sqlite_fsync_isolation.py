"""Unit tests must not wait on the shared runner disk for sqlite fsyncs (CHE-2621)."""

import os
import sqlite3

SYNCHRONOUS_OFF = 0


def _synchronous(conn):
    return conn.execute("PRAGMA synchronous").fetchone()[0]


def test_new_connections_skip_fsync(tmp_path):
    with sqlite3.connect(tmp_path / "rw.db") as conn:
        assert _synchronous(conn) == SYNCHRONOUS_OFF


def test_wal_connections_and_uri_connections_skip_fsync(tmp_path):
    path = tmp_path / "wal.db"
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    readonly = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert _synchronous(readonly) == SYNCHRONOUS_OFF
    finally:
        readonly.close()


def test_os_fsync_is_a_no_op(tmp_path):
    os.fsync(-1)  # the real call raises EBADF for a bad descriptor
    with open(tmp_path / "f", "w") as handle:
        os.fsync(handle.fileno())
