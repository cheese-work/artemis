"""Device state history: idempotent ``state_changed`` rows in ``device_events`` (CHE-1477).

Contract: docs/device-identity.md, "Device state". A row never points at a foreign key.
"""

from itertools import groupby
import json

from apps.admin_console.database.connection import db_session

KIND = "state_changed"
UNKNOWN = "unknown"


class DeviceEventRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def record_state(
        self, device_id: str, state: str, ts: float, *, reason: str | None = None
    ) -> bool:
        """Append the transition to ``state``; False when it was already the latest state.

        Replaying a transition (a restart, a second writer) leaves one row: the latest
        state is read and written in one transaction, and the event key backs it up.
        """
        with db_session(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                if self._latest(conn, device_id) == state:
                    return False
                return self._insert(conn, device_id, state, ts, reason)

    @staticmethod
    def _insert(conn, device_id, state, ts, reason, *, after_event_id=None) -> bool:
        payload = {"state": state, **({"reason": reason} if reason else {})}
        key = f"{device_id}:{KIND}:{state}:{ts!r}"
        if after_event_id is not None:
            key = f"{key}:after:{after_event_id}"
        cursor = conn.execute(
            "INSERT OR IGNORE INTO device_events (device_id, event_key, kind, payload, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (device_id, key, KIND, json.dumps(payload), ts),
        )
        return cursor.rowcount == 1

    def begin_boot(self, now: float) -> None:
        """Atomically close known states at the old checkpoint and record this boot."""
        with db_session(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                checkpoint = conn.execute(
                    "SELECT last_alive_at FROM device_reconciler_checkpoint WHERE singleton = 1"
                ).fetchone()
                alive = min(checkpoint[0], now) if checkpoint is not None else None
                for device in conn.execute(
                    "SELECT DISTINCT device_id FROM device_events WHERE kind = ?", (KIND,)
                ).fetchall():
                    device_id = device[0]
                    event = self._latest_event(conn, device_id)
                    if event is not None and json.loads(event["payload"])["state"] != UNKNOWN:
                        ts = event["ts"]
                        self._insert(
                            conn,
                            device_id,
                            UNKNOWN,
                            max(ts, alive) if alive is not None else ts,
                            "startup",
                            after_event_id=event["event_id"],
                        )
                self._checkpoint(conn, now)

    def checkpoint(self, now: float) -> None:
        with db_session(self.db_path) as conn, conn:
            self._checkpoint(conn, now)

    @staticmethod
    def _checkpoint(conn, now: float) -> None:
        conn.execute(
            "INSERT INTO device_reconciler_checkpoint (singleton, last_alive_at) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET last_alive_at = excluded.last_alive_at "
            "WHERE last_alive_at != excluded.last_alive_at",
            (now,),
        )

    def compact_states(self, cutoff: float) -> None:
        """Keep one pre-cutoff state per device and remove in-window state repeats."""
        with db_session(self.db_path) as conn, conn:
            conn.execute(
                """
                DELETE FROM device_events WHERE event_id IN (
                    SELECT event_id FROM (
                        SELECT event_id, ts, json_extract(payload, '$.state') AS state,
                               lag(json_extract(payload, '$.state')) OVER history AS previous_state,
                               lead(ts) OVER history AS next_ts
                        FROM device_events WHERE kind = ?
                        WINDOW history AS (PARTITION BY device_id ORDER BY ts, event_id)
                    ) WHERE next_ts < ? OR (ts >= ? AND state = previous_state)
                )
                """,
                (KIND, cutoff, cutoff),
            )

    def timeline(self, device_id: str, start: float, end: float) -> list[tuple[str, float, float]]:
        """Return (state, start, end) spans in Unix seconds for the half-open window."""
        if not start < end:
            return []
        with db_session(self.db_path) as conn:
            conn.execute("BEGIN")
            seed = conn.execute(
                "SELECT payload FROM device_events WHERE device_id = ? AND kind = ? AND ts <= ? "
                "ORDER BY ts DESC, event_id DESC LIMIT 1",
                (device_id, KIND, start),
            ).fetchone()
            rows = conn.execute(
                "SELECT ts, payload FROM device_events WHERE device_id = ? AND kind = ? "
                "AND ts > ? AND ts < ? ORDER BY ts, event_id DESC",
                (device_id, KIND, start, end),
            ).fetchall()

        state = json.loads(seed[0])["state"] if seed else UNKNOWN
        spans = []
        for ts, events in groupby(rows, key=lambda row: row["ts"]):
            next_state = json.loads(next(events)["payload"])["state"]
            if state != next_state:
                spans.append((state, start, ts))
                state, start = next_state, ts
        spans.append((state, start, end))
        return spans

    def latest_states(self) -> dict[str, str]:
        """The newest recorded state of every device that has one."""
        with db_session(self.db_path) as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT device_id FROM device_events WHERE kind = ?", (KIND,)
                )
            ]
            return {device_id: self._latest(conn, device_id) or UNKNOWN for device_id in ids}

    def latest_state(self, device_id: str) -> str | None:
        with db_session(self.db_path) as conn:
            return self._latest(conn, device_id)

    @classmethod
    def _latest(cls, conn, device_id: str) -> str | None:
        row = cls._latest_event(conn, device_id)
        return json.loads(row["payload"])["state"] if row else None

    @staticmethod
    def _latest_event(conn, device_id: str):
        return conn.execute(
            "SELECT event_id, payload, ts FROM device_events WHERE device_id = ? AND kind = ? "
            "ORDER BY ts DESC, event_id DESC LIMIT 1",
            (device_id, KIND),
        ).fetchone()
