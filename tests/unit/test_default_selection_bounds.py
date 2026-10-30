"""The deterministic job has a 60 min budget: tests that rebuild the wheel stay out of it (CHE-2621)."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _addopts():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"][
        "addopts"
    ]


def test_default_selection_excludes_wheel_build_tests():
    opts = _addopts()
    selection = opts[opts.index("-m") + 1]
    assert "not wheel_build" in selection


def test_the_wheel_build_marker_is_registered_and_used():
    ini = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("wheel_build:") for m in ini["markers"])
    demo = (ROOT / "tests/unit/admin_console/test_preview_demo.py").read_text()
    assert "@pytest.mark.wheel_build" in demo
