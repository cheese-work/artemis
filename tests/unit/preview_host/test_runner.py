import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import preview_runner as runner
from tests.unit.preview_host import test_github as github_fixtures


documents = github_fixtures.documents
fixture = github_fixtures.fixture
approval = github_fixtures.approval


@pytest.fixture
def admitted(approval):
    return approval[0][0], github_fixtures.admit(approval)


@pytest.fixture
def group(admitted):
    policy, _ = admitted
    return {
        "id": 42,
        "name": "artemis-preview-controller",
        "visibility": "selected",
        "allows_public_repositories": True,
        "restricted_to_workflows": True,
        "selected_workflows": [
            f"cheese-work/artemis/.github/workflows/preview-controller.yml@{policy.controller_sha}"
        ],
        "inherited": False,
    }


@pytest.fixture
def job(admitted):
    policy, _ = admitted
    return runner.Job(
        repository_id=policy.repository_id,
        workflow_ref=f"cheese-work/artemis/.github/workflows/preview-controller.yml@{policy.controller_sha}",
        workflow_sha=policy.controller_sha,
        run_id=1234,
        run_attempt=1,
        job_id=2345,
        runner_id=3456,
        runner_group_id=42,
        labels=("self-hosted", "cheese-x99", "cheese-x99-artemis-preview"),
    )


def allocate(admitted, group, job, **options):
    policy, record = admitted
    pin = runner.verify_group(policy, group, selected(policy))
    return runner.allocate(
        policy, record, pin, job, controller_uid=20001, builder_uid=20002, now=1501, **options
    )


def selected(policy):
    return [{"id": policy.repository_id, "full_name": "cheese-work/artemis", "private": False}]


def test_exact_repository_workflow_and_admission_allocate_fresh_run(admitted, group, job):
    first = allocate(admitted, group, job)
    second = allocate(admitted, group, replace(job, run_attempt=2))
    assert first.nonce != second.nonce
    assert len(first.nonce) == 32
    assert first.admission_id == admitted[1].id
    assert first.repository_id == job.repository_id
    assert first.controller_uid != first.builder_uid
    assert runner.run_name(first) != runner.run_name(second)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "shared"),
        ("visibility", "all"),
        ("allows_public_repositories", False),
        ("restricted_to_workflows", False),
        ("restricted_to_workflows", 1),
        ("selected_workflows", []),
        (
            "selected_workflows",
            ["cheese-work/artemis/.github/workflows/preview-controller.yml@main"],
        ),
        ("selected_workflows", ["cheese-work/artemis/.github/workflows/other.yml@" + "c" * 40]),
        ("inherited", True),
        ("id", True),
    ],
)
def test_native_group_rejects_missing_enforcement_or_mutable_workflows(
    admitted, group, field, value
):
    group[field] = value
    with pytest.raises(ValueError):
        runner.verify_group(admitted[0], group, selected(admitted[0]))


@pytest.mark.parametrize("repositories", [[], [1], [True], [{"id": 1}], "1365625873"])
def test_group_is_exclusive_to_repository(admitted, group, repositories):
    with pytest.raises(ValueError):
        runner.verify_group(admitted[0], group, repositories)


def test_public_flag_is_required_only_for_the_selected_public_repository(admitted, group):
    policy = admitted[0]
    repositories = selected(policy)
    assert runner.verify_group(policy, group, repositories).group_id == 42
    with pytest.raises(ValueError):
        runner.verify_group(policy, group, repositories * 2)
    repositories[0]["private"] = True
    group["allows_public_repositories"] = False
    assert runner.verify_group(policy, group, repositories).group_id == 42
    repositories[0]["private"] = 1
    with pytest.raises(ValueError):
        runner.verify_group(policy, group, repositories)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_id", 1),
        ("workflow_sha", "d" * 40),
        ("workflow_ref", "cheese-work/artemis/.github/workflows/preview-controller.yml@main"),
        ("workflow_ref", "other/repo/.github/workflows/preview-controller.yml@" + "c" * 40),
        ("workflow_ref", "cheese-work/artemis/.github/workflows/ci.yml@" + "c" * 40),
        ("runner_group_id", 43),
        ("labels", ("cheese-x99",)),
        ("run_attempt", 0),
        ("run_id", True),
        ("job_id", -1),
        ("runner_id", "3456"),
    ],
)
def test_other_repository_workflow_sha_group_or_job_cannot_allocate(
    admitted, group, job, field, value
):
    with pytest.raises(ValueError):
        allocate(admitted, group, replace(job, **{field: value}))


@pytest.mark.parametrize(
    "field,value", [("status", "pending-human"), ("approval", None), ("expires_at", 1501)]
)
def test_unadmitted_or_expired_record_cannot_allocate(admitted, group, job, field, value):
    with pytest.raises(ValueError):
        allocate((admitted[0], replace(admitted[1], **{field: value})), group, job)


def test_policy_and_candidate_substitution_cannot_allocate(admitted, group, job):
    policy, record = admitted
    with pytest.raises(ValueError):
        allocate((replace(policy, policy_revision=2), record), group, job)
    receipt = replace(record.receipt, head_sha="d" * 40)
    with pytest.raises(ValueError):
        allocate((policy, replace(record, receipt=receipt)), group, job)


@pytest.mark.parametrize(
    "controller_uid,builder_uid",
    [(0, 20002), (1000, 20002), (20001, 20001), (True, 20002), (20001, 2**32)],
)
def test_shared_root_or_invalid_uid_is_rejected(admitted, group, job, controller_uid, builder_uid):
    policy, record = admitted
    pin = runner.verify_group(policy, group, selected(policy))
    with pytest.raises(ValueError):
        runner.allocate(
            policy,
            record,
            pin,
            job,
            controller_uid=controller_uid,
            builder_uid=builder_uid,
            now=1501,
        )


@pytest.fixture
def tree(tmp_path, admitted, group, job, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    ownership = []
    monkeypatch.setattr(
        runner.os, "fchown", lambda descriptor, uid, gid: ownership.append((uid, gid))
    )
    allocation = allocate(admitted, group, job)
    options = {"root": root, "anchor": tmp_path, "owner_uid": os.getuid()}
    return allocation, options, ownership


def test_exclusive_tree_has_separate_controller_builder_and_handoff_ownership(tree):
    allocation, options, ownership = tree
    path = runner.prepare_workspace(allocation, **options)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for relative in (
        "controller",
        "controller/home",
        "controller/work",
        "controller/runtime",
        "builder",
        "builder/home",
        "builder/work",
        "builder/runtime",
        "builder/state",
        "handoff",
    ):
        assert stat.S_IMODE((path / relative).stat().st_mode) == 0o700
    assert ownership.count((allocation.controller_uid, allocation.controller_uid)) == 3
    assert ownership.count((allocation.builder_uid, allocation.builder_uid)) == 5
    for branch in (path, path / "controller", path / "builder"):
        assert branch.stat().st_uid == options["owner_uid"]
    with pytest.raises(FileExistsError):
        runner.prepare_workspace(allocation, **options)


def test_new_nonce_cannot_reuse_run_uid_or_builder_state(tree):
    allocation, options, _ = tree
    runner.prepare_workspace(allocation, **options)
    for changed in (
        replace(allocation, nonce="f" * 32),
        replace(allocation, run_id=1235, nonce="f" * 32),
        replace(allocation, controller_uid=20003, builder_uid=20004, nonce="f" * 32),
        replace(allocation, controller_uid=20002, builder_uid=20003, run_id=1235),
    ):
        with pytest.raises(FileExistsError):
            runner.prepare_workspace(changed, **options)


def test_symlink_writable_parent_and_traversal_fail_closed(tree, tmp_path):
    allocation, options, _ = tree
    options["root"].chmod(0o777)
    with pytest.raises(ValueError):
        runner.prepare_workspace(allocation, **options)
    options["root"].chmod(0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(options["root"], target_is_directory=True)
    with pytest.raises(OSError):
        runner.prepare_workspace(allocation, **(options | {"root": linked}))
    with pytest.raises(ValueError):
        runner.prepare_workspace(replace(allocation, nonce="../bad"), **options)


def test_single_use_runner_and_job_cannot_be_reallocated_to_another_run(tree):
    allocation, options, _ = tree
    runner.prepare_workspace(allocation, **options)
    for changed in (
        replace(allocation, controller_uid=20003, builder_uid=20004, run_id=1235),
        replace(allocation, controller_uid=20003, builder_uid=20004, run_id=1235, runner_id=9999),
    ):
        with pytest.raises(FileExistsError):
            runner.prepare_workspace(changed, **options)


def test_run_root_cannot_expose_user_owned_children(tree):
    allocation, options, _ = tree
    options["root"].chmod(0o755)
    with pytest.raises(ValueError):
        runner.prepare_workspace(allocation, **options)


def test_concurrent_allocators_cannot_share_claims_or_partial_state(tree):
    allocation, options, _ = tree

    def attempt():
        try:
            return runner.prepare_workspace(allocation, **options)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda unused: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1


def test_native_group_requires_explicit_fields_and_selected_repository_identity(admitted, group):
    policy = admitted[0]
    for field in (
        "restricted_to_workflows",
        "selected_workflows",
        "inherited",
        "allows_public_repositories",
    ):
        with pytest.raises(ValueError):
            runner.verify_group(
                policy,
                {key: value for key, value in group.items() if key != field},
                selected(policy),
            )
    repositories = selected(policy)
    repositories[0]["full_name"] = "other/repo"
    with pytest.raises(ValueError):
        runner.verify_group(policy, group, repositories)


def test_failures_preserve_claims_and_do_not_reuse_partially_written_tree(tree, monkeypatch):
    allocation, options, _ = tree
    monkeypatch.setattr(
        runner.os, "fchown", lambda *args: (_ for _ in ()).throw(OSError("fixture failure"))
    )
    with pytest.raises(OSError, match="fixture failure"):
        runner.prepare_workspace(allocation, **options)
    with pytest.raises(FileExistsError):
        runner.prepare_workspace(allocation, **options)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", 1235),
        ("run_attempt", 2),
        ("nonce", "f" * 32),
        ("repository_id", 1),
        ("job_id", 2346),
        ("runner_id", 3457),
    ],
)
def test_cross_run_request_is_denied(admitted, group, job, field, value):
    allocation = allocate(admitted, group, job)
    request = runner.run_identity(allocation)
    assert runner.check_identity(allocation, request, peer_uid=allocation.controller_uid) is None
    request[field] = value
    with pytest.raises(ValueError):
        runner.check_identity(allocation, request, peer_uid=allocation.controller_uid)


@pytest.mark.parametrize("peer_uid", [0, 1000, 20002, 20003, True])
def test_builder_shared_agent_and_other_controller_cannot_handoff(admitted, group, job, peer_uid):
    allocation = allocate(admitted, group, job)
    with pytest.raises(ValueError):
        runner.check_identity(allocation, runner.run_identity(allocation), peer_uid=peer_uid)


def test_identity_rejects_caller_paths_commands_unknown_fields_and_boolean_ids(
    admitted, group, job
):
    allocation = allocate(admitted, group, job)
    for extra in ({"path": "/tmp/a"}, {"command": "sh"}, {"run_attempt": True}):
        with pytest.raises(ValueError):
            runner.check_identity(
                allocation,
                runner.run_identity(allocation) | extra,
                peer_uid=allocation.controller_uid,
            )


def test_rootless_builder_plan_mounts_only_own_state_and_output(admitted, group, job):
    allocation = allocate(admitted, group, job)
    command = runner.builder_command(allocation)
    assert command[:1] == ["systemd-run"]
    assert f"--uid={allocation.builder_uid}" in command
    assert f"--gid={allocation.builder_uid}" in command
    assert "--property=RootDirectory=/usr/local/lib/artemis-preview-builder" in command
    assert "--net=none" in command
    assert "--oci-worker-net=host" in command
    assert "--oci-worker-snapshotter=native" in command
    assert "--containerd-worker=false" in command
    assert "--property=IPAddressDeny=any" in command
    assert not any("no-process-sandbox" in argument for argument in command)
    mounts = [argument for argument in command if argument.startswith("--property=BindPaths=")]
    assert len(mounts) == 1
    assert "/controller" not in mounts[0]
    assert "docker.sock" not in mounts[0]
    assert "controller.sock" not in mounts[0]
    assert "github-runner-multica" not in " ".join(command)
    assert "--root" in command and "/state" in command
    assert "unix:///run/builder/buildkitd.sock" in command
    other = replace(allocation, run_id=1235, controller_uid=20003, builder_uid=20004)
    assert runner.builder_command(other) != command


def test_controller_plan_has_private_home_and_work_and_no_builder_mount(admitted, group, job):
    allocation = allocate(admitted, group, job)
    command = runner.controller_command(allocation)
    assert f"--uid={allocation.controller_uid}" in command
    assert f"--gid={allocation.controller_uid}" in command
    assert "--property=RootDirectory=/usr/local/lib/artemis-preview-controller" in command
    assert "--setenv=HOME=/home/controller" in command
    assert "--property=NoNewPrivileges=yes" in command
    assert "--property=CapabilityBoundingSet=" in command
    assert "--property=BindReadOnlyPaths=/run/artemis-preview/controller.sock" in command
    assert not any(
        "/builder" in argument or "/handoff" in argument or "docker.sock" in argument
        for argument in command
    )


def test_workflow_is_inert_and_selects_dedicated_group_and_labels():
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/preview-controller.yml"
    content = workflow.read_text()
    assert "group: artemis-preview-controller" in content
    assert "labels: [self-hosted, cheese-x99, cheese-x99-artemis-preview]" in content
    assert "if: ${{ false }}" in content
    assert "id-token: write" in content
    assert "pull_request_target" not in content
    assert "actions/checkout" not in content
    assert "workflow_dispatch:" in content
    assert "github.workflow_sha" in content
