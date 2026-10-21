"""CI runs on the cheese-work X99 runner pool, and never on a fork pull request."""

from pathlib import Path

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


def test_every_active_job_runs_on_the_x99_pool():
    for file, name, _workflow, job in _jobs():
        labels = job["runs-on"]
        assert labels[:2] == ["self-hosted", "cheese-x99"] or (
            labels[0] == "self-hosted" and "${{ matrix.os }}" in labels[1]
        ), (file, name, labels)


def test_python_matrix_names_the_x99_label():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["python-quality"]["strategy"]["matrix"]["os"]
    assert matrix == ["cheese-x99"]


def test_pull_request_jobs_skip_forks_before_a_self_hosted_runner_starts():
    for file, name, workflow, job in _jobs():
        triggers = workflow[True] if True in workflow else workflow["on"]
        if "pull_request" in triggers:
            assert FORK_GUARD in job["if"], (file, name)


def test_frontend_job_installs_its_own_chrome_and_keeps_an_existing_one():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    steps = {step["name"]: step for step in workflow["jobs"]["frontend"]["steps"]}
    install = steps["Install headless Chrome"]["run"]
    assert "apt-get install" in install and "libnss3" in install and "CHROME_BIN" in install
    assert "--install-deps" not in install  # the runner user is not root
    assert (
        ":-$(command -v google-chrome"
        in steps["Check root and /preview/pr/70/ browser requests"]["run"]
    )


def test_python_jobs_install_the_opencv_system_libraries():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    for name in ("python-quality", "package"):
        runs = [step.get("run", "") for step in workflow["jobs"][name]["steps"]]
        assert any("libxcb1" in run and "libgl1" in run for run in runs), name
