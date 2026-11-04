"""CI runs on the cheese-work X99 runner pool, and never on a fork pull request."""

import json
import os
from pathlib import Path
import re
import subprocess

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
FORK_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"
# Runs only when dispatched by hand and uses its own protected controller group.
INACTIVE = {("preview-controller.yml", "preview-controller")}


def _jobs():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            if (path.name, name) not in INACTIVE:
                yield path.name, name, workflow, job


def test_no_workflow_depends_on_blacksmith():
    offenders = [
        path.name for path in WORKFLOWS.glob("*.yml") if "blacksmith" in path.read_text().lower()
    ]
    assert offenders == []


def _x99_labels(runs_on):
    """The X99 label list a job selects, from a literal list or the owner-gated expression."""
    if isinstance(runs_on, list):
        return runs_on
    match = re.search(
        r"fromJSON\('(\[[^']*\])'\) \|\| fromJSON\('\[\"ubuntu-latest\"\]'\)", runs_on
    )
    assert match, runs_on
    return json.loads(match.group(1))


def test_every_active_job_runs_on_the_x99_pool():
    for file, name, _workflow, job in _jobs():
        labels = _x99_labels(job["runs-on"])
        assert labels[0] == "self-hosted" and "cheese-x99" in labels, (file, name, labels)


def test_only_the_frontend_job_requires_the_browser_runner():
    for file, name, _workflow, job in _jobs():
        browser = "cheese-x99-browser" in _x99_labels(job["runs-on"])
        assert browser == (name == "frontend"), (file, name)


def _triggers(workflow):
    return workflow[True] if True in workflow else workflow["on"]


def _skips_pull_request(job):
    return job.get("if", "").strip("${} ") == "github.event_name != 'pull_request'"


def test_pull_request_jobs_skip_forks_before_a_self_hosted_runner_starts():
    for file, name, workflow, job in _jobs():
        if "pull_request" in _triggers(workflow) and not _skips_pull_request(job):
            assert FORK_GUARD in job["if"], (file, name)


def test_frontend_job_provisions_chrome_without_disabling_its_sandbox():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    steps = {step["name"]: step for step in workflow["jobs"]["frontend"]["steps"]}
    assert re.fullmatch(
        r"browser-actions/setup-chrome@[0-9a-f]{40}", steps["Set up headless Chrome"]["uses"]
    )
    assert "--no-sandbox" not in (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")


def test_python_jobs_install_the_opencv_system_libraries():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    for name in ("python-quality", "python-tests", "b2-gate", "package"):
        steps = workflow["jobs"][name]["steps"]
        install = next(
            (i for i, step in enumerate(steps) if "apt-get install" in step.get("run", "")), None
        )
        assert install is not None, name
        for package in ("libgl1", "libglib2.0-0", "libxcb1"):
            assert package in steps[install]["run"].split(), (name, package)
        # OpenCV loads at import time, so the libraries precede every Python and uv step.
        before = " ".join(str(step.get("uses", "")) for step in steps[:install])
        assert "setup-python" not in before and "setup-uv" not in before, name


def test_python_job_timeout_allows_the_slower_x99_container():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    assert workflow["jobs"]["python-tests"]["timeout-minutes"] == 90


def test_ci_does_not_duplicate_pull_request_runs_on_branch_pushes():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers


def test_python_passes_have_independent_budgets_without_extra_runner_contention():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    tests = workflow["jobs"]["python-tests"]
    assert tests["timeout-minutes"] == 90
    assert tests["strategy"] == {
        "fail-fast": False,
        "max-parallel": 1,
        "matrix": {"suite": ["deterministic", "reverse"]},
    }
    steps = {step["name"]: step for step in tests["steps"]}
    normal = steps["Run deterministic Python tests"]
    reverse = steps["Run unit tests in reverse file order"]
    assert normal["if"] == "matrix.suite == 'deterministic'"
    assert reverse["if"] == "matrix.suite == 'reverse'"
    for path in ("tests/unit", "tests/tools", "packages/artemis-client/tests"):
        assert path in normal["run"]
    assert "--cov-fail-under=60" in normal["run"]
    assert "sort -zr" in reverse["run"]
    assert 'pytest "${test_files[@]}"' in reverse["run"]
    for step in (normal, reverse):
        assert "--durations=20" in step["run"]
    assert "Fetch CHE-844 receipt sources" in steps


def _ci():
    return yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


def test_pull_request_required_job_set_is_the_fast_lane():
    """PR feedback within 15 min p90 (CHE-3276): the full suite does not run on a PR."""
    on_pull_request = {
        job["name"] for job in _ci()["jobs"].values() if not _skips_pull_request(job)
    }
    assert on_pull_request == {
        "Python 3.12",
        "B2 ADB allowlist and tunnel fuzz",
        "Frontend tests and production build",
        "Build and smoke-test Python packages",
    }


def test_full_python_suite_runs_on_main_nightly_and_dispatch_only():
    workflow = _ci()
    tests = workflow["jobs"]["python-tests"]
    assert _skips_pull_request(tests)
    triggers = _triggers(workflow)
    assert triggers["push"] == {"branches": ["main"]}
    assert len(triggers["schedule"]) == 1 and "cron" in triggers["schedule"][0]
    assert "workflow_dispatch" in triggers


def test_main_runs_queue_instead_of_cancelling_each_other():
    cancel = _ci()["concurrency"]["cancel-in-progress"]
    assert cancel == "${{ github.event_name == 'pull_request' }}"


def test_python_pull_request_lane_runs_checks_and_the_fast_subset():
    job = _ci()["jobs"]["python-quality"]
    assert job["name"] == "Python 3.12"
    assert "needs" not in job
    assert job["timeout-minutes"] <= 20
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    for command in ("ruff format --check", "ruff check", "quality_ratchet.py", "pyright"):
        assert command in runs, command
    assert "--ignore=tests/unit/admin_console" in runs
    assert "--cov" not in runs
    assert "sort -zr" not in runs
    assert "test_host_tunnel" not in runs  # B2 is its own check: see the next test


def test_b2_gate_is_its_own_pull_request_check_that_can_be_required():
    """Branch rules can require a check by name; a step inside `Python 3.12` cannot be required alone."""
    jobs = _ci()["jobs"]
    job = jobs["b2-gate"]
    assert job["name"] == "B2 ADB allowlist and tunnel fuzz"
    assert not _skips_pull_request(job)
    assert FORK_GUARD in job["if"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "tests/unit/runtime/test_host_tunnel.py" in runs
    assert "tests/unit/admin_console/test_host_tunnel_service.py" in runs
    assert "|| true" not in runs


def test_mapped_test_selection_fails_closed():
    steps = _ci()["jobs"]["python-quality"]["steps"]
    run = next(step for step in steps if "ci_mapped_tests.py" in step.get("run", ""))["run"]
    assert "set -euo pipefail" in run
    # A selector error must stop the job; only an empty grep result may be tolerated.
    assert "ci_mapped_tests.py | grep" not in run
    assert '> "$RUNNER_TEMP/mapped.txt"' in run


def test_python_pull_request_lane_runs_tests_mapped_from_the_changed_paths():
    steps = _ci()["jobs"]["python-quality"]["steps"]
    mapped = next(step for step in steps if "ci_mapped_tests.py" in step.get("run", ""))
    assert "git diff --name-only" in mapped["run"]
    assert "github.base_ref" in mapped["env"]["BASE_REF"]
    assert "pytest" in mapped["run"]
    # The mapped tests follow the B2 gate position check: both run before the job ends.
    names = [step["name"] for step in steps]
    assert names.index(mapped["name"]) > names.index("Run fast Python tests")


def test_host_agent_does_not_run_on_branch_pushes_beside_pull_requests():
    workflow = yaml.safe_load((WORKFLOWS / "host-agent.yml").read_text(encoding="utf-8"))
    triggers = _triggers(workflow)
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["paths"] == triggers["pull_request"]["paths"]


def test_karma_tolerates_a_browser_stall_on_a_shared_x99_host():
    karma = (WORKFLOWS.parents[1] / "apps" / "showcase_ui" / "karma.conf.js").read_text()
    for setting in ("browserDisconnectTimeout", "browserDisconnectTolerance", "captureTimeout"):
        assert setting in karma


def test_third_party_actions_on_the_x99_pool_are_pinned_to_a_full_commit_sha():
    """A mutable tag on a self-hosted runner can run changed upstream code."""
    pattern = re.compile(r"^\s*-?\s*uses:\s*(astral-sh/[\w.-]+)@(\S+)", re.MULTILINE)
    found = [
        (path.name, action, ref)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for action, ref in pattern.findall(path.read_text(encoding="utf-8"))
    ]
    assert found, "expected at least one astral-sh action"
    assert [item for item in found if not re.fullmatch(r"[0-9a-f]{40}", item[2])] == []
