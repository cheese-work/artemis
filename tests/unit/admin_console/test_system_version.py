# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deployed-version contract (CHE-1146): public, never fails, degrades to unknown."""

from datetime import datetime, UTC
import os

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.access_control import route_tier
from apps.admin_console.server import app

SHA = "52b9ed0a1b2c3d4e5f60718293a4b5c6d7e8f901"
VERSION = "/api/system/version"
UNKNOWN = {"status": "unknown", "sha": None, "short_sha": None, "deployed_at": None, "build": None}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("ARTEMIS_DEPLOYED_SHA", "ARTEMIS_DEPLOYED_AT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARTEMIS_DEPLOYED_SHA_FILE", str(tmp_path / "DEPLOYED_SHA"))
    return tmp_path / "DEPLOYED_SHA"


async def _get(peer=("203.0.113.9", 50000)):
    transport = ASGITransport(app=app, client=peer)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get(VERSION)


def test_version_route_is_public_tier():
    assert route_tier(VERSION, {"GET"}) == "public"


@pytest.mark.asyncio
async def test_file_with_sha_and_time(clean_env):
    clean_env.write_text(f"{SHA}\n2026-10-05T03:10:00Z\n")

    response = await _get()

    assert response.status_code == 200
    assert response.json() == {
        "status": "known",
        "sha": SHA,
        "short_sha": "52b9ed0",
        "deployed_at": "2026-10-05T03:10:00Z",
        "build": "20261005-1010",
    }


@pytest.mark.asyncio
async def test_file_without_time_falls_back_to_mtime(clean_env):
    clean_env.write_text(f"{SHA}\n")
    moment = datetime(2026, 10, 5, 17, 30, tzinfo=UTC).timestamp()
    os.utime(clean_env, (moment, moment))

    body = (await _get()).json()

    assert body["status"] == "known"
    assert body["short_sha"] == "52b9ed0"
    assert body["deployed_at"].endswith("Z")
    assert body["build"] == "20261006-0030"


@pytest.mark.asyncio
async def test_env_overrides_file(clean_env, monkeypatch):
    clean_env.write_text("deadbeefdeadbeef\n2020-01-01T00:00:00Z\n")
    monkeypatch.setenv("ARTEMIS_DEPLOYED_SHA", SHA)
    monkeypatch.setenv("ARTEMIS_DEPLOYED_AT", "2026-10-05T03:10:00Z")

    body = (await _get()).json()

    assert body["sha"] == SHA
    assert body["deployed_at"] == "2026-10-05T03:10:00Z"
    assert body["build"] == "20261005-1010"


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-10-05T18:42:00Z", "20261006-0142"),
        ("2026-10-05T23:42:00+05:00", "20261006-0142"),
        ("2026-12-31T20:59:00Z", "20270101-0359"),
        ("2026-10-05T03:10:00", "20261005-1010"),
        ("0001-01-01T00:00:00Z", "00010101-0700"),
    ],
)
@pytest.mark.asyncio
async def test_env_build_uses_ict(monkeypatch, stamp, expected):
    monkeypatch.setenv("ARTEMIS_DEPLOYED_SHA", SHA)
    monkeypatch.setenv("ARTEMIS_DEPLOYED_AT", stamp)

    response = await _get()

    assert response.status_code == 200
    assert response.json()["build"] == expected


@pytest.mark.parametrize(
    "stamp",
    [None, "not-a-time", "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00"],
)
@pytest.mark.asyncio
async def test_env_without_usable_time_has_no_build(monkeypatch, stamp):
    monkeypatch.setenv("ARTEMIS_DEPLOYED_SHA", SHA)
    if stamp is not None:
        monkeypatch.setenv("ARTEMIS_DEPLOYED_AT", stamp)

    response = await _get()

    assert response.status_code == 200
    assert response.json() == {
        "status": "known",
        "sha": SHA,
        "short_sha": "52b9ed0",
        "deployed_at": None,
        "build": None,
    }


@pytest.mark.asyncio
async def test_ict_conversion_overflow_keeps_utc_time_without_build(monkeypatch):
    monkeypatch.setenv("ARTEMIS_DEPLOYED_SHA", SHA)
    monkeypatch.setenv("ARTEMIS_DEPLOYED_AT", "9999-12-31T23:59:59Z")

    response = await _get()

    assert response.status_code == 200
    assert response.json()["deployed_at"] == "9999-12-31T23:59:59Z"
    assert response.json()["build"] is None


@pytest.mark.asyncio
async def test_missing_file_is_unknown_not_an_error():
    response = await _get()

    assert response.status_code == 200
    assert response.json() == UNKNOWN


@pytest.mark.parametrize("content", ["", "\n", "not-a-sha\n", "abc\n", "52b9ed0; rm -rf /\n"])
@pytest.mark.asyncio
async def test_malformed_content_is_unknown(clean_env, content):
    clean_env.write_text(content)

    assert (await _get()).json() == UNKNOWN


@pytest.mark.asyncio
async def test_malformed_time_is_dropped_not_echoed(clean_env):
    clean_env.write_text(f"{SHA}\n<script>alert(1)</script>\n")

    body = (await _get()).json()

    assert body["status"] == "known"
    assert body["deployed_at"] is None or body["deployed_at"].endswith("Z")
    assert "<script>" not in str(body)


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00"])
@pytest.mark.asyncio
async def test_out_of_range_time_degrades_to_mtime_not_500(clean_env, stamp):
    clean_env.write_text(f"{SHA}\n{stamp}\n")
    moment = datetime(2026, 10, 5, 3, 10, tzinfo=UTC).timestamp()
    os.utime(clean_env, (moment, moment))

    response = await _get()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "known"
    assert body["deployed_at"].endswith("Z")
    assert body["deployed_at"] != stamp
    assert body["build"] == "20261005-1010"


@pytest.mark.asyncio
async def test_env_time_survives_when_sha_comes_from_file(clean_env, monkeypatch):
    clean_env.write_text(f"{SHA}\n2026-10-05T03:10:00Z\n")
    monkeypatch.setenv("ARTEMIS_DEPLOYED_AT", "2026-10-05T05:00:00Z")

    body = (await _get()).json()

    assert body["sha"] == SHA
    assert body["deployed_at"] == "2026-10-05T05:00:00Z"
    assert body["build"] == "20261005-1200"
