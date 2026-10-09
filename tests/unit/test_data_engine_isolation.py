import logging
import threading
from types import SimpleNamespace

from artemis.data_engine import engine as engine_mod
from artemis.utils.logger import DataEngineHandler
from tests.support.data_engine_isolation import quiesce_data_engine_logs


def test_quiesce_waits_for_a_drain_callback_that_captured_the_old_engine():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def record_trace(**_):
        entered.set()
        release.wait(5)
        finished.set()

    engine = SimpleNamespace(current_session_id="s", record_trace=record_trace)
    handler = DataEngineHandler()
    logger = logging.getLogger("test_data_engine_isolation")
    logger.addHandler(handler)
    try:
        engine_mod._CURRENT_DATA_ENGINE = engine
        logger.warning("in flight")
        assert entered.wait(5)

        done = threading.Event()
        threading.Thread(target=lambda: (quiesce_data_engine_logs(), done.set())).start()
        assert engine_mod._CURRENT_DATA_ENGINE is None
        assert not done.wait(0.2), "quiesce returned while the old engine's callback was running"

        release.set()
        assert done.wait(5)
        assert finished.is_set()
    finally:
        release.set()
        logger.removeHandler(handler)
