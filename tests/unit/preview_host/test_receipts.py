import copy
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from scripts import preview_control as control
from scripts import preview_receipts as receipts
from tests.unit.preview_host import test_control as control_fixtures
from tests.unit.preview_host.test_control import AUTHOR, ISSUE, REVIEWER, THREAD


VERDICT = "77777777-7777-4777-8777-777777777777"
RUN = "88888888-8888-4888-8888-888888888888"
NEW_VERDICT = "99999999-9999-4999-8999-999999999999"
documents = control_fixtures.documents


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.fixture
def fixture(documents):
    policy = control.validate_documents(*documents, now=1500)
    candidate = policy.candidate(70)
    body = {
        "schema_version": 1,
        "decision": "PASS",
        "repository": "cheese-work/artemis",
        "repository_id": policy.repository_id,
        "pr": 70,
        "head_sha": candidate.head_sha,
        "base_sha": candidate.base_sha,
        "author_ids": [AUTHOR],
        "reviewer_id": REVIEWER,
        "reviewer_model": "reviewer-model",
        "reviewer_lab": "reviewer-lab",
        "policy_revision": 1,
        "same_origin_scope": "trusted-root-origin-insiders",
        "scope": "path-preview-admission",
        "complete": True,
        "unresolved_p0_p1": 0,
    }
    comment = {
        "id": VERDICT,
        "issue_id": ISSUE,
        "parent_id": THREAD,
        "author_type": "agent",
        "author_id": REVIEWER,
        "source_task_id": RUN,
        "revision": 1,
        "type": "comment",
        "created_at": timestamp(1400),
        "updated_at": timestamp(1400),
        "content": json.dumps(body),
    }
    root = {
        "id": THREAD,
        "issue_id": ISSUE,
        "parent_id": None,
        "author_id": AUTHOR,
        "author_type": "agent",
        "content": "Review dispatch",
    }
    run = {
        "id": RUN,
        "agent_id": REVIEWER,
        "issue_id": ISSUE,
        "workspace_id": policy.workspace_id,
        "status": "completed",
        "kind": "comment",
        "trigger_comment_id": THREAD,
        "started_at": timestamp(1300),
        "completed_at": timestamp(1450),
        "delivered_comment_ids": [VERDICT],
    }
    issue = {"id": ISSUE, "workspace_id": policy.workspace_id, "project_id": policy.project_id}
    state = receipts.CandidateState(
        policy.repository_id, 70, candidate.head_sha, candidate.base_sha, "open", True
    )
    return policy, body, comment, root, run, issue, state


class NativeCLI:
    def __init__(self, fixture, pages=None):
        self.policy, self.body, self.comment, self.root, self.run, self.issue, self.state = fixture
        self.pages = pages
        self.commands = []
        self.failure = None

    def __call__(self, command, **options):
        self.commands.append(command)
        assert options["timeout"] == 30
        assert options["capture_output"] is True
        assert "--compact" not in command and "--summary" not in command
        if self.failure:
            raise self.failure
        stderr = ""
        if command[2] == "get":
            result = self.issue
        elif command[2] == "runs":
            result = [self.run]
        else:
            assert command[2:5] == ["comment", "list", ISSUE]
            if self.pages:
                index = 1 if "--before" in command else 0
                result, stderr = self.pages[index]
            else:
                result = [self.root, self.comment]
        return subprocess.CompletedProcess(command, 0, json.dumps(result), stderr)


def prepare(fixture, cli=None):
    native = NativeCLI(fixture) if cli is None else cli
    return receipts.prepare(fixture[0], 70, VERDICT, fixture[-1], reader(native), now=1500)


def reader(native):
    return receipts.MulticaReader(native, clock=lambda: 1500)


def test_native_receipt_records_provenance_but_cannot_admit(fixture):
    native = NativeCLI(fixture)
    record = prepare(fixture, native)
    assert record.status == "pending-human"
    assert record.receipt.comment_id == VERDICT
    assert record.receipt.source_task_id == RUN
    assert record.receipt.revision == 1
    assert record.receipt.updated_at == timestamp(1400)
    assert record.receipt.author_id == REVIEWER
    assert record.receipt.author_ids == (AUTHOR,)
    assert record.expires_at == 2000
    assert len(record.receipt.content_sha256) == 64
    assert native.commands[0] == ["multica", "issue", "get", ISSUE, "--output", "json"]
    with pytest.raises(ValueError, match="L5a3"):
        receipts.require_admitted(record)


def test_both_reply_cursors_are_followed_before_accepting(fixture):
    policy, body, comment, root, run, issue, state = fixture
    cursor = f"Next reply cursor: --before {timestamp(1400)} --before-id {VERDICT}\n"
    native = NativeCLI(fixture, [([root], cursor), ([root, comment], "")])
    assert prepare(fixture, native).receipt.comment_id == VERDICT
    page_commands = [command for command in native.commands if "list" in command]
    assert page_commands[1][-6:-2] == ["--before", timestamp(1400), "--before-id", VERDICT]


@pytest.mark.parametrize(
    "stderr", ["Next reply cursor: --before missing\n", "Next thread cursor: bad\n"]
)
def test_partial_or_wrong_cursor_fails_closed(fixture, stderr):
    native = NativeCLI(fixture, [([fixture[3]], stderr)])
    with pytest.raises(ValueError, match="cursor"):
        prepare(fixture, native)


def test_cursor_cycles_and_page_budget_fail_closed(fixture):
    cursor = f"Next reply cursor: --before {timestamp(1400)} --before-id {VERDICT}\n"
    native = NativeCLI(fixture, [([fixture[3]], cursor), ([fixture[3]], cursor)])
    with pytest.raises(ValueError, match="cursor|budget"):
        prepare(fixture, native)


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision", "FAIL"),
        ("repository_id", True),
        ("pr", True),
        ("head_sha", "a" * 7),
        ("head_sha", "d" * 40),
        ("base_sha", "d" * 40),
        ("author_ids", []),
        ("author_ids", [REVIEWER]),
        ("author_ids", [AUTHOR, AUTHOR]),
        ("reviewer_id", AUTHOR),
        ("reviewer_model", "other-model"),
        ("reviewer_lab", "other-lab"),
        ("policy_revision", 2),
        ("same_origin_scope", "untrusted"),
        ("scope", "logging-only"),
        ("complete", False),
        ("complete", "true"),
        ("unresolved_p0_p1", 1),
        ("unresolved_p0_p1", False),
        ("schema_version", 2),
    ],
)
def test_receipt_requires_exact_complete_machine_readable_pass(fixture, field, value):
    fixture[1][field] = value
    fixture[2]["content"] = json.dumps(fixture[1])
    with pytest.raises(ValueError):
        prepare(fixture)


@pytest.mark.parametrize(
    "content", ["PASS " + "a" * 40, "{}", '{"decision":"PASS","decision":"FAIL"}']
)
def test_free_form_missing_or_duplicate_fields_fail_closed(fixture, content):
    fixture[2]["content"] = content
    with pytest.raises(ValueError):
        prepare(fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("author_id", AUTHOR),
        ("author_type", "member"),
        ("issue_id", THREAD),
        ("source_task_id", None),
        ("parent_id", ISSUE),
        ("revision", True),
        ("created_at", timestamp(1200)),
        ("updated_at", timestamp(1490)),
        ("content_truncated", True),
        ("folded_reply_count", 1),
    ],
)
def test_native_comment_attribution_and_unfolded_metadata_are_required(fixture, field, value):
    fixture[2][field] = value
    with pytest.raises(ValueError):
        prepare(fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", VERDICT),
        ("agent_id", AUTHOR),
        ("issue_id", THREAD),
        ("workspace_id", ISSUE),
        ("status", "running"),
        ("status", "failed"),
        ("trigger_comment_id", ISSUE),
        ("started_at", timestamp(1410)),
        ("completed_at", timestamp(1390)),
        ("completed_at", timestamp(1510)),
        ("delivered_comment_ids", []),
        ("delivered_comment_ids", None),
    ],
)
def test_verdict_must_map_to_completed_native_review_dispatch(fixture, field, value):
    fixture[4][field] = value
    with pytest.raises(ValueError):
        prepare(fixture)


def test_reviewer_must_differ_from_every_candidate_author_lab(fixture):
    policy = fixture[0]
    second_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    second = control.Participant(second_id, "second-model", "reviewer-lab")
    candidate = replace(policy.candidates[0], author_ids=(AUTHOR, second_id))
    policy = replace(policy, authors=(*policy.authors, second), candidates=(candidate,))
    fixture[1]["author_ids"] = list(candidate.author_ids)
    fixture[2]["content"] = json.dumps(fixture[1])
    fixture = (policy, *fixture[1:])
    with pytest.raises(ValueError, match="lab"):
        prepare(fixture)


@pytest.mark.parametrize(
    "lab", ["REVIEWER-LAB", "ｒｅｖｉｅｗｅｒ-ｌａｂ", "revieωer-lab", "revieвer-lab"]
)
def test_case_width_and_non_ascii_homoglyphs_cannot_fake_lab_diversity(fixture, lab):
    policy = replace(fixture[0], authors=(replace(fixture[0].authors[0], lab=lab),))
    with pytest.raises(ValueError, match="lab"):
        prepare((policy, *fixture[1:]))


def test_scope_assessment_window_is_capped_in_protected_loader(documents):
    policy, registry, scope = documents
    scope["expires_at"] = scope["checked_at"] + control.MAX_SCOPE_WINDOW + 1
    with pytest.raises(ValueError, match="window"):
        control.validate_documents(policy, registry, scope, now=1500)


@pytest.mark.parametrize(
    "ticks,expected_calls", [([2000], 0), ([1500, 2000], 1), ([1500, 1500, 2000], 1)]
)
def test_scope_expiry_is_checked_before_and_after_every_credential_use(
    fixture, ticks, expected_calls
):
    native = NativeCLI(fixture)
    clock = iter(ticks)
    native_reader = receipts.MulticaReader(native, clock=lambda: next(clock))
    with pytest.raises(ValueError, match="scope"):
        receipts.prepare(fixture[0], 70, VERDICT, fixture[-1], native_reader, now=1500)
    assert len(native.commands) == expected_calls


@pytest.mark.parametrize("field", ["workspace_id", "project_id", "id"])
def test_native_issue_must_match_protected_registry(fixture, field):
    fixture[5][field] = THREAD
    with pytest.raises(ValueError, match="issue"):
        prepare(fixture)


def test_native_response_failures_are_not_receipts(fixture):
    for result in (
        subprocess.CompletedProcess([], 1, "[]", "denied"),
        subprocess.CompletedProcess([], 0, "not JSON", ""),
        subprocess.CompletedProcess([], 0, "[" * 2000 + "]" * 2000, ""),
        subprocess.CompletedProcess([], 0, " " * (receipts.MAX_RESPONSE_BYTES + 1), ""),
    ):
        native_reader = reader(lambda *arguments, **options: result)
        with pytest.raises(ValueError):
            receipts.prepare(fixture[0], 70, VERDICT, fixture[-1], native_reader, now=1500)


def test_malformed_native_ancestry_invalidates_instead_of_crashing(fixture):
    record = prepare(fixture)
    fixture[2]["parent_id"] = []
    assert (
        receipts.revalidate(
            record, fixture[0], fixture[-1], reader(NativeCLI(fixture)), now=1501
        ).status
        == "invalidated"
    )


def test_page_budget_never_accepts_partial_history(fixture, monkeypatch):
    monkeypatch.setattr(receipts, "MAX_PAGES", 1)
    cursor = f"Next reply cursor: --before {timestamp(1400)} --before-id {VERDICT}\n"
    native = NativeCLI(fixture, [([fixture[3], fixture[2]], cursor)])
    with pytest.raises(ValueError, match="budget"):
        prepare(fixture, native)


def test_pagination_finishes_even_when_verdict_is_on_first_page(fixture):
    newer = copy.deepcopy(fixture[2])
    newer.update(id=NEW_VERDICT, created_at=timestamp(1401), updated_at=timestamp(1401))
    cursor = f"Next reply cursor: --before {timestamp(1400)} --before-id {VERDICT}\n"
    native = NativeCLI(fixture, [([fixture[3], fixture[2]], cursor), ([fixture[3], newer], "")])
    with pytest.raises(ValueError, match="newer reviewer"):
        prepare(fixture, native)


def test_duplicate_comment_identity_and_missing_thread_root_fail_closed(fixture):
    for page in ([fixture[2]], [fixture[3], fixture[2], fixture[2]]):
        with pytest.raises(ValueError):
            prepare(fixture, NativeCLI(fixture, [(page, "")]))


def test_hard_24_hour_ttl_cannot_be_extended_by_revalidation(fixture):
    fixture = (
        replace(fixture[0], scope_checked_at=1500, scope_expires_at=1500 + receipts.TTL_SECONDS),
        *fixture[1:],
    )
    record = prepare(fixture)
    assert record.expires_at == 1500 + 24 * 60 * 60
    assert (
        receipts.revalidate(
            record,
            fixture[0],
            fixture[-1],
            reader(NativeCLI(fixture)),
            now=record.expires_at,
        ).status
        == "invalidated"
    )


@pytest.mark.parametrize("failure", [OSError("offline"), subprocess.TimeoutExpired("multica", 30)])
def test_api_failure_rejects_new_record_and_invalidates_existing(fixture, failure):
    record = prepare(fixture)
    native = NativeCLI(fixture)
    native.failure = failure
    native_reader = reader(native)
    with pytest.raises((OSError, subprocess.SubprocessError)):
        receipts.prepare(fixture[0], 70, VERDICT, fixture[-1], native_reader, now=1500)
    invalid = receipts.revalidate(record, fixture[0], fixture[-1], native_reader, now=1501)
    assert invalid.status == "invalidated"
    assert invalid.reason


@pytest.mark.parametrize(
    "mutation",
    [
        "edit",
        "delete",
        "revision",
        "blocking",
        "policy",
        "head",
        "base",
        "closed",
        "ci",
        "ttl",
        "scope",
    ],
)
def test_revalidation_invalidates_changed_evidence_and_candidate(fixture, mutation):
    record = prepare(fixture)
    policy, body, comment, root, run, issue, state = fixture
    now = 1501
    native = NativeCLI(fixture)
    if mutation == "edit":
        comment["content"] += " "
    elif mutation == "delete":
        native.pages = [([root], "")]
    elif mutation == "revision":
        comment["revision"] = 2
    elif mutation == "blocking":
        newer = copy.deepcopy(comment)
        newer.update(id=NEW_VERDICT, created_at=timestamp(1401), updated_at=timestamp(1401))
        newer["content"] = json.dumps(body | {"decision": "FAIL"})
        native.pages = [([root, comment, newer], "")]
    elif mutation == "policy":
        policy = replace(policy, policy_revision=2)
    elif mutation in {"head", "base"}:
        state = replace(state, **{f"{mutation}_sha": "d" * 40})
    elif mutation == "closed":
        state = replace(state, state="closed")
    elif mutation == "ci":
        state = replace(state, ci_passed=False)
    elif mutation == "ttl":
        now = record.expires_at
    elif mutation == "scope":
        policy = replace(policy, scope_expires_at=now)
    invalid = receipts.revalidate(record, policy, state, reader(native), now=now)
    assert invalid.status == "invalidated"
    assert invalid.id == record.id and invalid.expires_at == record.expires_at


def test_revalidation_retains_expiry_and_never_revives_invalid_record(fixture):
    record = prepare(fixture)
    refreshed = receipts.revalidate(
        record, fixture[0], fixture[-1], reader(NativeCLI(fixture)), now=1501
    )
    assert refreshed == record
    invalid = replace(record, status="invalidated", reason="withdrawn")
    assert (
        receipts.revalidate(invalid, fixture[0], fixture[-1], reader(NativeCLI(fixture)), now=1502)
        == invalid
    )


def test_record_store_is_create_once_and_round_trips_exact_evidence(fixture, tmp_path):
    record = prepare(fixture)
    directory = tmp_path / "admissions"
    directory.mkdir(mode=0o700)
    store = receipts.AdmissionStore(directory, owner_uid=os.getuid(), anchor=tmp_path)
    store.create(record)
    assert store.load(record.id) == record
    assert (directory / f"{record.id}.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        store.create(record)
    invalid = store.invalidate(record.id, "native verification failed")
    assert store.load(record.id) == invalid
    assert invalid.status == "invalidated"


def test_record_store_rejects_symlinks_writable_paths_and_admitted_status(fixture, tmp_path):
    record = prepare(fixture)
    directory = tmp_path / "admissions"
    directory.mkdir(mode=0o700)
    store = receipts.AdmissionStore(directory, owner_uid=os.getuid(), anchor=tmp_path)
    with pytest.raises(ValueError, match="status"):
        store.create(replace(record, status="admitted"))
    directory.chmod(0o777)
    with pytest.raises(ValueError):
        store.create(record)
    directory.chmod(0o700)
    (directory / f"{record.id}.json").symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        store.load(record.id)


@pytest.mark.parametrize(
    "mutation",
    ["owner", "hardlink", "oversize", "private", "fields", "duplicate", "identity", "status"],
)
def test_persisted_records_are_protected_and_strictly_parsed(fixture, tmp_path, mutation):
    record = prepare(fixture)
    directory = tmp_path / "admissions"
    directory.mkdir(mode=0o700)
    store = receipts.AdmissionStore(directory, owner_uid=os.getuid(), anchor=tmp_path)
    store.create(record)
    path = directory / f"{record.id}.json"
    if mutation == "owner":
        store.owner_uid = os.getuid() + 1
    elif mutation == "hardlink":
        os.link(path, directory / "other.json")
    elif mutation == "oversize":
        path.write_bytes(b" " * (control.MAX_BYTES + 1))
    elif mutation == "private":
        path.chmod(0o640)
    elif mutation == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1}')
    else:
        document = json.loads(path.read_text())
        if mutation == "fields":
            document["approved"] = True
        elif mutation == "identity":
            document["id"] = VERDICT
        elif mutation == "status":
            document["status"] = "admitted"
        path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        store.load(record.id)


def test_failed_store_create_preserves_original_and_removes_only_its_scratch(fixture, tmp_path):
    record = prepare(fixture)
    directory = tmp_path / "admissions"
    directory.mkdir(mode=0o700)
    store = receipts.AdmissionStore(directory, owner_uid=os.getuid(), anchor=tmp_path)
    store.create(record)
    with pytest.raises(FileExistsError):
        store.create(replace(record, expires_at=1900))
    assert store.load(record.id) == record
    assert sorted(path.name for path in directory.iterdir()) == [f"{record.id}.json"]
