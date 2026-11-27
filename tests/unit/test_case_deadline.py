import os
import signal
import subprocess
import sys
import time
import types
import unittest
from unittest.mock import patch

from tests.support import case_deadline as module


class CaseDeadlineTests(unittest.TestCase):
    @unittest.skipUnless(module.deadline_supported(), "needs SIGALRM and the main thread")
    def test_deadline_interrupts_a_blocked_read_and_restores_the_handler(self):
        before = signal.getsignal(signal.SIGALRM)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        started = time.monotonic()
        with self.assertRaises(AssertionError):
            with module.case_deadline(1):
                os.read(read_fd, 1)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIs(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_without_setitimer_nothing_is_armed(self):
        # No SIGALRM machinery at all, as on Windows: the context must be a no-op.
        without = types.SimpleNamespace(SIGALRM=signal.SIGALRM)
        with patch.object(module, "signal", without):
            self.assertFalse(module.deadline_supported())
            with module.case_deadline(1):
                pass

    @unittest.skipUnless(module.deadline_supported(), "needs SIGALRM and the main thread")
    def test_until_raises_case_reached_once_and_leaves_no_timer(self):
        flag = {"hit": False}
        with self.assertRaises(module.CaseReached):
            with module.case_deadline(5, until=lambda: flag["hit"], poll=0.05):
                time.sleep(0.1)
                flag["hit"] = True
                time.sleep(2)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    @unittest.skipUnless(sys.platform.startswith("linux"), "process groups")
    def test_owned_workers_kills_a_live_worker_group_and_reaps_it(self):
        original = subprocess.Popen
        with module.owned_workers() as workers:
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
            )
            self.assertEqual(len(workers), 1)
            self.assertIsNone(workers[0].poll())
        self.assertIsNotNone(workers[0].returncode)
        self.assertIs(subprocess.Popen, original)


if __name__ == "__main__":
    unittest.main()
