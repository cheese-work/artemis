#!/usr/bin/env python3
"""CHE-4503: workflows that run PR checks must also run on `merge_group`.

The merge queue waits for the required checks on the queue commit; a workflow that never
triggers there leaves the queue hanging. Text checks only, no PyYAML (runners may lack it).
"""
import pathlib
import re
import sys

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / "workflows"
GATED = ["ci.yml"]


def on_block(text: str) -> str:
    m = re.search(r"^on:\n((?:[ \t]+.*\n|\n)+)", text, re.M)
    return m.group(1) if m else ""


def main() -> int:
    bad = []
    for name in GATED:
        text = (WORKFLOWS / name).read_text()
        on = on_block(text)
        if not re.search(r"^  merge_group:", on, re.M):
            bad.append(f"{name}: no merge_group trigger")
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
