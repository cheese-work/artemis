"""Principal, email-history and delegation tables (CHE-1385; contract: docs/spaces-contract.md).

Additive and idempotent: nothing reads them while spaces are disabled.
"""

from __future__ import annotations

import sqlite3

DDL = (
    """
CREATE TABLE IF NOT EXISTS principals (
    id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    sub TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('user', 'agent', 'service')),
    email TEXT,
    created_at REAL NOT NULL,
    UNIQUE (issuer, sub)
)""",
    """
CREATE TABLE IF NOT EXISTS principal_emails (
    email TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    contested INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_principal_emails_principal ON principal_emails (principal_id)",
    """
CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    agent_principal_id TEXT NOT NULL REFERENCES principals(id),
    human_principal_id TEXT NOT NULL REFERENCES principals(id),
    mode TEXT NOT NULL CHECK (mode IN ('read', 'run')),
    expires_at REAL NOT NULL,
    revoked_at REAL,
    created_at REAL NOT NULL
)""",
    """
CREATE TABLE IF NOT EXISTS delegation_spaces (
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    space_id TEXT NOT NULL,
    PRIMARY KEY (delegation_id, space_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_delegations_agent ON delegations (agent_principal_id)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)
