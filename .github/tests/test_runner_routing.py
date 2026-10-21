"""Regression check for runner billing and fork-safe fallbacks."""

from pathlib import Path
import unittest


class RunnerRoutingTest(unittest.TestCase):
    def test_artemis_uses_x99_and_provisions_chrome(self):
        ci = Path(".github/workflows/ci.yml").read_text()
        whats_new = Path(".github/workflows/whats-new.yml").read_text()
        self.assertEqual(ci.count("github.repository_owner == 'cheese-work'"), 3)
        self.assertIn("cheese-x99", whats_new)
        self.assertIn("browser-actions/setup-chrome@", ci)
        self.assertIn("CHROME_BIN: ${{ steps.chrome.outputs.chrome-path }}", ci)
        self.assertNotIn('export CHROME_BIN="$(command -v', ci)
        self.assertEqual(ci.count("apt-get install -y --no-install-recommends libxcb1 libgl1"), 2)
        self.assertIn("chrome-sandbox", ci)
        self.assertIn("chown root:root", ci)
        self.assertIn("chmod 4755", ci)

    def test_no_paid_runner_selectors(self):
        workflows = list(Path(".github/workflows").glob("*.y*ml"))
        self.assertTrue(workflows, "No workflows found")
        for workflow in workflows:
            with self.subTest(workflow=workflow.name):
                for line in workflow.read_text().splitlines():
                    if not line.lstrip().startswith("#"):
                        self.assertNotIn("blacksmith-", line)


if __name__ == "__main__":
    unittest.main()
