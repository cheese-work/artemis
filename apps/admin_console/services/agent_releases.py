"""Durable release store for the host agent: canary and stable channels, halt and revoke."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal

from apps.admin_console.database.connection import get_db
from artemis.runtime.host_protocol import CONTRACT

MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
AUTO_HALT_HOSTS = 2
AUTO_HALT_WINDOW_SECONDS = 30 * 60
AUTO_HALT_REASON = "auto_halt"
TARGETS = frozenset({("linux", "amd64"), ("darwin", "arm64"), ("windows", "amd64")})

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")

Channel = Literal["none", "canary", "stable"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_releases (
    version TEXT NOT NULL PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    component TEXT NOT NULL CHECK (component = 'agent'),
    channel TEXT NOT NULL CHECK (channel IN ('none', 'canary', 'stable')),
    status TEXT NOT NULL CHECK (status IN ('active', 'halted', 'revoked')),
    halt_reason TEXT,
    created_at REAL NOT NULL,
    canary_at REAL,
    stable_at REAL,
    halted_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS agent_release_artifacts (
    version TEXT NOT NULL,
    os TEXT NOT NULL,
    arch TEXT NOT NULL,
    url TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (version, os, arch)
);
CREATE TABLE IF NOT EXISTS agent_release_failures (
    version TEXT NOT NULL,
    host_id TEXT NOT NULL,
    reported_at REAL NOT NULL,
    PRIMARY KEY (version, host_id)
)
"""


class ReleaseError(Exception):
    """Base class for release store failures."""


class InvalidRelease(ReleaseError):
    """The version or an artifact is malformed."""


class ReleaseConflict(ReleaseError):
    """The version is not greater than every stored version."""


class ReleaseNotFound(ReleaseError):
    """No release has this version."""


class ReleaseStateError(ReleaseError):
    """The requested transition is not allowed from the release's current state."""


@dataclass(frozen=True)
class ReleaseArtifact:
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    sequence: int
    channel: Literal["canary", "stable"]
    artifact: ReleaseArtifact | None
    revoked_versions: list[str]
    min_supported: int


def _parse_version(version: object) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise InvalidRelease("version must be MAJOR.MINOR.PATCH")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _validate_artifact(item: Mapping[str, object]) -> tuple[str, str, str, int, str]:
    os_, arch = item.get("os"), item.get("arch")
    if not isinstance(os_, str) or not isinstance(arch, str) or (os_, arch) not in TARGETS:
        raise InvalidRelease(f"unsupported target {os_!r}/{arch!r}")
    url, size, sha256 = item.get("url"), item.get("size"), item.get("sha256")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise InvalidRelease("artifact url must start with https://")
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_ARTIFACT_BYTES:
        raise InvalidRelease(f"artifact size must be 1 to {MAX_ARTIFACT_BYTES} bytes")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise InvalidRelease("artifact sha256 must be 64 lowercase hex characters")
    return os_, arch, url, size, sha256


class AgentReleaseStore:
    def __init__(self, db_path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self._db_path = db_path
        self.clock = clock
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA.split(";"):
                conn.execute(statement)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(get_db(self._db_path)) as conn, conn:
            yield conn

    @staticmethod
    def _release(conn: sqlite3.Connection, version: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM agent_releases WHERE version = ?", (version,)).fetchone()
        if row is None:
            raise ReleaseNotFound(version)
        return row

    def create_draft(self, version: str, artifacts: Sequence[Mapping[str, object]]) -> None:
        parsed = _parse_version(version)
        rows = [_validate_artifact(item) for item in artifacts]
        if len({(row[0], row[1]) for row in rows}) != len(rows):
            raise InvalidRelease("duplicate os/arch artifact")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stored = conn.execute("SELECT version, sequence FROM agent_releases").fetchall()
            if any(_parse_version(row["version"]) >= parsed for row in stored):
                raise ReleaseConflict(f"{version} is not greater than every stored version")
            sequence = max((row["sequence"] for row in stored), default=0) + 1
            conn.execute(
                "INSERT INTO agent_releases (version, sequence, component, channel, status, "
                "created_at) VALUES (?, ?, 'agent', 'none', 'active', ?)",
                (version, sequence, self.clock()),
            )
            conn.executemany(
                "INSERT INTO agent_release_artifacts (version, os, arch, url, size, sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(version, os_, arch, url, size, sha) for os_, arch, url, size, sha in rows],
            )

    def promote(self, version: str, channel: Channel) -> None:
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            release = self._release(conn, version)
            if release["status"] != "active":
                raise ReleaseStateError(f"{version} is {release['status']}")
            if (release["channel"], channel) not in (("none", "canary"), ("canary", "stable")):
                raise ReleaseStateError(
                    f"cannot promote {version} from {release['channel']} to {channel}"
                )
            column = "canary_at" if channel == "canary" else "stable_at"
            conn.execute(
                f"UPDATE agent_releases SET channel = ?, {column} = ? WHERE version = ?",
                (channel, self.clock(), version),
            )

    def halt(self, version: str, reason: str) -> None:
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._release(conn, version)
            self._halt(conn, version, reason)

    def _halt(self, conn: sqlite3.Connection, version: str, reason: str) -> bool:
        cursor = conn.execute(
            "UPDATE agent_releases SET status = 'halted', halt_reason = ?, halted_at = ? "
            "WHERE version = ? AND status = 'active'",
            (reason, self.clock(), version),
        )
        return cursor.rowcount == 1

    def revoke(self, version: str) -> None:
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._release(conn, version)
            conn.execute(
                "UPDATE agent_releases SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?) "
                "WHERE version = ?",
                (self.clock(), version),
            )

    def latest(
        self, channel: Literal["canary", "stable"], os: str, arch: str
    ) -> ReleaseManifest | None:
        channels = ("stable",) if channel == "stable" else ("canary", "stable")
        marks = ",".join("?" * len(channels))
        with self._db() as conn:
            release = conn.execute(
                f"SELECT version, sequence FROM agent_releases WHERE status = 'active' "
                f"AND channel IN ({marks}) ORDER BY sequence DESC LIMIT 1",
                channels,
            ).fetchone()
            if release is None:
                return None
            row = conn.execute(
                "SELECT url, size, sha256 FROM agent_release_artifacts "
                "WHERE version = ? AND os = ? AND arch = ?",
                (release["version"], os, arch),
            ).fetchone()
            revoked = [
                r["version"]
                for r in conn.execute("SELECT version FROM agent_releases WHERE status = 'revoked'")
            ]
        return ReleaseManifest(
            version=release["version"],
            sequence=release["sequence"],
            channel=channel,
            artifact=None
            if row is None
            else ReleaseArtifact(row["url"], row["size"], row["sha256"]),
            revoked_versions=sorted(revoked, key=_parse_version),
            min_supported=CONTRACT.minimum_version,
        )

    def report_failure(self, version: str, host_id: str) -> bool:
        """Record a failed update; return True when this report auto-halted the release."""
        now = self.clock()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._release(conn, version)
            conn.execute(
                "INSERT INTO agent_release_failures (version, host_id, reported_at) VALUES (?, ?, ?) "
                "ON CONFLICT(version, host_id) DO UPDATE SET reported_at = excluded.reported_at",
                (version, host_id, now),
            )
            (hosts,) = conn.execute(
                "SELECT COUNT(*) FROM agent_release_failures WHERE version = ? AND reported_at >= ?",
                (version, now - AUTO_HALT_WINDOW_SECONDS),
            ).fetchone()
            return hosts >= AUTO_HALT_HOSTS and self._halt(conn, version, AUTO_HALT_REASON)
