from unittest.mock import patch

from tests.support import startup_budget as module


def _budget(probe_seconds, floor):
    clock = iter([100.0, 100.0 + probe_seconds])
    with (
        patch.object(module.subprocess, "run"),
        patch.object(module.time, "monotonic", lambda: next(clock)),
    ):
        return module.startup_budget(floor)


def test_a_quick_import_keeps_the_floor():
    assert _budget(0.5, floor=6.0) == 6.0


def test_a_slow_import_stretches_the_deadline_with_the_measured_time():
    assert _budget(10.0, floor=6.0) == 40.0
