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

"""Classify an Android device as phone, emulator or unknown from its adb properties.

The serial is only an address: a physical phone relayed through a browser shows
up as ``127.0.0.1:<port>``, so serial text says nothing about what the device is.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
import re

_PROP_LINE = re.compile(r"^\[(?P<key>[^\]]+)\]: \[(?P<value>.*)\]$")
_QEMU_HARDWARE = frozenset({"goldfish", "ranchu"})
_EMULATOR_PRODUCT_PREFIXES = ("sdk_", "emulator")


class DeviceKind(StrEnum):
    PHONE = "phone"
    EMULATOR = "emulator"
    UNKNOWN = "unknown"


def parse_getprop(output: str) -> dict[str, str]:
    """Parse ``adb shell getprop`` output (``[key]: [value]`` lines)."""
    props: dict[str, str] = {}
    for line in output.splitlines():
        match = _PROP_LINE.match(line.strip())
        if match:
            props[match["key"]] = match["value"]
    return props


def classify_properties(props: Mapping[str, str]) -> DeviceKind:
    """Return UNKNOWN when no property could be read, never a guess."""
    if not props:
        return DeviceKind.UNKNOWN
    if (
        props.get("ro.kernel.qemu") == "1"
        or props.get("ro.boot.qemu") == "1"
        or props.get("ro.hardware") in _QEMU_HARDWARE
        or props.get("ro.boot.hardware") in _QEMU_HARDWARE
        or props.get("ro.product.name", "").startswith(_EMULATOR_PRODUCT_PREFIXES)
    ):
        return DeviceKind.EMULATOR
    return DeviceKind.PHONE
