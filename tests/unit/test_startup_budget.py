import os
import subprocess
import sys
from unittest.mock import patch

import pytest

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


def test_a_stalled_probe_fails_within_the_probe_limit_and_leaves_no_child(tmp_path):
    pid_file = tmp_path / "probe.pid"
    stall = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(5)"
    with (
        patch.object(module, "APP_IMPORT", stall),
        patch.object(module, "PROBE_TIMEOUT", 0.5, create=True),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        module.startup_budget(6.0)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_probe_import_uses_the_child_environment(monkeypatch):
    monkeypatch.setenv("ARTEMIS_STARTUP_PROBE_SENTINEL", "parent")
    probe = "import os; assert os.environ['ARTEMIS_STARTUP_PROBE_SENTINEL'] == 'child'"
    child_env = {"ARTEMIS_STARTUP_PROBE_SENTINEL": "child", "PATH": os.environ["PATH"]}
    if sys.platform == "win32":
        child_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    with patch.object(module, "APP_IMPORT", probe):
        assert module.startup_budget(0.01, env=child_env) >= 0.01
