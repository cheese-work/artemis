"""Regression check for runner billing and fork-safe fallbacks."""

from pathlib import Path
import re
import unittest


class RunnerRoutingTest(unittest.TestCase):
    def test_shared_x99_jobs_skip_upstream_sync_heads(self):
        expected = (
            "${{ !startsWith(github.head_ref || github.ref_name, 'upstream-sync/') "
            "&& (github.event_name != 'pull_request' "
            "|| github.event.pull_request.head.repo.full_name == github.repository) }}"
        )
        skip = "!startsWith(github.head_ref || github.ref_name, 'upstream-sync/')"
        not_pr = "${{ " + skip + " && github.event_name != 'pull_request' }}"
        pr_lane = (
            "${{ " + skip + " && github.event_name == 'pull_request' "
            "&& github.event.pull_request.head.repo.full_name == github.repository }}"
        )
        workflows = list(Path(".github/workflows").glob("*.y*ml"))
        self.assertTrue(workflows, "No workflows found")
        for workflow in workflows:
            jobs = re.split(r"(?m)^  ([\w-]+):\n", workflow.read_text().split("\njobs:\n", 1)[1])
            for name, body in zip(jobs[1::2], jobs[2::2]):
                if "cheese-x99" not in body:
                    continue
                with self.subTest(workflow=workflow.name, job=name):
                    condition = re.search(r"(?m)^    if: (.+)$", body)
                    self.assertIsNotNone(condition, "X99 job needs a job-level guard")
                    # The preview controller is intentionally inactive on every ref.
                    if condition[1] != "${{ false }}":
                        # CHE-3276: python-tests runs off pull requests only;
                        # python-quality and b2-gate are the same-repo PR lane.
                        job_expected = {
                            ("ci.yml", "python-tests"): not_pr,
                            ("ci.yml", "python-quality"): pr_lane,
                            ("ci.yml", "b2-gate"): pr_lane,
                        }.get((workflow.name, name), expected)
                        self.assertEqual(condition[1], job_expected)

    def test_main_push_ci_keeps_python_matrix_and_result_gate(self):
        ci = Path(".github/workflows/ci.yml").read_text()
        self.assertIn("\non:\n  push:\n    branches: [main]\n  pull_request:\n  schedule:\n", ci)
        jobs = re.split(r"(?m)^  ([\w-]+):\n", ci.split("\njobs:\n", 1)[1])
        self.assertEqual(
            jobs[1::2], ["python-tests", "python-quality", "b2-gate", "frontend", "package"]
        )

    def test_artemis_uses_x99_and_provisions_chrome(self):
        ci = Path(".github/workflows/ci.yml").read_text()
        whats_new = Path(".github/workflows/whats-new.yml").read_text()
        self.assertEqual(ci.count("github.repository_owner == 'cheese-work'"), 5)
        # CHE-3604: the What's New check is a light job (5 min limit), so it runs GitHub-hosted.
        self.assertIn("runs-on: ubuntu-latest", whats_new)
        self.assertNotIn("cheese-x99", whats_new)
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

    def test_python_matrix_legs_keep_their_test_report_when_the_job_log_is_lost(self):
        # GitHub returned 404 for the long Python job logs (CHE-2621), so a
        # failing test could not be named. The report lives in an artifact.
        ci = Path(".github/workflows/ci.yml").read_text()
        tests = ci.split("  python-tests:\n", 1)[1].split("  python-quality:\n", 1)[0]
        self.assertEqual(tests.count("--junitxml="), 2)
        self.assertEqual(tests.count("-rfE"), 2)
        before, upload = tests.split("actions/upload-artifact@v4", 1)
        self.assertIn("if: ${{ always() }}", before.rsplit("- name:", 1)[1])
        self.assertIn("junit-${{ matrix.suite }}", upload)

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
