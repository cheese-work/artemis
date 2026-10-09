"""Stop the process-wide awake service between tests."""

import threading
import time

from artemis.runtime.awake_service import screen_awake_service


def stop_awake_service(timeout: float = 15.0) -> None:
    """Shut the service down and wait until its threads are gone.

    ``shutdown()`` joins for 2 seconds only, but a heartbeat can sit in an adb call for up to 10.
    A survivor would run ``subprocess.run`` inside the next test's patch. Raise rather than
    return while one is alive.
    """
    service = screen_awake_service
    with service._lock:
        threads = [*service._heartbeat_threads.values(), service._monitor_thread]
    service.shutdown()
    deadline = time.monotonic() + timeout
    for thread in threads:
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
    service._shutdown_requested = False
    alive = [thread.name for thread in threads if thread is not None and thread.is_alive()]
    if alive:
        raise RuntimeError(f"awake-service threads still running after {timeout}s: {alive}")
