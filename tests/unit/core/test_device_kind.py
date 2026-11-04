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

"""Device kind comes from adb properties, never from the serial text."""

import pytest

from artemis.utils.device_kind import DeviceKind, classify_properties, parse_getprop

PHONE_PROPS = {
    "ro.product.model": "21081111RG",
    "ro.product.name": "vayu_global",
    "ro.hardware": "qcom",
    "ro.kernel.qemu": "",
}
EMULATOR_PROPS = {
    "ro.product.model": "sdk_gphone64_arm64",
    "ro.hardware": "ranchu",
    "ro.kernel.qemu": "1",
}


def test_parse_getprop_reads_bracketed_pairs():
    out = "[ro.product.model]: [21081111RG]\n[ro.kernel.qemu]: []\nnoise line\n"
    assert parse_getprop(out) == {"ro.product.model": "21081111RG", "ro.kernel.qemu": ""}


def test_physical_properties_are_phone():
    assert classify_properties(PHONE_PROPS) is DeviceKind.PHONE


@pytest.mark.parametrize(
    "props",
    [
        EMULATOR_PROPS,
        {"ro.product.model": "x", "ro.boot.qemu": "1"},
        {"ro.product.model": "x", "ro.hardware": "goldfish"},
        {"ro.product.model": "x", "ro.boot.hardware": "ranchu"},
        {"ro.product.model": "x", "ro.product.name": "sdk_gphone64_x86_64"},
    ],
)
def test_qemu_markers_are_emulator(props):
    assert classify_properties(props) is DeviceKind.EMULATOR


def test_unreadable_properties_are_unknown():
    assert classify_properties({}) is DeviceKind.UNKNOWN
