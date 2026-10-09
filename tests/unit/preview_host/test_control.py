"""Protected control-plane checks use disposable files and no credentials."""

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import preview_control as control


CONFIG = Path(__file__).resolve().parents[3] / "config" / "preview"
AUTHOR = "11111111-1111-4111-8111-111111111111"
REVIEWER = "22222222-2222-4222-8222-222222222222"
IDENTITY = "33333333-3333-4333-8333-333333333333"
CREDENTIAL = "44444444-4444-4444-8444-444444444444"
ISSUE = "55555555-5555-4555-8555-555555555555"
THREAD = "66666666-6666-4666-8666-666666666666"


@pytest.fixture
def documents():
    policy = {
        "schema_version": 1,
        "revision": 1,
        "workspace_id": "f2b734e0-b6de-4414-8a27-a2f69b0ef843",
        "project_id": "3c98ab77-0446-4c89-99f0-9f59ddf84e03",
        "repository_id": 1365625873,
        "repository": "cheese-work/artemis",
        "controller_sha": "c" * 40,
        "same_origin_scope": "trusted-root-origin-insiders",
        "authors": [{"id": AUTHOR, "model": "author-model", "lab": "author-lab"}],
        "reviewers": [{"id": REVIEWER, "model": "reviewer-model", "lab": "reviewer-lab"}],
        "human_approver_ids": [123456],
        "platform_identity_id": IDENTITY,
        "platform_credential_id": CREDENTIAL,
    }
    registry = {
        "schema_version": 1,
        "policy_revision": 1,
        "entries": [
            {
                "pr": 70,
                "issue_id": ISSUE,
                "thread_id": THREAD,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "author_ids": [AUTHOR],
            }
        ],
    }
    scope = {
        "schema_version": 1,
        "identity_id": IDENTITY,
        "credential_id": CREDENTIAL,
        "workspace_id": policy["workspace_id"],
        "project_ids": [policy["project_id"]],
        "permissions": sorted(control.READ_PERMISSIONS),
        "complete": True,
        "supported_identity": True,
        "exclusive_custody": True,
        "checked_at": 1000,
        "expires_at": 2000,
    }
    return policy, registry, scope


def test_protected_registry_resolves_exact_candidate(documents):
    policy, registry, scope = documents
    result = control.validate_documents(policy, registry, scope, now=1500)
    candidate = result.candidate(70)
    assert candidate.issue_id == ISSUE
    assert candidate.thread_id == THREAD
    assert candidate.head_sha == "a" * 40
    assert candidate.base_sha == "b" * 40
    assert candidate.author_ids == (AUTHOR,)
    assert result.policy_revision == 1
    with pytest.raises(ValueError, match="registered"):
        result.candidate(71)


@pytest.mark.parametrize("pr", [True, 0, -1, "70", 1.5, 2**63])
def test_submitter_can_supply_only_bounded_numeric_pr(documents, pr):
    result = control.validate_documents(*documents, now=1500)
    with pytest.raises(ValueError):
        result.candidate(pr)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "attacker/artemis"),
        ("repository_id", True),
        ("controller_sha", "main"),
        ("controller_sha", "0" * 40),
        ("revision", True),
        ("same_origin_scope", "untrusted-public"),
        ("platform_identity_id", AUTHOR),
        ("platform_identity_id", REVIEWER),
        ("authors", []),
        ("reviewers", []),
        ("workspace_id", "../another-workspace"),
        ("schema_version", 2),
    ],
)
def test_policy_rejects_unknown_or_unsafe_provenance(documents, field, value):
    policy, registry, scope = documents
    policy[field] = value
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)


def test_overlapping_or_duplicate_participants_are_rejected(documents):
    policy, registry, scope = documents
    policy["reviewers"][0]["id"] = AUTHOR
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)
    policy["reviewers"][0]["id"] = REVIEWER
    policy["authors"] *= 2
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)


@pytest.mark.parametrize(
    "field,value",
    [
        ("permissions", ["*"]),
        ("permissions", ["issue.read", "issue.update"]),
        ("permissions", ["issue.read"]),
        ("permissions", sorted(control.READ_PERMISSIONS) + ["agent.impersonate"]),
        ("identity_id", AUTHOR),
        ("credential_id", ISSUE),
        ("project_ids", []),
        ("project_ids", [ISSUE]),
        ("workspace_id", ISSUE),
        ("complete", False),
        ("complete", "true"),
        ("supported_identity", False),
        ("exclusive_custody", False),
        ("checked_at", 1501),
        ("expires_at", 1500),
        ("expires_at", True),
    ],
)
def test_credential_scope_fails_closed(documents, field, value):
    policy, registry, scope = documents
    scope[field] = value
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)


@pytest.mark.parametrize("document_index", [0, 1, 2])
def test_unknown_missing_and_secret_fields_rejected(documents, document_index):
    for field in ("token", "approved", "path"):
        changed = copy.deepcopy(documents)
        changed[document_index][field] = "caller-controlled"
        with pytest.raises(ValueError):
            control.validate_documents(*changed, now=1500)
    changed = copy.deepcopy(documents)
    changed[document_index].pop("schema_version")
    with pytest.raises(ValueError):
        control.validate_documents(*changed, now=1500)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pr", True),
        ("issue_id", "../unprotected"),
        ("thread_id", None),
        ("head_sha", "a" * 7),
        ("base_sha", "main"),
        ("author_ids", [REVIEWER]),
        ("author_ids", []),
        ("author_ids", [AUTHOR, AUTHOR]),
    ],
)
def test_registry_cannot_invent_candidate_provenance(documents, field, value):
    policy, registry, scope = documents
    registry["entries"][0][field] = value
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)


def test_registry_rejects_duplicates_stale_policy_and_overflow(documents):
    policy, registry, scope = documents
    registry["entries"] *= 2
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)
    registry["entries"] = registry["entries"][:1]
    registry["policy_revision"] = 2
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)
    registry["policy_revision"] = 1
    registry["entries"] *= control.MAX_ENTRIES + 1
    with pytest.raises(ValueError):
        control.validate_documents(policy, registry, scope, now=1500)


def test_candidate_is_immutable_and_detached_from_input(documents):
    result = control.validate_documents(*documents, now=1500)
    documents[1]["entries"][0]["author_ids"].clear()
    assert result.candidate(70).author_ids == (AUTHOR,)
    with pytest.raises(AttributeError):
        result.candidate(70).issue_id = THREAD


@pytest.fixture
def protected_tree(tmp_path, documents):
    directory = tmp_path / "protected"
    directory.mkdir(mode=0o750)
    for name, document in zip(control.DOCUMENT_NAMES, documents, strict=True):
        path = directory / name
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o640)
    return directory, {"owner_uid": os.getuid(), "anchor": tmp_path, "now": 1500}


def test_reads_held_descriptors_from_protected_fixture(protected_tree):
    directory, options = protected_tree
    assert control.load_control(directory, **options).candidate(70).issue_id == ISSUE


@pytest.mark.parametrize("target", ["directory", "policy.json", "registry.json", "scope.json"])
def test_rejects_group_or_world_writable_control_paths(protected_tree, target):
    directory, options = protected_tree
    path = directory if target == "directory" else directory / target
    path.chmod(0o770 if target == "directory" else 0o660)
    with pytest.raises(ValueError, match="protected"):
        control.load_control(directory, **options)


def test_rejects_wrong_owner_and_writable_ancestor(protected_tree):
    directory, options = protected_tree
    with pytest.raises(ValueError, match="protected"):
        control.load_control(directory, **(options | {"owner_uid": os.getuid() + 1}))
    options["anchor"].chmod(0o777)
    with pytest.raises(ValueError, match="protected"):
        control.load_control(directory, **options)


@pytest.mark.parametrize("target", ["directory", "policy.json"])
def test_rejects_symlink_paths(protected_tree, tmp_path, target):
    directory, options = protected_tree
    path = directory if target == "directory" else directory / target
    saved = path.with_name(path.name + "-saved")
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises((ValueError, OSError)):
        control.load_control(directory, **options)


def test_rejects_hardlinks_oversize_duplicates_and_special_files(protected_tree):
    directory, options = protected_tree
    path = directory / "policy.json"
    linked = directory / "extra-link"
    os.link(path, linked)
    with pytest.raises(ValueError):
        control.load_control(directory, **options)
    linked.unlink()
    for content in ('{"schema_version":1,"schema_version":1}', " " * (control.MAX_BYTES + 1)):
        path.write_text(content)
        with pytest.raises(ValueError):
            control.load_control(directory, **options)
    path.unlink()
    os.mkfifo(path, mode=0o600)
    with pytest.raises(ValueError):
        control.load_control(directory, **options)


def test_missing_malformed_and_non_utf8_documents_fail_closed(protected_tree):
    directory, options = protected_tree
    path = directory / "policy.json"
    for content in (b"not JSON", b"\xff", b"[]", b"null"):
        path.write_bytes(content)
        with pytest.raises(ValueError):
            control.load_control(directory, **options)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        control.load_control(directory, **options)


def test_unprovisioned_example_is_not_activation_ready():
    example = json.loads((CONFIG / "control.example.json").read_text())
    with pytest.raises(ValueError):
        control.validate_documents(example["policy"], example["registry"], example["scope"])


def test_units_keep_authority_out_of_shared_agents_and_builds():
    daemon = (CONFIG / "artemis-previewd.service").read_text()
    runner = (CONFIG / "artemis-preview-runner@.service").read_text()
    ingress = (CONFIG / "traefik.service").read_text()
    assert "User=artemis-previewd" in daemon
    assert "SupplementaryGroups=docker" in daemon
    assert "LoadCredential=multica:" in daemon
    assert "StateDirectoryMode=0700" in daemon
    assert "ExecStartPre=+" in daemon
    assert "ConditionPathExists=/etc/artemis-preview/activation.ready" in daemon
    assert "DynamicUser=true" in runner
    assert "User=artemis-prjob-%i" in runner
    assert "SupplementaryGroups=artemis-preview-runner" in runner
    assert (
        "InaccessiblePaths=/etc/artemis-preview /var/lib/artemis-preview /run/docker.sock" in runner
    )
    assert "LoadCredential" not in runner
    assert "User=artemis-preview-ingress" in ingress
    assert "CapabilityBoundingSet=" in ingress
    assert "docker.sock" not in ingress
    assert "congvc" not in daemon + runner + ingress


def test_sysusers_and_tmpfiles_only_touch_disposable_root(tmp_path):
    if not shutil.which("systemd-sysusers") or not shutil.which("systemd-tmpfiles"):
        pytest.skip("native systemd fixture checks require Linux systemd utilities")
    result = subprocess.run(
        ["systemd-sysusers", f"--root={tmp_path}", "--dry-run", str(CONFIG / "sysusers.conf")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "artemis-previewd" in result.stderr
    assert "artemis-preview-ingress" in result.stderr
    assert "artemis-preview-runner" in result.stderr
    assert not (tmp_path / "etc/passwd").exists()
    result = subprocess.run(
        ["systemd-tmpfiles", f"--root={tmp_path}", "--create", str(CONFIG / "tmpfiles.conf")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Unknown user" in result.stderr or "Failed to resolve" in result.stderr
    fixture = (CONFIG / "tmpfiles.conf").read_text()
    for account in (
        "root",
        "artemis-previewd",
        "artemis-preview-ingress",
        "artemis-preview-runner",
    ):
        fixture = fixture.replace(account, str(os.getuid()))
    lines = []
    for line in fixture.splitlines():
        fields = line.split()
        fields[4] = str(os.getgid())
        lines.append(" ".join(fields))
    configuration = tmp_path / "fixture.conf"
    configuration.write_text("\n".join(lines) + "\n")
    subprocess.run(
        ["systemd-tmpfiles", f"--root={tmp_path}", "--create", str(configuration)],
        capture_output=True,
        text=True,
        check=True,
    )
    for path, mode in (
        ("etc/artemis-preview", 0o755),
        ("etc/artemis-preview/credentials", 0o700),
        ("etc/artemis-preview/routes", 0o2750),
        ("var/lib/artemis-preview", 0o700),
        ("run/artemis-preview", 0o750),
    ):
        assert (tmp_path / path).stat().st_mode & 0o7777 == mode
    assert not (tmp_path / "etc/artemis-preview/activation.ready").exists()


def test_native_units_parse_with_only_disposable_placeholder_executables(tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("native unit verification requires systemd-analyze")
    units = tmp_path / "etc/systemd/system"
    units.mkdir(parents=True)
    for name in ("sysinit", "basic", "shutdown", "network"):
        (units / f"{name}.target").write_text("[Unit]\nDefaultDependencies=no\n")
    (units / "docker.service").write_text(
        "[Unit]\nDefaultDependencies=no\n[Service]\nExecStart=/usr/bin/true\n"
    )
    for name in (
        "usr/bin/true",
        "usr/local/libexec/artemis-preview/venv/bin/python",
        "usr/local/libexec/artemis-preview/artemis-previewd",
        "usr/local/libexec/artemis-preview/artemis-preview-controller",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("disposable placeholder: systemd verify does not execute this file\n")
        path.chmod(0o755)
    names = ("artemis-previewd.service", "artemis-preview-runner@1.service")
    for name in names:
        source = "artemis-preview-runner@.service" if "@" in name else name
        (units / name).write_bytes((CONFIG / source).read_bytes())
    result = subprocess.run(
        ["systemd-analyze", "verify", f"--root={tmp_path}", "--man=no", *names],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "run/artemis-preview").exists()
