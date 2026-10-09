"""Stop the process-wide awake service between tests."""

import threading
import time

from artemis.runtime.awake_service import screen_awake_service

THREAD_NAME_PREFIX = "artemis-awake-"


def stop_awake_service(timeout: float = 15.0) -> None:
    """Shut the service down and wait until every awake thread is gone.

    ``shutdown()`` and ``_stop_device()`` unregister a worker, then join it for 2 seconds only,
    but a heartbeat can sit in an adb call for up to 10. Such a survivor is no longer in the
    registry, so find the threads by name. One left alive would run ``subprocess.run`` inside
    the next test's patch, so raise rather than return while one is alive.
    """
    screen_awake_service.shutdown()
    deadline = time.monotonic() + timeout
    while True:
        alive = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith(THREAD_NAME_PREFIX)
            and thread is not threading.current_thread()
        ]
        if not alive:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"awake-service threads still running after {timeout}s: {[t.name for t in alive]}"
            )
        alive[0].join(0.05)
    screen_awake_service._shutdown_requested = False
