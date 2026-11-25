"""Wall-clock bound for one test case, and cleanup of the worker processes it starts.

``case_deadline`` interrupts a blocked call (``os.read``, ``readline``, ``wait``) with a
``SIGALRM`` timer, so a hung case fails in seconds instead of holding the job until the CI
timeout. It needs ``SIGALRM``, ``setitimer`` and the main thread; elsewhere it is a no-op.
Every import is available on every platform; POSIX-only calls happen at run time only.
"""

import contextlib
import os
import signal
import subprocess
import threading
import time
from unittest import mock


class CaseTimeout(BaseException):
    """The deadline passed. Re-raised as ``AssertionError`` when ``case_deadline`` exits."""


class CaseReached(BaseException):
    """``until()`` became true. Propagates out of ``case_deadline`` unchanged."""


def deadline_supported():
    return (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )


@contextlib.contextmanager
def case_deadline(limit, *, until=None, poll=0.25):
    """Raise ``CaseReached`` once ``until()`` is true, else ``CaseTimeout`` after ``limit`` s.

    Raised at most once: the timer is cleared before the exception, so cleanup code that
    runs in the interrupted frame's ``except``/``finally`` is not interrupted again.
    """
    if not deadline_supported():
        yield
        return
    started = time.monotonic()
    done = False

    def tick(signum, frame):
        nonlocal done
        if done:
            return
        remaining = limit - (time.monotonic() - started)
        if until is not None and until():
            done = True
            raise CaseReached()
        if remaining <= 0:
            done = True
            raise CaseTimeout(f"case exceeded {limit} s")
        signal.setitimer(signal.ITIMER_REAL, max(0.001, min(poll, remaining)))

    previous = signal.signal(signal.SIGALRM, tick)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, min(poll, limit)))
    try:
        yield
    except CaseTimeout as exc:
        raise AssertionError(str(exc)) from None
    finally:
        done = True
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous if previous is not None else signal.SIG_DFL)


@contextlib.contextmanager
def owned_workers():
    """Record every ``subprocess.Popen`` started inside; SIGKILL the live ones' groups on exit.

    Workers are expected to run with ``start_new_session=True`` (pid == process group id).
    Yields the list of recorded ``Popen`` objects.
    """
    workers = []
    real = subprocess.Popen

    class Recorded(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            workers.append(self)

    try:
        with mock.patch.object(subprocess, "Popen", Recorded):
            yield workers
    finally:
        for proc in workers:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
