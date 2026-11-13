"""Cooperative Android claim authority in the broker SQLite store.

All mutations and SDK checks hold the broker's flock before opening SQLite.
The source is explicit: a complete owner census covering both writers, not an
empty list inferred from registry membership or absence of running processes.
"""

import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


def _text(*values):
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError("claim identity required")


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid census time")
    return value


def _source(conn, guard_path, *, clock=time.time):
    row = conn.execute(
        "SELECT generation,complete,observed_at,max_age,aliases,guard_path,lock_dir "
        "FROM android_claim_source WHERE singleton=1"
    ).fetchone()
    if (
        row is None
        or row[1] != 1
        or not -5 <= _number(clock()) - _number(row[2]) <= row[3]
        or Path(row[5]).resolve() != Path(guard_path).resolve()
    ):
        raise ValueError("claim census unavailable")
    return row, json.loads(row[4])


def _canonical(aliases, alias):
    _text(alias)
    if alias not in aliases:
        raise ValueError("unknown claim identity")
    return aliases[alias]


def _available(conn, device_id, aliases, lock_dir, *, own_path=None):
    # Retain expired/verifying/quarantined occupancy until owner reconciliation.
    if conn.execute(
        "SELECT 1 FROM broker_devices d JOIN broker_groups g USING(lease_id) "
        "WHERE d.device_id=? AND g.state!='released'",
        (device_id,),
    ).fetchone():
        raise ValueError("device occupied by broker")
    for (reserved,) in conn.execute("SELECT reserved FROM broker_tickets WHERE state='offered'"):
        if device_id in json.loads(reserved):
            raise ValueError("device reserved by broker")
    if conn.execute("SELECT 1 FROM android_claims WHERE device_id=?", (device_id,)).fetchone():
        raise ValueError("device claimed by Android")
    directory = Path(lock_dir)
    if not directory.is_dir():
        raise ValueError("SDK lock census unavailable")
    paths = [*directory.glob("artemis-device-*.lock")]
    legacy = directory / "artemis-global-device.lock"
    if legacy.exists():
        paths.append(legacy)
    for path in paths:
        if own_path is not None and path.resolve() == Path(own_path).resolve():
            continue  # Existing same-path owner validation remains the SDK's job.
        owner = json.loads(path.read_text())
        if _canonical(aliases, owner["device_id"]) == device_id:
            raise ValueError("device owned by SDK")


def check_sdk_available(db_path, guard_path, alias, lock_dir, own_path):
    """Called ONLY while the SDK holds guard_path. Any uncertainty refuses."""
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        source, aliases = _source(conn, guard_path)
        if Path(source[6]).resolve() != Path(lock_dir).resolve():
            raise ValueError("SDK lock directory differs from census")
        _available(conn, _canonical(aliases, alias), aliases, lock_dir, own_path=own_path)


class ClaimLedger:
    def __init__(self, db_path, guard, registry, lock_dir, *, max_age=60, clock=time.time):
        if type(max_age) is not int or not 1 <= max_age <= 3600:
            raise ValueError("invalid census age")
        self.db_path, self.guard, self.registry = Path(db_path), guard, registry
        self.lock_dir, self.max_age, self.clock = Path(lock_dir), max_age, clock
        self.aliases = {
            alias: d.device_id for d in registry.devices() for alias in (d.device_id, *d.aliases)
        }
        with self.guard(), sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS android_claim_source ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL, "
                "complete INTEGER NOT NULL, observed_at REAL NOT NULL, max_age INTEGER NOT NULL, "
                "aliases TEXT NOT NULL, guard_path TEXT NOT NULL, lock_dir TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS android_claims ("
                "claim_id TEXT PRIMARY KEY, device_id TEXT NOT NULL UNIQUE, "
                "owner TEXT NOT NULL, run_id TEXT NOT NULL, reference TEXT NOT NULL)"
            )

    def _read_source(self, conn):
        source, aliases = _source(conn, self.guard.path, clock=self.clock)
        if (
            aliases != self.aliases
            or source[3] != self.max_age
            or Path(source[6]).resolve() != self.lock_dir.resolve()
        ):
            raise ValueError("claim census configuration changed")
        return source, aliases

    def read(self):
        # Broker already holds this SAME guard; FileGuard supports nesting.
        with self.guard(), sqlite3.connect(self.db_path) as conn:
            self._read_source(conn)
            return [r[0] for r in conn.execute("SELECT device_id FROM android_claims")]

    def census(self, *, generation, observed_at, complete, writers, claims):
        """Owner attests complete structured census; never erase known live claims.

        A new generation permits configuration refresh. A same-generation refresh
        must retain configuration and all claims. Explicit owned release removes
        claims; freshness expiration never does.
        """
        with self.guard(), sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                complete is not True
                or not isinstance(writers, list)
                or sorted(writers) != ["android", "artemis"]
            ):
                conn.execute("UPDATE android_claim_source SET complete=0")
                conn.commit()
                raise ValueError("complete census of both writers required")
            if (
                type(generation) is not int
                or generation < 1
                or not isinstance(claims, list)
                or not -5 <= self.clock() - _number(observed_at) <= self.max_age
            ):
                raise ValueError("invalid census")
            prior = conn.execute(
                "SELECT generation,aliases,guard_path,lock_dir FROM android_claim_source"
            ).fetchone()
            config = (
                json.dumps(self.aliases, sort_keys=True),
                str(self.guard.path.resolve()),
                str(self.lock_dir.resolve()),
            )
            if prior and (
                generation < prior[0] or (generation == prior[0] and tuple(prior[1:]) != config)
            ):
                raise ValueError("stale census generation")
            incoming = {}
            for claim in claims:
                if not isinstance(claim, dict) or set(claim) != {
                    "claim_id",
                    "device_id",
                    "owner",
                    "run_id",
                    "reference",
                }:
                    raise ValueError("invalid census claim")
                _text(*claim.values())
                row = tuple(
                    claim[k] for k in ("claim_id", "device_id", "owner", "run_id", "reference")
                )
                if row[0] in incoming or _canonical(self.aliases, row[1]) != row[1]:
                    raise ValueError("invalid census identity")
                incoming[row[0]] = row
            existing = {r[0]: tuple(r) for r in conn.execute("SELECT * FROM android_claims")}
            if any(incoming.get(key) != row for key, row in existing.items()):
                raise ValueError("census cannot discard owned claims")
            for key, row in incoming.items():
                if key not in existing:
                    _available(conn, row[1], self.aliases, self.lock_dir)
                    conn.execute("INSERT INTO android_claims VALUES (?,?,?,?,?)", row)
            conn.execute(
                "INSERT OR REPLACE INTO android_claim_source VALUES (1,?,1,?,?,?,?,?)",
                (generation, observed_at, self.max_age, *config),
            )
            return {"generation": generation, "claims": len(incoming)}

    def claim(self, alias, *, owner, run_id, reference):
        _text(owner, run_id, reference)
        with self.guard(), sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, aliases = self._read_source(conn)
            device = _canonical(aliases, alias)
            _available(conn, device, aliases, self.lock_dir)
            claim_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO android_claims VALUES (?,?,?,?,?)",
                (claim_id, device, owner, run_id, reference),
            )
            return claim_id

    def release(self, claim_id, *, owner, run_id):
        _text(claim_id, owner, run_id)
        with self.guard(), sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                "DELETE FROM android_claims WHERE claim_id=? AND owner=? AND run_id=?",
                (claim_id, owner, run_id),
            )
            if result.rowcount != 1:
                raise ValueError("claim not yours")
