#!/usr/bin/env python3
"""Print the admin_console tests that the changed paths map to, one per line.

The PR fast lane skips tests/unit/admin_console (74% of suite time). A PR that
changes admin_console code or tests still runs the matching tests (CHE-3276).
Usage: git diff --name-only <base> HEAD | python scripts/ci_mapped_tests.py

- A changed test file maps to itself.
- A changed apps/admin_console module maps to tests named after it
  (test_<stem>*.py or test_*<stem>*.py).
- A changed module with no such test maps to FALLBACK.
Stdlib only.
"""

import sys
from pathlib import Path

TEST_DIR = "tests/unit/admin_console"
SOURCE_DIR = "apps/admin_console/"
FALLBACK = (f"{TEST_DIR}/test_access_control.py", f"{TEST_DIR}/test_route_tiers.py")


def mapped_tests(changed, root):
    root = Path(root)
    selected = set()
    for path in changed:
        if path.startswith(TEST_DIR + "/") and path.endswith(".py"):
            if (root / path).is_file() and Path(path).name.startswith("test_"):
                selected.add(path)
        elif path.startswith(SOURCE_DIR) and path.endswith(".py") and Path(path).stem != "__init__":
            named = {
                found.relative_to(root).as_posix()
                for found in (root / TEST_DIR).rglob(f"test_*{Path(path).stem}*.py")
            }
            selected |= named or set(FALLBACK)
    return sorted(selected)


if __name__ == "__main__":
    print("\n".join(mapped_tests(sys.stdin.read().split(), Path(__file__).resolve().parents[1])))
