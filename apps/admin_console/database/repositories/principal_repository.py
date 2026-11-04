"""Principals, email history and delegations (CHE-1385; contract: docs/spaces-contract.md)."""

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
    # Emails whose legacy history this principal may claim: reserved by it, not contested.
    history_emails: frozenset[str] = frozenset()


_COLUMNS = "id, issuer, sub, kind, email"


class PrincipalRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def get(self, issuer: str, sub: str) -> Principal | None:
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            return self._find(conn, issuer, sub)

    def email_state(self, email: str) -> tuple[str, bool] | None:
        """``(reserving principal id, contested)`` for an address, or None when unreserved."""
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            row = conn.execute(
                "SELECT principal_id, contested FROM principal_emails WHERE email = ?", (email,)
            ).fetchone()
            return (row["principal_id"], bool(row["contested"])) if row else None

    def ensure_user(self, issuer: str, sub: str, email: str | None) -> Principal:
        """The user principal for a verified ``(issuer, sub)``; created on first login.

        Every email a principal presents is reserved first-come in the same
        transaction. A different principal presenting a reserved address marks it
        ``contested`` and reserves nothing; reservations survive an email change.
        """
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            found = self._find(conn, issuer, sub)
            if found and found.email == email and self._settled(conn, found.id, email):
                return found
            conn.execute("BEGIN IMMEDIATE")  # serializes logins, reservations and conflicts
            with conn:  # commits, or rolls back on error
                found = self._find(conn, issuer, sub, with_history=False)
                if found is None:
                    principal_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO principals (id, issuer, sub, kind, email, created_at) "
                        "VALUES (?, ?, ?, 'user', ?, ?)",
                        (principal_id, issuer, sub, email, time.time()),
                    )
                else:
                    principal_id = found.id
                    if found.email != email:
                        conn.execute(
                            "UPDATE principals SET email = ? WHERE id = ?", (email, principal_id)
                        )
                if email:
                    self._reserve(conn, principal_id, email)
            result = self._find(conn, issuer, sub)
            if result is None:  # written under the write lock above
                raise RuntimeError("principal missing after write")
            return result

    def grant_delegation(
        self,
        agent_id: str,
        human_id: str,
        space_ids: list[str],
        expires_at: float,
        mode: str = "run",
    ) -> str:
        delegation_id = uuid.uuid4().hex
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                conn.execute(
                    "INSERT INTO delegations "
                    "(id, agent_principal_id, human_principal_id, mode, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (delegation_id, agent_id, human_id, mode, expires_at, time.time()),
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

    def delegation_covers(
        self, agent_id: str, human_id: str, space_id: str, *, need: str = "run"
    ) -> bool:
        """Whether ``human_id`` currently lets ``agent_id`` ``need`` (``read`` or ``run``) in ``space_id``.

        A ``run`` delegation includes ``read``; a ``read`` delegation never allows ``run``.
        """
        if need not in ("read", "run"):
            raise ValueError(f"unsupported delegation need {need!r}; use 'read' or 'run'")
        modes = ("run",) if need == "run" else ("read", "run")
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            return (
                conn.execute(
                    "SELECT 1 FROM delegations d JOIN delegation_spaces s ON s.delegation_id = d.id "
                    "WHERE d.agent_principal_id = ? AND d.human_principal_id = ? "
                    "AND s.space_id = ? AND d.revoked_at IS NULL AND d.expires_at > ? "
                    f"AND d.mode IN ({', '.join('?' * len(modes))})",
                    (agent_id, human_id, space_id, time.time(), *modes),
                ).fetchone()
                is not None
            )

    @staticmethod
    def _reserve(conn: sqlite3.Connection, principal_id: str, email: str) -> None:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO principal_emails (email, principal_id, first_seen_at) "
            "VALUES (?, ?, ?)",
            (email, principal_id, time.time()),
        )
        if cursor.rowcount == 0:  # already reserved: by this principal, or a different one
            conn.execute(
                "UPDATE principal_emails SET contested = 1 WHERE email = ? AND principal_id != ?",
                (email, principal_id),
            )

    @staticmethod
    def _settled(conn: sqlite3.Connection, principal_id: str, email: str | None) -> bool:
        """True when this login would write nothing: the address is mine, or already contested."""
        if not email:
            return True
        row = conn.execute(
            "SELECT principal_id, contested FROM principal_emails WHERE email = ?", (email,)
        ).fetchone()
        return row is not None and (row["principal_id"] == principal_id or bool(row["contested"]))

    @staticmethod
    def _find(
        conn: sqlite3.Connection, issuer: str, sub: str, *, with_history: bool = True
    ) -> Principal | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM principals WHERE issuer = ? AND sub = ?", (issuer, sub)
        ).fetchone()
        if row is None:
            return None
        history = frozenset()
        if with_history:
            history = frozenset(
                r["email"]
                for r in conn.execute(
                    "SELECT email FROM principal_emails WHERE principal_id = ? AND contested = 0",
                    (row["id"],),
                )
            )
        return Principal(**{key: row[key] for key in row.keys()}, history_emails=history)

    @staticmethod
    def _require_ready(conn: sqlite3.Connection) -> None:
        (found,) = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('principals', 'principal_emails')"
        ).fetchone()
        if found != 2:
            raise PrincipalStoreNotReady


principal_repo = PrincipalRepository()
