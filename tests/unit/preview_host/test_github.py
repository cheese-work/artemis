import copy
import json
import os
import subprocess
from dataclasses import asdict, replace

import pytest

from scripts import preview_control as control
from scripts import preview_github as github
from scripts import preview_receipts as receipts
from tests.unit.preview_host import test_receipts as receipt_fixtures
from tests.unit.preview_host.test_receipts import (
    VERDICT,
    NativeCLI,
    prepare,
    reader,
    timestamp,
)


HUMAN = 123456
REVIEW = 987654
documents = receipt_fixtures.documents
fixture = receipt_fixtures.fixture


@pytest.fixture
def approval(fixture):
    policy = replace(fixture[0], human_approver_ids=(HUMAN,))
    fixture = (policy, *fixture[1:])
    record = prepare(fixture)
    receipt = record.receipt
    body = {
        "schema_version": 1,
        "decision": "APPROVED",
        "repository": "cheese-work/artemis",
        "repository_id": receipt.repository_id,
        "pr": receipt.pr,
        "head_sha": receipt.head_sha,
        "base_sha": receipt.base_sha,
        "verdict_id": receipt.comment_id,
        "verdict_revision": receipt.revision,
        "verdict_content_sha256": receipt.content_sha256,
        "source_task_id": receipt.source_task_id,
        "author_ids": list(receipt.author_ids),
        "reviewer_id": receipt.author_id,
        "reviewer_model": receipt.reviewer_model,
        "reviewer_lab": receipt.reviewer_lab,
        "policy_revision": receipt.policy_revision,
        "same_origin_scope": "trusted-root-origin-insiders",
        "scope": "path-preview-admission",
        "complete": True,
    }
    review = {
        "databaseId": str(REVIEW),
        "author": {"__typename": "User", "databaseId": HUMAN},
        "state": "APPROVED",
        "body": json.dumps(body),
        "commit": {"oid": receipt.head_sha},
        "createdAt": timestamp(1455),
        "submittedAt": timestamp(1460),
        "updatedAt": timestamp(1460),
        "lastEditedAt": None,
    }
    candidate = {
        "number": 70,
        "state": "OPEN",
        "headRefOid": receipt.head_sha,
        "baseRefOid": receipt.base_sha,
        "headRepository": {"databaseId": policy.repository_id},
        "isCrossRepository": False,
        "mergeStateStatus": "CLEAN",
        "commits": {
            "nodes": [
                {"commit": {"oid": receipt.head_sha, "statusCheckRollup": {"state": "SUCCESS"}}}
            ]
        },
    }
    return fixture, body, review, candidate


class GitHubCLI:
    def __init__(self, approval, pages=None):
        self.fixture, self.body, self.review, self.candidate = approval
        self.pages = pages
        self.commands = []
        self.failure = None
        self.transform = lambda result: result

    def __call__(self, command, **options):
        self.commands.append(command)
        assert command[:3] == ["gh", "api", "graphql"]
        assert options["timeout"] == 30 and options["capture_output"] is True
        if self.failure:
            raise self.failure
        nodes, cursor, more = [self.review], None, False
        if self.pages:
            index = 1 if "after=next" in command else 0
            nodes, cursor, more = self.pages[index]
        connection = {
            "nodes": nodes,
            "pageInfo": {"endCursor": cursor, "hasNextPage": more},
            "totalCount": 2 if self.pages else 1,
        }
        result = {
            "data": {
                "repository": {
                    "databaseId": self.fixture[0].repository_id,
                    "nameWithOwner": "cheese-work/artemis",
                    "pullRequest": self.candidate | {"reviews": connection},
                }
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(self.transform(result)), "")


def github_reader(native):
    return github.GitHubReader(native, clock=lambda: 1500)


def admit(approval, native=None):
    native = GitHubCLI(approval) if native is None else native
    fixture = approval[0]
    return github.admit(
        fixture[0], 70, VERDICT, REVIEW, reader(NativeCLI(fixture)), github_reader(native), now=1500
    )


def test_exact_human_anchor_admits_and_round_trips_protected_store(approval, tmp_path):
    record = admit(approval)
    assert record.status == "admitted"
    assert record.approval.review_id == REVIEW
    assert record.approval.approver_id == HUMAN
    assert record.approval.content_sha256 == receipts._hash(approval[2]["body"])
    assert receipts.require_admitted(record, now=1501) == record
    directory = tmp_path / "admissions"
    directory.mkdir(mode=0o700)
    store = receipts.AdmissionStore(directory, owner_uid=os.getuid(), anchor=tmp_path)
    store.create(record)
    assert store.load(record.id) == record
    with pytest.raises(FileExistsError):
        store.create(record)


@pytest.mark.parametrize("identities", [[], [True], ["123456"], [0], [-1], [HUMAN, HUMAN]])
def test_protected_policy_rejects_unsafe_human_allowlist(documents, identities):
    documents[0]["human_approver_ids"] = identities
    with pytest.raises(ValueError):
        control.validate_documents(*documents, now=1500)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "DISMISSED"),
        ("state", "PENDING"),
        ("state", "COMMENTED"),
        ("author", {"__typename": "Bot", "databaseId": HUMAN}),
        ("author", {"__typename": "User", "databaseId": HUMAN + 1}),
        ("author", {"__typename": "User", "databaseId": str(HUMAN)}),
        ("commit", {"oid": "c" * 40}),
        ("lastEditedAt", timestamp(1470)),
        ("submittedAt", timestamp(1390)),
        ("updatedAt", timestamp(1600)),
        ("body", "Generic merge approval"),
        ("body", '{"decision":"APPROVED","decision":"APPROVED"}'),
        ("databaseId", True),
    ],
)
def test_native_approval_rejects_ineligible_stale_or_edited_reviews(approval, field, value):
    approval[2][field] = value
    with pytest.raises(ValueError):
        admit(approval)


@pytest.mark.parametrize(
    "field",
    [
        "repository_id",
        "pr",
        "head_sha",
        "base_sha",
        "verdict_id",
        "verdict_revision",
        "verdict_content_sha256",
        "source_task_id",
        "author_ids",
        "reviewer_id",
        "reviewer_model",
        "reviewer_lab",
        "policy_revision",
        "same_origin_scope",
        "scope",
        "complete",
        "schema_version",
    ],
)
def test_every_human_body_binding_is_mandatory(approval, field):
    body = approval[1]
    body[field] = False if type(body[field]) in {int, bool} else "different"
    approval[2]["body"] = json.dumps(body)
    with pytest.raises(ValueError):
        admit(approval)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "CLOSED"),
        ("headRefOid", "c" * 40),
        ("baseRefOid", "d" * 40),
        ("isCrossRepository", True),
        ("headRepository", {"databaseId": 1}),
        ("mergeStateStatus", "UNKNOWN"),
        ("mergeStateStatus", "BLOCKED"),
        ("mergeStateStatus", "UNSTABLE"),
    ],
)
def test_fresh_candidate_and_native_ci_gate(approval, field, value):
    approval[3][field] = value
    with pytest.raises(ValueError):
        admit(approval)


@pytest.mark.parametrize("rollup", [None, {}, {"state": "PENDING"}, {"state": "FAILURE"}])
def test_clean_merge_state_is_not_proof_of_a_passing_ci_result(approval, rollup):
    approval[3]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = rollup
    with pytest.raises(ValueError):
        admit(approval)


def test_all_pages_are_read_and_later_blocking_review_rejects(approval):
    later = copy.deepcopy(approval[2])
    later.update(databaseId=str(REVIEW + 1), state="CHANGES_REQUESTED", submittedAt=timestamp(1470))
    native = GitHubCLI(approval, [([approval[2]], "next", True), ([later], None, False)])
    with pytest.raises(ValueError):
        admit(approval, native)
    assert len(native.commands) == 4


def test_missing_deleted_review_is_not_approval(approval):
    approval[2]["databaseId"] = str(REVIEW + 1)
    with pytest.raises(ValueError):
        admit(approval)


def test_dismissal_during_multica_fetch_cannot_use_earlier_github_snapshot(approval):
    fixture = approval[0]
    native = NativeCLI(fixture)

    def dismiss_during_read(command, **options):
        result = native(command, **options)
        if command[2] == "runs":
            approval[2]["state"] = "DISMISSED"
        return result

    with pytest.raises(ValueError):
        github.admit(
            fixture[0],
            70,
            VERDICT,
            REVIEW,
            reader(dismiss_during_read),
            github_reader(GitHubCLI(approval)),
            now=1500,
        )


@pytest.mark.parametrize("change", ["body", "dismissed", "verdict", "policy", "outage", "expired"])
def test_revalidation_invalidates_both_anchor_changes(approval, change):
    record = admit(approval)
    fixture, body, review, candidate = approval
    native = GitHubCLI(approval)
    policy = fixture[0]
    observed = 1501
    if change == "body":
        review["body"] += " "
    elif change == "dismissed":
        review["state"] = "DISMISSED"
    elif change == "verdict":
        fixture[2]["revision"] += 1
    elif change == "policy":
        policy = replace(policy, human_approver_ids=(HUMAN + 1,))
    elif change == "outage":
        native.failure = subprocess.TimeoutExpired("gh", 30)
    else:
        observed = record.expires_at
    invalid = github.revalidate(
        record, policy, reader(NativeCLI(fixture)), github_reader(native), now=observed
    )
    assert invalid.status == "invalidated"
    with pytest.raises(ValueError):
        receipts.require_admitted(invalid, now=observed)
    assert github.revalidate(invalid, policy, None, None, now=observed) == invalid


def test_unchanged_anchor_revalidation_preserves_id_and_expiry(approval):
    record = admit(approval)
    fixture = approval[0]
    assert (
        github.revalidate(
            record,
            fixture[0],
            reader(NativeCLI(fixture)),
            github_reader(GitHubCLI(approval)),
            now=1501,
        )
        == record
    )
    with pytest.raises(ValueError):
        receipts.require_admitted(record, now=record.expires_at)


def test_expired_scope_during_github_call_stops_further_credential_use(approval):
    native = GitHubCLI(approval)
    ticks = iter([1500, 2000])
    client = github.GitHubReader(native, clock=lambda: next(ticks))
    with pytest.raises(ValueError):
        client.snapshot(approval[0][0], 70)
    assert len(native.commands) == 1


def test_admitted_status_cannot_be_forged_without_second_anchor(approval):
    record = prepare(approval[0])
    with pytest.raises(ValueError):
        receipts._record(asdict(replace(record, status="admitted")))


@pytest.mark.parametrize(
    "mutation",
    [
        "errors",
        "missing",
        "wrong-repository",
        "boolean-repository",
        "unknown-state",
        "duplicate",
        "count",
        "cursor",
        "cursor-cycle",
        "malformed-review",
        "oversize",
    ],
)
def test_incomplete_or_hostile_native_github_reads_fail_closed(approval, mutation):
    native = GitHubCLI(approval)

    def transform(result):
        repository = result["data"]["repository"]
        connection = repository["pullRequest"]["reviews"]
        if mutation == "errors":
            result["errors"] = [{"message": "partial read"}]
        elif mutation == "missing":
            repository["pullRequest"] = None
        elif mutation == "wrong-repository":
            repository["databaseId"] = 1
        elif mutation == "boolean-repository":
            repository["databaseId"] = True
        elif mutation == "unknown-state":
            connection["nodes"][0]["state"] = None
        elif mutation == "duplicate":
            connection["nodes"] *= 2
        elif mutation == "count":
            connection["totalCount"] += 1
        elif mutation in {"cursor", "cursor-cycle"}:
            connection["pageInfo"] = {
                "hasNextPage": True,
                "endCursor": None if mutation == "cursor" else "next",
            }
            if len(native.commands) > 1:
                connection["nodes"][0]["databaseId"] = str(REVIEW + 1)
        elif mutation == "malformed-review":
            connection["nodes"] = [None]
        else:
            result["padding"] = " " * receipts.MAX_RESPONSE_BYTES
        return result

    native.transform = transform
    with pytest.raises(ValueError):
        admit(approval, native)


def test_later_approval_can_be_found_on_second_native_page(approval):
    earlier = copy.deepcopy(approval[2])
    earlier.update(
        databaseId=str(REVIEW + 1),
        state="COMMENTED",
        submittedAt=timestamp(1300),
        updatedAt=timestamp(1300),
    )
    native = GitHubCLI(approval, [([earlier], "next", True), ([approval[2]], None, False)])
    assert admit(approval, native).approval.review_id == REVIEW
    assert "after=next" in native.commands[1]


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("gh", 30), OSError("unavailable")])
def test_api_failure_never_creates_admission(approval, failure):
    native = GitHubCLI(approval)
    native.failure = failure
    with pytest.raises((OSError, subprocess.SubprocessError)):
        admit(approval, native)


@pytest.mark.parametrize(
    "field,value",
    [
        ("receipt", None),
        ("approval", {}),
        ("approval", {"review_id": True}),
    ],
)
def test_malformed_protected_anchor_fails_as_validation_error(approval, field, value):
    document = json.loads(json.dumps(asdict(admit(approval))))
    document[field] = value
    with pytest.raises(ValueError):
        receipts._record(document)
