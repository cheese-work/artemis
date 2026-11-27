"""Tests for the host-agent release store (channels, halt, revoke, auto-halt)."""

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any, cast

import pytest

from apps.admin_console.services.agent_releases import (
    AgentReleaseStore,
    InvalidRelease,
    ReleaseConflict,
    ReleaseNotFound,
    ReleaseStateError,
)
from artemis.runtime.host_protocol import CONTRACT

SHA = "a" * 64
LINUX = ("linux", "amd64")


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def artifact(os_: str = "linux", arch: str = "amd64", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "os": os_,
        "arch": arch,
        "url": f"https://dl.example/{os_}-{arch}",
        "size": 1024,
        "sha256": SHA,
    }
    base.update(overrides)
    return base


def all_targets() -> list[dict[str, object]]:
    return [artifact("linux", "amd64"), artifact("darwin", "arm64"), artifact("windows", "amd64")]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "releases.db"


@pytest.fixture
def store(db_path: Path, clock: Clock) -> AgentReleaseStore:
    return AgentReleaseStore(db_path=db_path, clock=clock)


def test_draft_is_not_served_until_canary(store: AgentReleaseStore) -> None:
    store.create_draft("1.0.0", all_targets())
    assert store.latest("canary", *LINUX) is None
    assert store.latest("stable", *LINUX) is None

    store.promote("1.0.0", "canary")

    manifest = store.latest("canary", *LINUX)
    assert manifest is not None
    assert manifest.version == "1.0.0"
    assert manifest.channel == "canary"
    assert manifest.artifact is not None
    assert (manifest.artifact.url, manifest.artifact.size, manifest.artifact.sha256) == (
        "https://dl.example/linux-amd64",
        1024,
        SHA,
    )
    assert manifest.revoked_versions == []
    assert manifest.min_supported == CONTRACT.minimum_version
    assert store.latest("stable", *LINUX) is None


def test_stable_needs_canary_first_and_channels_track_independently(
    store: AgentReleaseStore,
) -> None:
    store.create_draft("1.0.0", all_targets())
    with pytest.raises(ReleaseStateError):
        store.promote("1.0.0", "stable")

    store.promote("1.0.0", "canary")
    store.promote("1.0.0", "stable")
    for channel in ("canary", "stable"):
        manifest = store.latest(channel, *LINUX)
        assert manifest is not None
        assert manifest.version == "1.0.0"

    store.create_draft("1.1.0", all_targets())
    store.promote("1.1.0", "canary")
    canary = store.latest("canary", *LINUX)
    stable = store.latest("stable", *LINUX)
    assert canary is not None and canary.version == "1.1.0"
    assert canary.sequence > 1
    assert stable is not None and stable.version == "1.0.0"


def test_promote_rejects_unknown_version_and_repeat(store: AgentReleaseStore) -> None:
    with pytest.raises(ReleaseNotFound):
        store.promote("9.9.9", "canary")
    store.create_draft("1.0.0", all_targets())
    store.promote("1.0.0", "canary")
    with pytest.raises(ReleaseStateError):
        store.promote("1.0.0", "canary")
    with pytest.raises(ReleaseStateError):
        store.promote("1.0.0", cast(Any, "none"))


def test_create_draft_validation(store: AgentReleaseStore) -> None:
    store.create_draft("1.2.0", all_targets())
    for version in ("1.2.0", "1.1.9", "0.9.0"):
        with pytest.raises(ReleaseConflict):
            store.create_draft(version, all_targets())
    # 1.10.0 > 1.9.0 numerically, not lexically.
    store.create_draft("1.9.0", all_targets())
    store.create_draft("1.10.0", all_targets())

    bad: list[dict[str, object]] = [
        artifact(sha256="A" * 64),
        artifact(sha256="a" * 63),
        artifact(url="http://dl.example/x"),
        artifact("freebsd", "amd64"),
        artifact("linux", "arm64"),
        artifact(size=0),
        artifact(size=128 * 1024 * 1024 + 1),
    ]
    for item in bad:
        with pytest.raises(InvalidRelease):
            store.create_draft("2.0.0", [item])
    for version in ("2.0", "v2.0.0", "2.0.0-rc1", "dev", ""):
        with pytest.raises(InvalidRelease):
            store.create_draft(version, all_targets())
    with pytest.raises(InvalidRelease):
        store.create_draft("2.0.0", [artifact(), artifact()])  # duplicate target
    store.create_draft("2.0.0", [artifact(size=128 * 1024 * 1024)])


def test_missing_target_returns_manifest_without_artifact(store: AgentReleaseStore) -> None:
    store.create_draft("1.0.0", [artifact("linux", "amd64"), artifact("darwin", "arm64")])
    store.promote("1.0.0", "canary")

    manifest = store.latest("canary", "windows", "amd64")
    assert manifest is not None
    assert manifest.version == "1.0.0"
    assert manifest.artifact is None
    present = store.latest("canary", "darwin", "arm64")
    assert present is not None and present.artifact is not None


def test_halt_falls_back_and_revoke_is_listed_everywhere(store: AgentReleaseStore) -> None:
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        store.create_draft(version, all_targets())
        store.promote(version, "canary")
        store.promote(version, "stable")

    store.halt("1.2.0", "crash loop")
    latest = store.latest("stable", *LINUX)
    assert latest is not None and latest.version == "1.1.0"
    assert latest.revoked_versions == []

    store.revoke("1.1.0")
    for channel in ("stable", "canary"):
        manifest = store.latest(channel, *LINUX)
        assert manifest is not None
        assert manifest.version == "1.0.0"
        assert manifest.revoked_versions == ["1.1.0"]

    store.revoke("1.0.0")
    assert store.latest("stable", *LINUX) is None
    assert store.latest("canary", *LINUX) is None
    store.create_draft("1.3.0", all_targets())
    store.promote("1.3.0", "canary")
    manifest = store.latest("canary", *LINUX)
    assert manifest is not None
    assert manifest.version == "1.3.0"
    assert manifest.revoked_versions == ["1.0.0", "1.1.0"]  # sorted numerically


def test_halted_or_revoked_cannot_be_promoted_and_unknown_raises(
    store: AgentReleaseStore,
) -> None:
    store.create_draft("1.0.0", all_targets())
    store.create_draft("1.1.0", all_targets())
    store.promote("1.0.0", "canary")
    store.halt("1.0.0", "bad")
    with pytest.raises(ReleaseStateError):
        store.promote("1.0.0", "stable")
    store.revoke("1.1.0")
    with pytest.raises(ReleaseStateError):
        store.promote("1.1.0", "canary")
    for call in (lambda: store.halt("9.9.9", "x"), lambda: store.revoke("9.9.9")):
        with pytest.raises(ReleaseNotFound):
            call()


def test_auto_halt_needs_two_distinct_hosts_within_thirty_minutes(
    store: AgentReleaseStore, clock: Clock
) -> None:
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        store.create_draft(version, all_targets())
        store.promote(version, "canary")

    # Two hosts 1799 s apart halt the release.
    clock.now = 0
    assert store.report_failure("1.2.0", "host-a") is False
    clock.now = 1799
    assert store.report_failure("1.2.0", "host-b") is True
    latest = store.latest("canary", *LINUX)
    assert latest is not None and latest.version == "1.1.0"
    with store._db() as conn:
        row = conn.execute(
            "SELECT status, halt_reason, halted_at FROM agent_releases WHERE version = '1.2.0'"
        ).fetchone()
    assert (row["status"], row["halt_reason"], row["halted_at"]) == ("halted", "auto_halt", 1799)

    # 1801 s apart does not.
    clock.now = 0
    assert store.report_failure("1.1.0", "host-a") is False
    clock.now = 1801
    assert store.report_failure("1.1.0", "host-b") is False
    latest = store.latest("canary", *LINUX)
    assert latest is not None and latest.version == "1.1.0"

    # The same host twice does not, however often it reports.
    clock.now = 10
    assert store.report_failure("1.0.0", "host-a") is False
    clock.now = 20
    assert store.report_failure("1.0.0", "host-a") is False
    latest = store.latest("canary", *LINUX)
    assert latest is not None and latest.version == "1.1.0"


def test_report_failure_unknown_version_and_already_halted(
    store: AgentReleaseStore, clock: Clock
) -> None:
    with pytest.raises(ReleaseNotFound):
        store.report_failure("9.9.9", "host-a")
    store.create_draft("1.0.0", all_targets())
    store.promote("1.0.0", "canary")
    store.report_failure("1.0.0", "host-a")
    assert store.report_failure("1.0.0", "host-b") is True
    # A third host after the halt does not halt again.
    assert store.report_failure("1.0.0", "host-c") is False


def test_second_store_sees_same_releases_and_sequence_keeps_increasing(
    db_path: Path, clock: Clock
) -> None:
    first = AgentReleaseStore(db_path=db_path, clock=clock)
    first.create_draft("1.0.0", all_targets())
    first.promote("1.0.0", "canary")

    second = AgentReleaseStore(db_path=db_path, clock=clock)
    seen = second.latest("canary", *LINUX)
    assert seen is not None and seen.version == "1.0.0"
    second.create_draft("1.1.0", all_targets())
    second.promote("1.1.0", "canary")

    newest = first.latest("canary", *LINUX)
    assert newest is not None and newest.version == "1.1.0"
    assert newest.sequence > seen.sequence

    with closing(sqlite3.connect(db_path)) as conn:
        sequences = [r[0] for r in conn.execute("SELECT sequence FROM agent_releases ORDER BY 1")]
    assert sequences == sorted(set(sequences)) and len(sequences) == 2
