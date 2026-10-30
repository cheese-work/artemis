import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class BuildCacheTests(unittest.TestCase):
    def test_workflow_parser_has_a_direct_dev_dependency(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertTrue(
            any(
                dependency.lower().startswith("pyyaml")
                for dependency in project["dependency-groups"]["dev"]
            )
        )

    def test_uv_action_keeps_the_disposable_cache_path(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text())
            for name, job in workflow.get("jobs", {}).items():
                for step in job.get("steps", []):
                    if "setup-uv@" in step.get("uses", ""):
                        with self.subTest(workflow=path.name, job=name):
                            self.assertEqual(
                                step.get("with", {}).get("cache-local-path"),
                                "${{ runner.temp }}/build-cache/uv",
                                "setup-uv otherwise overrides UV_CACHE_DIR with runner-home storage",
                            )

    def test_ssd2_cache_storage_does_not_fail_jobs(self):
        count = 0
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text())
            for job in workflow.get("jobs", {}).values():
                for step in job.get("steps", []):
                    if step.get("name") != "Use disposable build caches":
                        continue
                    count += 1
                    with (
                        self.subTest(workflow=path.name),
                        tempfile.TemporaryDirectory() as directory,
                    ):
                        fake_df = Path(directory, "df")
                        fake_df.write_text(
                            '#!/bin/sh\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/nvme1n1 100 1 99 1%% /_work\n"\n'
                        )
                        fake_df.chmod(0o755)
                        output = Path(directory, "environment")
                        result = subprocess.run(
                            ["bash", "-euc", step["run"]],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            env={
                                **os.environ,
                                "PATH": directory + os.pathsep + os.environ["PATH"],
                                "RUNNER_TEMP": directory,
                                "GITHUB_ENV": str(output),
                                "GOFLAGS": "-count=1",
                            },
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        environment = dict(
                            line.split("=", 1) for line in output.read_text().splitlines()
                        )
                        self.assertEqual(
                            environment["GOCACHE"], directory + "/build-cache/go-build"
                        )
                        self.assertEqual(environment["GOFLAGS"], "-count=1 -modcacherw")
                        self.assertNotIn("exit 1", step["run"])
        self.assertGreater(count, 0)

    def test_checked_out_jobs_share_cache_initialization(self):
        shared = 0
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text())
            for job in workflow.get("jobs", {}).values():
                checkout = False
                for step in job.get("steps", []):
                    if "checkout@" in step.get("uses", ""):
                        checkout = True
                    if (
                        step.get("name") == "Use disposable build caches"
                        and checkout
                        and path.name != "pr-close-target-check.yml"
                    ):
                        shared += 1
                        self.assertEqual(
                            step["run"].strip(), "bash .github/scripts/build-cache-env.sh"
                        )
        self.assertGreater(shared, 0)

    def test_maintenance_has_no_build_cache_setup(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/cache-maintenance.yml").read_text())
        job = workflow["jobs"]["cleanup"]
        self.assertFalse(
            any(step.get("name") == "Use disposable build caches" for step in job["steps"])
        )
        self.assertEqual(job["runs-on"], "ubuntu-24.04")

    def test_tooling_jobs_use_removable_job_local_caches(self):
        count = 0
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text())
            for name, job in workflow.get("jobs", {}).items():
                tooling = [
                    step
                    for step in job.get("steps", [])
                    if any(
                        tool in step.get("uses", "")
                        for tool in ["setup-go@", "setup-node@", "setup-uv@"]
                    )
                ]
                if "cheese-x99" not in str(job) or not tooling:
                    continue
                count += 1
                with self.subTest(workflow=path.name, job=name):
                    step = next(
                        (
                            step
                            for step in job["steps"]
                            if step.get("name") == "Use disposable build caches"
                        ),
                        None,
                    )
                    self.assertIsNotNone(
                        step, "tooling must not read persistent runner-home caches"
                    )
                    self.assertLess(job["steps"].index(step), job["steps"].index(tooling[0]))
                    with tempfile.TemporaryDirectory() as directory:
                        output = Path(directory, "environment")
                        subprocess.run(
                            ["bash", "-euc", step["run"]],
                            cwd=ROOT,
                            check=True,
                            env={
                                **os.environ,
                                "RUNNER_TEMP": directory,
                                "GITHUB_ENV": str(output),
                                "GOFLAGS": "-count=1",
                            },
                        )
                        environment = dict(
                            line.split("=", 1) for line in output.read_text().splitlines()
                        )
                        for variable in [
                            "GOCACHE",
                            "GOMODCACHE",
                            "XDG_CACHE_HOME",
                            "npm_config_cache",
                            "npm_config_store_dir",
                            "PIP_CACHE_DIR",
                            "UV_CACHE_DIR",
                            "BUILDX_CONFIG",
                        ]:
                            self.assertTrue(
                                environment[variable].startswith(directory + "/"), variable
                            )
                        self.assertEqual(environment["GOFLAGS"], "-count=1 -modcacherw")
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
