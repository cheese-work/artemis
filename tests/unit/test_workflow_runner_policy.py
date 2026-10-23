"""CI runs on the cheese-work X99 runner pool, and never on a fork pull request."""

import json
from pathlib import Path
import re

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


def test_pull_request_jobs_skip_forks_before_a_self_hosted_runner_starts():
    for file, name, workflow, job in _jobs():
        triggers = workflow[True] if True in workflow else workflow["on"]
        if "pull_request" in triggers:
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
    for name in ("python-quality", "package"):
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
    assert workflow["jobs"]["python-quality"]["timeout-minutes"] == 60


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
