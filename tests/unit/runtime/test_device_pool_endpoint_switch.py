"""An endpoint switch while discovery is in flight must not cross-contaminate caches (CHE-1094)."""

from __future__ import annotations

import asyncio

import pytest

from artemis.config import settings
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.device_pool import DevicePool

ALPHA = [("alpha-only", "device", "Alpha Phone", None)]
BETA = [("beta-only", "device", "Beta Phone", None)]


@pytest.fixture(autouse=True)
def isolated_locks(tmp_path, monkeypatch):
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _n: tmp_path)


def _select(monkeypatch, port: int) -> None:
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", port)


def _cache(pool: DevicePool) -> dict[str, object]:
    return {identity: snap.raw for identity, snap in pool._snapshots.items()}


@pytest.mark.asyncio
async def test_a_switch_during_an_async_query_does_not_write_into_the_new_endpoints_cache(
    monkeypatch,
):
    pool = DevicePool(adb_path="adb-sentinel")
    started, release = asyncio.Event(), asyncio.Event()
    queried: list[str] = []

    async def query(timeout=None):
        queried.append(pool._endpoint().identity)
        if len(queried) == 1:
            started.set()
            await release.wait()
            return list(ALPHA)
        return list(BETA)

    monkeypatch.setattr(pool, "_query_adb_devices_async", query)
    _select(monkeypatch, 40001)
    first = asyncio.create_task(pool.list_devices_async())
    await started.wait()

    _select(monkeypatch, 40002)  # the preference moves while alpha's query is pending
    release.set()
    alpha_listing = await first
    beta_listing = await pool.list_devices_async()

    assert [d.serial for d in alpha_listing] == ["alpha-only"]
    assert [d.serial for d in beta_listing] == ["beta-only"]
    assert queried == ["tcp:127.0.0.1:40001", "tcp:127.0.0.1:40002"]
    assert _cache(pool) == {"tcp:127.0.0.1:40001": ALPHA, "tcp:127.0.0.1:40002": BETA}


@pytest.mark.asyncio
async def test_busy_state_of_an_interrupted_listing_uses_the_queried_endpoints_scope(monkeypatch):
    pool = DevicePool(adb_path="adb-sentinel")
    started, release = asyncio.Event(), asyncio.Event()

    async def query(timeout=None):
        started.set()
        await release.wait()
        return [("emulator-5554", "device", None, None)]

    monkeypatch.setattr(pool, "_query_adb_devices_async", query)
    _select(monkeypatch, 40001)
    lock = DeviceExecutionLock("emulator-5554", "run on alpha", lock_scope="tcp:127.0.0.1:40001")
    lock.acquire()
    try:
        first = asyncio.create_task(pool.list_devices_async())
        await started.wait()
        _select(monkeypatch, 40002)
        release.set()
        listing = await first
    finally:
        lock.release()

    assert listing[0].is_busy is True


def test_a_sync_listing_is_attributed_to_the_endpoint_it_started_on(monkeypatch):
    pool = DevicePool(adb_path="adb-sentinel")
    _select(monkeypatch, 40001)

    def query(timeout=None):
        _select(monkeypatch, 40002)  # preference changes while adb answers
        return list(ALPHA)

    monkeypatch.setattr(pool, "_query_adb_devices_sync", query)

    pool.list_devices()

    assert _cache(pool) == {"tcp:127.0.0.1:40001": ALPHA}


@pytest.mark.asyncio
async def test_a_stale_fallback_after_a_failed_query_stays_on_the_queried_endpoint(monkeypatch):
    pool = DevicePool(adb_path="adb-sentinel")
    _select(monkeypatch, 40001)
    pool._store_snapshot(ALPHA)  # warm alpha's snapshot
    pool._snapshot().at -= pool.CACHE_TTL + 1  # stale but inside the error window

    async def failing(timeout=None):
        _select(monkeypatch, 40002)
        return None

    monkeypatch.setattr(pool, "_query_adb_devices_async", failing)

    listing = await pool.list_devices_async()

    assert [d.serial for d in listing] == ["alpha-only"]
    assert _cache(pool).get("tcp:127.0.0.1:40002") is None
