import logging
import threading
import time
from types import SimpleNamespace

import pytest

from artemis.data_engine import engine as engine_mod
from artemis.utils.logger import DataEngineHandler
from tests.support.data_engine_isolation import quiesce_data_engine_logs


@pytest.fixture
def blocked_drain_callback():
    """A real drain thread stuck in ``record_trace`` of an engine it already captured."""
    entered, release = threading.Event(), threading.Event()

    def record_trace(**_):
        entered.set()
        release.wait(5)

    engine = SimpleNamespace(current_session_id="s", record_trace=record_trace)
    handler = DataEngineHandler()
    logger = logging.getLogger("test_data_engine_isolation")
    logger.addHandler(handler)
    engine_mod._CURRENT_DATA_ENGINE = engine
    logger.warning("in flight")
    assert entered.wait(5)
    try:
        yield release
    finally:
        release.set()
        logger.removeHandler(handler)
        handler._log_queue.join()


def test_quiesce_waits_for_a_drain_callback_that_captured_the_old_engine(blocked_drain_callback):
    done = threading.Event()
    threading.Thread(target=lambda: (quiesce_data_engine_logs(), done.set())).start()

    deadline = time.monotonic() + 5
    while engine_mod._CURRENT_DATA_ENGINE is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert engine_mod._CURRENT_DATA_ENGINE is None
    assert not done.wait(0.2), "quiesce returned while the old engine's callback was running"

    blocked_drain_callback.set()
    assert done.wait(5)


def test_quiesce_fails_instead_of_returning_while_a_callback_is_unfinished(blocked_drain_callback):
    with pytest.raises(RuntimeError, match="still running"):
        quiesce_data_engine_logs(timeout=0.05)
