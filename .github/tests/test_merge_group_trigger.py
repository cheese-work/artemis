"""Every required-check workflow must also run on `merge_group` (CHE-3638).

A merge queue waits for the required checks on the queue commit. A required
workflow without the trigger never starts there, and the queue stalls.
"""
from pathlib import Path
import re
import unittest

# Workflows that report a ruleset-required check. Keep in step with the rulesets.
REQUIRED = ["ci.yml"]


def trigger_block(text):
    match = re.search(r"^on:\n((?:[ \t]+.*\n|\n)+)", text, re.M)
    return match.group(1) if match else ""


class MergeGroupTriggerTest(unittest.TestCase):
    def test_required_workflows_run_on_merge_group(self):
        for name in REQUIRED:
            with self.subTest(workflow=name):
                text = Path(".github/workflows", name).read_text()
                self.assertRegex(trigger_block(text), r"(?m)^  merge_group:", f"{name}: no merge_group trigger")


if __name__ == "__main__":
    unittest.main()
