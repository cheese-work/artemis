"""Fetch the independent human GitHub anchor; never submit reviews or dispatch builds."""

import subprocess
import time
from dataclasses import replace

from scripts import preview_control as control
from scripts import preview_receipts as receipts


QUERY = """
query($pr:Int!, $after:String) {
  repository(owner:"cheese-work", name:"artemis") {
    databaseId nameWithOwner
    pullRequest(number:$pr) {
      number state headRefOid baseRefOid isCrossRepository mergeStateStatus
      headRepository { databaseId }
      commits(last:1) { nodes { commit { oid statusCheckRollup { state } } } }
      reviews(first:100, after:$after) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes {
          databaseId:fullDatabaseId state body createdAt submittedAt updatedAt lastEditedAt
          author { __typename ... on User { databaseId } }
          commit { oid }
        }
      }
    }
  }
}
"""


def _review_id(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and value[0] != "0":
        value = int(value)
    return control._number(value)


class GitHubReader:
    def __init__(self, runner=None, *, clock=None):
        self.runner = subprocess.run if runner is None else runner
        self.clock = time.time if clock is None else clock

    def _read(self, policy, pr, cursor):
        control._scope_window(policy.scope_checked_at, policy.scope_expires_at, self.clock())
        command = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-F", f"pr={pr}"]
        if cursor is not None:
            command.extend(["-f", f"after={cursor}"])
        result = self.runner(command, capture_output=True, text=True, timeout=30, check=False)
        control._scope_window(policy.scope_checked_at, policy.scope_expires_at, self.clock())
        if (
            result.returncode != 0
            or len(result.stdout.encode("utf-8")) > receipts.MAX_RESPONSE_BYTES
            or len(result.stderr.encode("utf-8")) > control.MAX_BYTES
        ):
            raise ValueError("native GitHub read failed or exceeded bound")
        document = receipts._json(result.stdout)
        if type(document) is not dict or document.get("errors"):
            raise ValueError("native GitHub response is incomplete")
        try:
            repository = document["data"]["repository"]
            if (
                type(repository) is not dict
                or control._number(repository.get("databaseId")) != policy.repository_id
                or repository.get("nameWithOwner") != "cheese-work/artemis"
            ):
                raise ValueError("native repository does not match protected policy")
            return repository["pullRequest"]
        except (KeyError, TypeError) as error:
            raise ValueError("native GitHub candidate is missing") from error

    def snapshot(self, policy, pr):
        candidate = policy.candidate(pr)
        reviews = {}
        cursor = None
        seen_cursors = set()
        expected_count = None
        for _ in range(receipts.MAX_PAGES):
            pull = self._read(policy, pr, cursor)
            if type(pull) is not dict or type(pull.get("headRepository")) is not dict:
                raise ValueError("native GitHub candidate is missing")
            commits = pull.get("commits")
            if type(commits) is not dict or commits.get("nodes") != [
                {"commit": {"oid": candidate.head_sha, "statusCheckRollup": {"state": "SUCCESS"}}}
            ]:
                raise ValueError("exact-head CI result is absent, pending or not passing")
            control._number(pull.get("number"))
            control._number(pull["headRepository"].get("databaseId"))
            if (
                pull["number"] != pr
                or pull.get("state") != "OPEN"
                or pull.get("headRefOid") != candidate.head_sha
                or pull.get("baseRefOid") != candidate.base_sha
                or pull["headRepository"]["databaseId"] != policy.repository_id
                or pull.get("isCrossRepository") is not False
                or pull.get("mergeStateStatus") != "CLEAN"
            ):
                raise ValueError("candidate changed, forked, closed or CI is not clean")
            connection = pull.get("reviews")
            if type(connection) is not dict:
                raise ValueError("native review history is missing")
            count = connection.get("totalCount")
            if type(count) is not int or not 0 <= count <= receipts.MAX_PAGES * 100:
                raise ValueError("native review history exceeds bound")
            if expected_count is not None and count != expected_count:
                raise ValueError("native review history changed during pagination")
            expected_count = count
            nodes = connection.get("nodes")
            if type(nodes) is not list or len(nodes) > 100:
                raise ValueError("native review page exceeds bound")
            for review in nodes:
                if type(review) is not dict:
                    raise ValueError("invalid native review")
                if type(review.get("state")) is not str or review["state"] not in {
                    "APPROVED",
                    "CHANGES_REQUESTED",
                    "COMMENTED",
                    "DISMISSED",
                    "PENDING",
                }:
                    raise ValueError("native review state is incomplete")
                identity = _review_id(review.get("databaseId"))
                if identity in reviews:
                    raise ValueError("duplicate or changing native review")
                reviews[identity] = review
            page = connection.get("pageInfo")
            if type(page) is not dict or type(page.get("hasNextPage")) is not bool:
                raise ValueError("native review pagination is incomplete")
            if not page["hasNextPage"]:
                break
            cursor = page.get("endCursor")
            if (
                not isinstance(cursor, str)
                or not 1 <= len(cursor) <= 1024
                or cursor in seen_cursors
                or not nodes
            ):
                raise ValueError("native review pagination cursor is invalid")
            seen_cursors.add(cursor)
        else:
            raise ValueError("native review pagination budget exceeded")
        if len(reviews) != expected_count:
            raise ValueError("native review history is incomplete")
        return receipts.CandidateState(
            policy.repository_id, pr, candidate.head_sha, candidate.base_sha, "open", True
        ), reviews


def _verify(policy, receipt, review_id, reviews, now):
    review = reviews.get(control._number(review_id))
    if review is None:
        raise ValueError("exact native human review is missing")
    author = review.get("author")
    if (
        type(author) is not dict
        or author.get("__typename") != "User"
        or control._number(author.get("databaseId")) not in policy.human_approver_ids
        or review.get("state") != "APPROVED"
        or review.get("commit") != {"oid": receipt.head_sha}
        or "lastEditedAt" not in review
        or review["lastEditedAt"] is not None
    ):
        raise ValueError("native approval is not an unedited allowlisted human exact-head approval")
    created = receipts._timestamp(review.get("createdAt"))
    submitted = receipts._timestamp(review.get("submittedAt"))
    updated = receipts._timestamp(review.get("updatedAt"))
    if not receipts._timestamp(receipt.updated_at) <= created <= submitted <= updated <= now:
        raise ValueError("human approval is stale, future-dated or predates the reviewer receipt")
    content = review.get("body")
    if not isinstance(content, str) or len(content.encode("utf-8")) > control.MAX_BYTES:
        raise ValueError("human approval body is missing or exceeds bound")
    expected = {
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
    body = receipts._json(content)
    control._fields(body, set(expected))
    if any(
        type(body[field]) is not type(value) or body[field] != value
        for field, value in expected.items()
    ):
        raise ValueError(
            "human approval does not bind the exact candidate and native reviewer receipt"
        )
    for identity, other in reviews.items():
        if identity == review_id or other.get("state") == "PENDING":
            continue
        activity = max(
            receipts._timestamp(other.get("submittedAt")),
            receipts._timestamp(other.get("updatedAt")),
        )
        if activity >= submitted and (
            other.get("state") == "CHANGES_REQUESTED" or other.get("author") == author
        ):
            raise ValueError("newer blocking or approver review requires a fresh human approval")
    return receipts.HumanApproval(
        review_id,
        author["databaseId"],
        receipts._hash(content),
        receipt.head_sha,
        review["createdAt"],
        review["submittedAt"],
        review["updatedAt"],
    )


def admit(policy, pr, verdict_id, review_id, multica_reader, github_reader, *, now=None):
    control._number(review_id)
    control._uuid(verdict_id)
    state, reviews = github_reader.snapshot(policy, pr)
    observed = int(time.time()) if now is None else control._number(now)
    record = receipts.prepare(policy, pr, verdict_id, state, multica_reader, now=observed)
    state, reviews = github_reader.snapshot(policy, pr)
    receipts._candidate_state(policy, policy.candidate(pr), state)
    approval = _verify(policy, record.receipt, review_id, reviews, observed)
    result = replace(record, status="admitted", approval=approval)
    return receipts.require_admitted(result, now=observed if now is not None else None)


def revalidate(record, policy, multica_reader, github_reader, *, now=None):
    if record.status == "invalidated":
        return record
    try:
        observed = int(time.time()) if now is None else control._number(now)
        receipts.require_admitted(record, now=observed)
        if record.control_sha256 != receipts._fingerprint(policy):
            raise ValueError("protected policy or human custody allowlist changed")
        fresh = admit(
            policy,
            record.receipt.pr,
            record.receipt.comment_id,
            record.approval.review_id,
            multica_reader,
            github_reader,
            now=observed,
        )
        if fresh.receipt != record.receipt or fresh.approval != record.approval:
            raise ValueError("native reviewer or human approval evidence changed")
        return receipts.require_admitted(record, now=observed if now is not None else None)
    except (ValueError, OSError, subprocess.SubprocessError):
        return replace(
            record,
            status="invalidated",
            reason="candidate, policy or either approval anchor failed revalidation",
        )
