"""Principals and delegations (CHE-1385; contract: docs/spaces-contract.md)."""

from dataclasses import dataclass
import sqlite3
import time
import uuid

from apps.admin_console.database.connection import db_session


class PrincipalStoreNotReady(Exception):
    """The principal tables are missing (schema bootstrap did not run or failed)."""


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    issuer: str
    sub: str
    kind: str
    email: str | None
    history_email: str | None


_COLUMNS = "id, issuer, sub, kind, email, history_email"


def _principal(row: sqlite3.Row) -> Principal:
    return Principal(**{key: row[key] for key in row.keys()})


class PrincipalRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def get(self, issuer: str, sub: str) -> Principal | None:
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            return self._find(conn, issuer, sub)

    def ensure_user(self, issuer: str, sub: str, email: str | None) -> Principal:
        """The user principal for a verified ``(issuer, sub)``; created on first login.

        ``history_email`` is claimed only by the first principal to present an
        email, so a later subject with the same email never inherits its history.
        """
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            found = self._find(conn, issuer, sub)
            if found and found.email == email:
                return found
            conn.execute("BEGIN IMMEDIATE")  # serializes first logins and the claim
            with conn:  # commits, or rolls back on error
                found = self._find(conn, issuer, sub)
                if found:
                    conn.execute("UPDATE principals SET email = ? WHERE id = ?", (email, found.id))
                else:
                    conn.execute(
                        "INSERT INTO principals "
                        "(id, issuer, sub, kind, email, history_email, created_at) "
                        "VALUES (?, ?, ?, 'user', ?, "
                        "CASE WHEN ? IS NOT NULL AND NOT EXISTS "
                        "(SELECT 1 FROM principals WHERE history_email = ?) THEN ? END, ?)",
                        (uuid.uuid4().hex, issuer, sub, email, email, email, email, time.time()),
                    )
            result = self._find(conn, issuer, sub)
            if result is None:  # written under the write lock above
                raise RuntimeError("principal missing after write")
            return result

    def grant_delegation(
        self, agent_id: str, human_id: str, space_ids: list[str], expires_at: float
    ) -> str:
        delegation_id = uuid.uuid4().hex
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                conn.execute(
                    "INSERT INTO delegations "
                    "(id, agent_principal_id, human_principal_id, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (delegation_id, agent_id, human_id, expires_at, time.time()),
                )
                conn.executemany(
                    "INSERT INTO delegation_spaces (delegation_id, space_id) VALUES (?, ?)",
                    [(delegation_id, space_id) for space_id in dict.fromkeys(space_ids)],
                )
        return delegation_id

    def revoke_delegation(self, delegation_id: str) -> bool:
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            cursor = conn.execute(
                "UPDATE delegations SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (time.time(), delegation_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delegation_covers(self, agent_id: str, human_id: str, space_id: str) -> bool:
        """Whether ``human_id`` currently lets ``agent_id`` act in ``space_id``."""
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            return (
                conn.execute(
                    "SELECT 1 FROM delegations d JOIN delegation_spaces s ON s.delegation_id = d.id "
                    "WHERE d.agent_principal_id = ? AND d.human_principal_id = ? "
                    "AND s.space_id = ? AND d.revoked_at IS NULL AND d.expires_at > ?",
                    (agent_id, human_id, space_id, time.time()),
                ).fetchone()
                is not None
            )

    @staticmethod
    def _find(conn: sqlite3.Connection, issuer: str, sub: str) -> Principal | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM principals WHERE issuer = ? AND sub = ?", (issuer, sub)
        ).fetchone()
        return _principal(row) if row else None

    @staticmethod
    def _require_ready(conn: sqlite3.Connection) -> None:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'principals'"
        ).fetchone():
            raise PrincipalStoreNotReady


principal_repo = PrincipalRepository()
