"""Device, connection, alias and event tables (CHE-1472; contract: docs/device-identity.md).

Additive: nothing reads them until the reconciler lands. ``device_id`` and
``connection_id`` are opaque; the ``{source, host_id, serial}`` tuple is only a
connection's routing key. ``device_events`` rows are history and never point at
a foreign key, so Merge and Split leave them as they are.
"""

from __future__ import annotations

from pathlib import Path

from artemis.data_engine import schema_revisions

MODULE = "devices"

REVISIONS: tuple[tuple[str, ...], ...] = (
    (
        """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    owner_principal_id TEXT REFERENCES principals(id),
    label TEXT,
    hardware_hash TEXT,
    match_state TEXT NOT NULL CHECK (match_state IN ('confirmed', 'uncertain', 'provisional')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_hardware_hash "
        "ON devices (hardware_hash) WHERE hardware_hash IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices (owner_principal_id)",
        """
CREATE TABLE IF NOT EXISTS device_connections (
    connection_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    kind TEXT NOT NULL CHECK (kind IN ('usb', 'wifi', 'bridge', 'avd')),
    source TEXT NOT NULL CHECK (source IN ('local', 'bridge', 'host')),
    host_id TEXT,
    serial TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL
)""",
        # NULL host_id (server adb) must still collide, hence ifnull().
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_connections_key "
        "ON device_connections (source, ifnull(host_id, ''), serial)",
        "CREATE INDEX IF NOT EXISTS idx_device_connections_device "
        "ON device_connections (device_id)",
        """
CREATE TABLE IF NOT EXISTS device_aliases (
    alias_device_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    created_at REAL NOT NULL
)""",
        """
CREATE TABLE IF NOT EXISTS device_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    connection_id TEXT,
    event_key TEXT UNIQUE,
    kind TEXT NOT NULL,
    payload TEXT,
    ts REAL NOT NULL
)""",
        "CREATE INDEX IF NOT EXISTS idx_device_events_device_ts ON device_events (device_id, ts)",
    ),
)


def migrate(db_path: str | Path) -> schema_revisions.RevisionReport:
    return schema_revisions.apply(db_path, MODULE, REVISIONS)
