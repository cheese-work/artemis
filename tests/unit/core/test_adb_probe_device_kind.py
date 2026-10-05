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

"""AdbDeviceProbe classifies devices by adb properties, not by serial text."""

import asyncio

import pytest

from artemis.core.diagnostics.probes.adb_probe import AdbDeviceProbe

PHONE_GETPROP = (
    "[ro.product.model]: [21081111RG]\n"
    "[ro.hardware]: [qcom]\n"
    "[ro.kernel.qemu]: []\n"
    "[ro.build.version.release]: [12]\n"
)
EMULATOR_GETPROP = (
    "[ro.product.model]: [sdk_gphone64_arm64]\n"
    "[ro.hardware]: [ranchu]\n"
    "[ro.kernel.qemu]: [1]\n"
    "[ro.build.version.release]: [14]\n"
)


class _FakeProc:
    returncode = 0

    def __init__(self, stdout: bytes):
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


def _make_probe(monkeypatch, devices_output: str, props_by_serial: dict[str, str]):
    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(devices_output.encode())

    async def fake_shell(_adb, serial, *args, **_kwargs):
        if args[:1] == ("getprop",):
            return props_by_serial.get(serial, "")
        return ""

    async def fake_lock(*_args, **_kwargs):
        return False

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    probe = AdbDeviceProbe()
    monkeypatch.setattr(probe, "_run_adb_shell", fake_shell)
    monkeypatch.setattr(probe, "_get_dashboard_lock_state", fake_lock)
    return probe


async def _parse(monkeypatch, devices_output: str, props_by_serial: dict[str, str]):
    probe = _make_probe(monkeypatch, devices_output, props_by_serial)
    return {d.serial: d for d in await probe._parse_adb_devices("adb")}


@pytest.mark.asyncio
async def test_browser_phone_on_loopback_serial_is_a_phone(monkeypatch):
    devices = await _parse(
        monkeypatch,
        "List of devices attached\n127.0.0.1:36411 device model:21081111RG\n",
        {"127.0.0.1:36411": PHONE_GETPROP},
    )
    phone = devices["127.0.0.1:36411"]
    assert phone.device_kind == "phone"
    assert phone.is_emulator is False
    assert phone.model == "21081111RG"
    assert phone.android_version == "12"


@pytest.mark.asyncio
async def test_real_emulator_is_an_emulator(monkeypatch):
    devices = await _parse(
        monkeypatch,
        "List of devices attached\nemulator-5554 device\n",
        {"emulator-5554": EMULATOR_GETPROP},
    )
    emulator = devices["emulator-5554"]
    assert emulator.device_kind == "emulator"
    assert emulator.is_emulator is True


@pytest.mark.asyncio
async def test_unreadable_properties_are_unknown_not_emulator(monkeypatch):
    devices = await _parse(
        monkeypatch,
        "List of devices attached\n127.0.0.1:40001 device\nemulator-5556 unauthorized\n",
        {},
    )
    for serial in ("127.0.0.1:40001", "emulator-5556"):
        assert devices[serial].device_kind == "unknown"
        assert devices[serial].is_emulator is False
        assert devices[serial].model is None


@pytest.mark.asyncio
async def test_failed_property_read_is_retried_not_cached(monkeypatch):
    serial = "127.0.0.1:40002"
    props: dict[str, str] = {}
    probe = _make_probe(monkeypatch, f"List of devices attached\n{serial} device\n", props)

    first = await probe._parse_adb_devices("adb")
    assert first[0].device_kind == "unknown"

    props[serial] = PHONE_GETPROP
    second = await probe._parse_adb_devices("adb")
    assert second[0].device_kind == "phone"
