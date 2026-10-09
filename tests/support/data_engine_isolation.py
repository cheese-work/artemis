"""Quiesce the process-wide DataEngine log path between tests."""

import sys
import threading
import time

from artemis.utils.logger import DataEngineHandler


def _handlers():
    """Every live handler, found by its drain thread so one never attached to a logger counts."""
    targets = (getattr(thread, "_target", None) for thread in threading.enumerate())
    return [
        target.__self__
        for target in targets
        if getattr(target, "__func__", None) is DataEngineHandler._drain_queue
    ]


def quiesce_data_engine_logs(timeout: float = 10.0) -> None:
    """Drop the current engine, then wait for in-flight and queued log callbacks.

    Clearing the global stops new records and makes the drain skip queued ones, but a callback
    that already captured the old engine keeps running into the next test's IPC mocks.
    Raises if work is still unfinished at the deadline: returning would claim isolation we lack.
    """
    engine_mod = sys.modules.get("artemis.data_engine.engine")
    if engine_mod is not None:
        engine_mod._CURRENT_DATA_ENGINE = None
    deadline = time.monotonic() + timeout
    for handler in _handlers():
        while handler._log_queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        if handler._log_queue.unfinished_tasks:
            raise RuntimeError(
                f"DataEngine log callbacks still running after {timeout}s; "
                "the next test would share them"
            )
