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

    def test_reverse_order_run_has_its_own_job_under_the_timeout(self):
        ci = Path(".github/workflows/ci.yml").read_text()
        quality = ci.split("  python-quality:\n", 1)[1].split("  python-reverse-order:\n", 1)[0]
        reverse = ci.split("  python-reverse-order:\n", 1)[1].split("  frontend:\n", 1)[0]
        self.assertNotIn("reverse file order", quality)
        self.assertIn("reverse file order", reverse)
        self.assertIn("name: Python 3.12\n", quality)
        self.assertIn("cheese-x99", reverse)
        self.assertIn("timeout-minutes: 60", reverse)
        # The shallow checkout needs the pinned CHE-844 receipt commits for
        # the receipt unit test; another job's fetch does not carry over.
        self.assertIn("Fetch CHE-844 receipt sources", reverse)
        self.assertLess(
            reverse.index("Fetch CHE-844 receipt sources"),
            reverse.index("reverse file order"),
        )

    def test_python_jobs_keep_their_test_report_when_the_job_log_is_lost(self):
        # GitHub returned 404 for the long Python job logs (CHE-2621), so a
        # failing test could not be named. The report lives in an artifact.
        ci = Path(".github/workflows/ci.yml").read_text()
        quality = ci.split("  python-quality:\n", 1)[1].split("  python-reverse-order:\n", 1)[0]
        reverse = ci.split("  python-reverse-order:\n", 1)[1].split("  frontend:\n", 1)[0]
        for name, job in (("quality", quality), ("reverse", reverse)):
            with self.subTest(job=name):
                self.assertIn("--junitxml=", job)
                self.assertIn("-rfE", job)
                upload = job.split("actions/upload-artifact@v4", 1)[1]
                self.assertIn("if: ${{ always() }}", job.split("actions/upload-artifact@v4", 1)[0].rsplit("- name:", 1)[1])
                self.assertIn("junit", upload)

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
