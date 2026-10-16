"""Device state history: idempotent ``state_changed`` rows in ``device_events`` (CHE-1477).

Contract: docs/device-identity.md, "Device state". A row never points at a foreign key.
"""

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
                payload = {"state": state, **({"reason": reason} if reason else {})}
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO device_events (device_id, event_key, kind, payload, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        device_id,
                        f"{device_id}:{KIND}:{state}:{ts!r}",
                        KIND,
                        json.dumps(payload),
                        ts,
                    ),
                )
                return cursor.rowcount == 1

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

    @staticmethod
    def _latest(conn, device_id: str) -> str | None:
        row = conn.execute(
            "SELECT payload FROM device_events WHERE device_id = ? AND kind = ? "
            "ORDER BY ts DESC, event_id DESC LIMIT 1",
            (device_id, KIND),
        ).fetchone()
        return json.loads(row[0])["state"] if row else None
