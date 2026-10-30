import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class BuildCacheTests(unittest.TestCase):
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
