"""Transactional event outbox -> append-only, fsynced JSONL, outside the writer lock."""

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3


def enqueue(conn, event, now, *, agent, run_id, issue, devices, **details):
    for device_id in devices or [None]:
        payload = dict(
            event=event,
            time=datetime.fromtimestamp(now, timezone(timedelta(hours=7))).strftime(
                "%Y-%m-%d %H:%M:%S ICT"
            ),
            agent=agent,
            run_id=run_id,
            issue=issue,
            device_id=device_id,
            **details,
        )
        conn.execute("INSERT INTO broker_audit(payload) VALUES (?)", (json.dumps(payload),))


def flush(db_path):
    path = Path(str(db_path) + ".audit.jsonl")
    # This separate lock serializes exporters, never network/device work or SQLite writers.
    lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT seq, payload FROM broker_audit ORDER BY seq").fetchall()
        lines = [
            json.dumps(dict(json.loads(payload), seq=seq), sort_keys=True) + "\n"
            for seq, payload in rows
        ]
        # ponytail: compare the whole prefix; add a verified cursor for measured large logs.
        previous = (
            path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        )
        if previous != lines[: len(previous)]:
            raise RuntimeError("audit prefix damaged; admission closed, ops recovery required")
        if len(previous) < len(lines):
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as output:
                output.writelines(lines[len(previous) :])
                output.flush()
                os.fsync(output.fileno())
