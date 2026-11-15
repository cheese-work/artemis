"""Keep the repository's workflows compliant with its vendored CI policy."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CiPolicyTest(unittest.TestCase):
    def test_repository_workflows_satisfy_policy(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / ".github/ci-policy/check.py"),
                "--root",
                str(ROOT),
                "--repo",
                "cheese-work/artemis",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
