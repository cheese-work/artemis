#!/usr/bin/env python3
"""Print the admin_console tests that the changed paths map to, one per line.

The PR fast lane skips tests/unit/admin_console (74% of suite time). A PR that
changes admin_console code or tests still runs the matching tests (CHE-3276).
Usage: git diff --name-only <base> HEAD | python scripts/ci_mapped_tests.py

- A changed test file maps to itself.
- A changed apps/admin_console module maps to tests named after it
  (test_*<stem>*.py) and to every test that imports it, so a security module
  change also runs the tests that exercise it from another file.
- A changed module that no test names or imports maps to FALLBACK.
- A changed conftest.py or __init__.py in the test directory maps to the whole
  directory.
Stdlib only.
"""

import re
import sys
from pathlib import Path

TEST_DIR = "tests/unit/admin_console"
SOURCE_DIR = "apps/admin_console/"
FALLBACK = (f"{TEST_DIR}/test_access_control.py", f"{TEST_DIR}/test_route_tiers.py")


def _imports(test_file, stem):
    pattern = re.compile(rf"\b{re.escape(stem)}\b")
    lines = test_file.read_text(encoding="utf-8").splitlines()
    return any(
        pattern.search(line) for line in lines if line.lstrip().startswith(("import ", "from "))
    )


def mapped_tests(changed, root):
    root = Path(root)
    selected = set()
    for path in changed:
        name = Path(path).name
        if path.startswith(TEST_DIR + "/") and path.endswith(".py"):
            if name in ("conftest.py", "__init__.py"):
                return [TEST_DIR]
            if (root / path).is_file() and name.startswith("test_"):
                selected.add(path)
        elif path.startswith(SOURCE_DIR) and path.endswith(".py") and name != "__init__.py":
            stem = Path(path).stem
            tests = sorted((root / TEST_DIR).rglob("test_*.py"))
            related = {
                test.relative_to(root).as_posix()
                for test in tests
                if stem in test.stem or _imports(test, stem)
            }
            selected |= related or set(FALLBACK)
    return sorted(selected)


if __name__ == "__main__":
    print("\n".join(mapped_tests(sys.stdin.read().split(), Path(__file__).resolve().parents[1])))
