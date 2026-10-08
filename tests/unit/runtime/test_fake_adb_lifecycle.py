"""The fake adb server's start/stop lifecycle (CHE-1094)."""

from __future__ import annotations

import threading

from tests.support.fake_adb import FakeAdbServer


def test_stop_before_the_serve_thread_runs_neither_hangs_nor_errors(monkeypatch):
    """``stop`` winning the race against the serve thread's first step must still return."""
    gate = threading.Event()
    real_thread = threading.Thread
    thread_errors: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))

    def delayed_thread(*args, **kwargs):
        target = kwargs.pop("target")

        def run_after_gate():
            gate.wait(timeout=5)
            target()

        return real_thread(*args, target=run_after_gate, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr("tests.support.fake_adb.threading.Thread", delayed_thread)
        server = FakeAdbServer("early-stop").start()

    import socketserver

    original_shutdown = socketserver.BaseServer.shutdown

    def release_then_shutdown(self):
        gate.set()  # the serve thread only gets to run once stop() is already underway
        original_shutdown(self)

    monkeypatch.setattr(socketserver.BaseServer, "shutdown", release_then_shutdown)

    stopper = real_thread(target=server.stop, daemon=True)
    stopper.start()
    stopper.join(timeout=3)

    assert not stopper.is_alive(), "stop() hung: the serve thread never reached serve_forever"
    assert thread_errors == []
