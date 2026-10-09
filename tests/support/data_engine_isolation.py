"""Quiesce the process-wide DataEngine log path between tests."""

import logging
import sys
import time

from artemis.utils.logger import DataEngineHandler


def _handlers():
    loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
    return [
        handler
        for logger in loggers
        if isinstance(logger, logging.Logger)
        for handler in logger.handlers
        if isinstance(handler, DataEngineHandler)
    ]


def quiesce_data_engine_logs(timeout: float = 10.0) -> None:
    """Drop the current engine, then wait for in-flight and queued log callbacks.

    Clearing the global stops new records and makes the drain skip queued ones, but a callback
    that already captured the old engine keeps running into the next test's IPC mocks.
    """
    engine_mod = sys.modules.get("artemis.data_engine.engine")
    if engine_mod is not None:
        engine_mod._CURRENT_DATA_ENGINE = None
    deadline = time.monotonic() + timeout
    for handler in _handlers():
        while handler._log_queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
