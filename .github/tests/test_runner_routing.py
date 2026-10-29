"""Regression check for runner billing and fork-safe fallbacks."""

from pathlib import Path
import unittest


class RunnerRoutingTest(unittest.TestCase):
    def test_artemis_uses_x99_and_provisions_chrome(self):
        ci = Path(".github/workflows/ci.yml").read_text()
        whats_new = Path(".github/workflows/whats-new.yml").read_text()
        self.assertEqual(ci.count("github.repository_owner == 'cheese-work'"), 4)
        self.assertIn("cheese-x99", whats_new)
        self.assertIn("browser-actions/setup-chrome@", ci)
        self.assertIn("CHROME_BIN: ${{ steps.chrome.outputs.chrome-path }}", ci)
        self.assertNotIn('export CHROME_BIN="$(command -v', ci)
        self.assertNotIn("chrome-sandbox", ci)
        self.assertNotIn("chown root:root", ci)
        self.assertNotIn("chmod 4755", ci)

    def test_only_frontend_requires_browser_runner(self):
        ci = Path(".github/workflows/ci.yml").read_text()
        frontend = ci.split("  frontend:\n", 1)[1].split("  package:\n", 1)[0]
        self.assertEqual(ci.count('"cheese-x99-browser"'), 1)
        self.assertIn(
            'fromJSON(\'["self-hosted","Linux","X64","cheese-x99","cheese-x99-browser"]\')',
            frontend,
        )
        self.assertIn("fromJSON('[\"ubuntu-latest\"]')", frontend)

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
