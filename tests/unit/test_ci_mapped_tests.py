"""The PR fast lane runs the admin_console tests that the changed paths map to (CHE-3276)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "ci_mapped_tests", ROOT / "scripts/ci_mapped_tests.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_changed_admin_console_test_maps_to_itself():
    mapped = _load().mapped_tests(["tests/unit/admin_console/test_drain.py"], ROOT)
    assert mapped == ["tests/unit/admin_console/test_drain.py"]


def test_a_changed_admin_console_module_maps_to_tests_named_after_it():
    mapped = _load().mapped_tests(["apps/admin_console/services/device_leases.py"], ROOT)
    assert "tests/unit/admin_console/test_device_leases.py" in mapped
    assert "tests/unit/admin_console/test_device_leases_api.py" in mapped
    assert all(path.startswith("tests/unit/admin_console/") for path in mapped)


def test_a_module_with_no_named_test_falls_back_to_the_route_and_access_tests():
    mapped = _load().mapped_tests(["apps/admin_console/core/unmatched_zzz.py"], ROOT)
    assert mapped == [
        "tests/unit/admin_console/test_access_control.py",
        "tests/unit/admin_console/test_route_tiers.py",
    ]


def test_paths_outside_admin_console_map_to_nothing():
    assert _load().mapped_tests(["artemis/core/agent.py", "README.md"], ROOT) == []


def test_a_deleted_test_file_is_not_selected():
    assert _load().mapped_tests(["tests/unit/admin_console/test_gone_zzz.py"], ROOT) == []


def test_a_module_maps_to_every_test_that_imports_it():
    """Auth regressions must not slip past the lane: test_security_boundary imports access_control."""
    mapped = _load().mapped_tests(["apps/admin_console/core/access_control.py"], ROOT)
    assert "tests/unit/admin_console/test_access_control.py" in mapped
    assert "tests/unit/admin_console/test_security_boundary.py" in mapped
    assert "tests/unit/admin_console/test_route_tiers.py" in mapped


def test_a_changed_shared_fixture_maps_to_the_whole_directory():
    mapped = _load().mapped_tests(["tests/unit/admin_console/conftest.py"], ROOT)
    assert mapped == ["tests/unit/admin_console"]
