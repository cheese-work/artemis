"""The two S6 fixture APKs are present, signed, and shaped as the restricted-mode check needs (CHE-2129)."""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

PACKAGES = Path(__file__).resolve().parents[3] / "packages" / "smartqa-fixture"
FIXTURE = "com.cheese.smartqa.fixture"
ESCAPE = "com.cheese.smartqa.fixture.escape"


def _entries(apk: str) -> dict[str, bytes]:
    with zipfile.ZipFile(PACKAGES / apk) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _mentions(blob: bytes, text: str) -> bool:
    return text.encode() in blob or text.encode("utf-16-le") in blob  # AXML pools are either


@pytest.mark.parametrize(
    ("apk", "package"),
    [("smartqa-fixture.apk", FIXTURE), ("smartqa-fixture-escape.apk", ESCAPE)],
)
def test_apk_is_signed_and_names_its_package(apk, package):
    entries = _entries(apk)
    assert any(name.startswith("META-INF/") and name.endswith(".RSA") for name in entries)
    assert _mentions(entries["AndroidManifest.xml"], package)
    assert "classes.dex" in entries


def test_fixture_has_the_open_escape_button_pointing_at_the_escape_package():
    dex = _entries("smartqa-fixture.apk")["classes.dex"]
    assert b"Open escape" in dex
    assert ESCAPE.encode() in dex


def test_escape_package_is_blank_with_one_activity():
    entries = _entries("smartqa-fixture-escape.apk")
    assert b"EscapeActivity" in entries["classes.dex"]
    assert b"Open escape" not in entries["classes.dex"]
