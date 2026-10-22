"""Wall-clock allowance for a child process that must import the app before it can act."""

import subprocess
import sys
import time

APP_IMPORT = "import apps.admin_console.server, mcp_server"


def startup_budget(floor: float, *, factor: float = 4.0) -> float:
    """Return ``max(floor, factor * T)``, where ``T`` is the time a no-op child takes to import the app.

    A fixed deadline on a child's first answer measures how busy the host is: the import alone costs
    seconds of CPU and stretches with CPU contention. Timing the same import in a probe child, right
    before the real one, scales the deadline with that contention. A child that never answers still
    exceeds ``factor`` times its own import time.
    """
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", APP_IMPORT], check=True, capture_output=True)
    return max(floor, factor * (time.monotonic() - started))
