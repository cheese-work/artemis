"""Verify native Multica evidence and retain protected two-anchor admission records."""

import hashlib
import json
import os
import re
import subprocess
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from scripts import preview_control as control


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 100
TTL_SECONDS = 24 * 60 * 60
RECEIPT_FIELDS = {
    "schema_version",
    "decision",
    "repository",
    "repository_id",
    "pr",
    "head_sha",
    "base_sha",
    "author_ids",
    "reviewer_id",
    "reviewer_model",
    "reviewer_lab",
    "policy_revision",
    "same_origin_scope",
    "scope",
    "complete",
    "unresolved_p0_p1",
}
CURSOR = re.compile(r"Next reply cursor: --before (\S+) --before-id (\S+)")


@dataclass(frozen=True)
class CandidateState:
    repository_id: int
    pr: int
    head_sha: str
    base_sha: str
    state: str
    ci_passed: bool


@dataclass(frozen=True)
class Receipt:
    comment_id: str
    revision: int
    content_sha256: str
    created_at: str
    updated_at: str
    source_task_id: str
    author_id: str
    reviewer_model: str
    reviewer_lab: str
    repository_id: int
    pr: int
    head_sha: str
    base_sha: str
    author_ids: tuple[str, ...]
    issue_id: str
    thread_id: str
    policy_revision: int


@dataclass(frozen=True)
class HumanApproval:
    review_id: int
    approver_id: int
    content_sha256: str
    commit_sha: str
    created_at: str
    submitted_at: str
    updated_at: str


@dataclass(frozen=True)
class Admission:
    schema_version: int
    id: str
    control_sha256: str
    receipt: Receipt
    prepared_at: int
    expires_at: int
    status: str = "pending-human"
    reason: str = ""
    approval: HumanApproval | None = None


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _json(content):
    try:
        return json.loads(
            content, object_pairs_hook=control._unique_object, parse_constant=_invalid_constant
        )
    except RecursionError as error:
        raise ValueError("JSON nesting exceeds parser bound") from error


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("native timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("native timestamp requires an offset")
    return parsed.timestamp()


def _hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fingerprint(policy):
    return _hash(json.dumps(asdict(policy), sort_keys=True, separators=(",", ":")))


class MulticaReader:
    def __init__(self, runner=None, *, clock=None):
        self.runner = subprocess.run if runner is None else runner
        self.clock = time.time if clock is None else clock

    def _read(self, arguments, policy):
        control._scope_window(policy.scope_checked_at, policy.scope_expires_at, self.clock())
        result = self.runner(
            ["multica", *arguments, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        control._scope_window(policy.scope_checked_at, policy.scope_expires_at, self.clock())
        if result.returncode != 0:
            raise ValueError("native Multica read failed")
        if (
            len(result.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES
            or len(result.stderr) > control.MAX_BYTES
        ):
            raise ValueError("native response exceeds bound")
        return _json(result.stdout), result.stderr

    def snapshot(self, policy, candidate):
        issue, _ = self._read(["issue", "get", candidate.issue_id], policy)
        if type(issue) is not dict or any(
            issue.get(field) != expected
            for field, expected in (
                ("id", candidate.issue_id),
                ("workspace_id", policy.workspace_id),
                ("project_id", policy.project_id),
            )
        ):
            raise ValueError("native issue is outside protected workspace/project")
        comments = {}
        cursor = None
        seen_cursors = set()
        for _ in range(MAX_PAGES):
            arguments = [
                "issue",
                "comment",
                "list",
                candidate.issue_id,
                "--thread",
                candidate.thread_id,
                "--tail",
                "30",
            ]
            if cursor:
                arguments.extend(["--before", cursor[0], "--before-id", cursor[1]])
            page, stderr = self._read(arguments, policy)
            if type(page) is not list or len(page) > 31:
                raise ValueError("native comment page is not bounded")
            for comment in page:
                if type(comment) is not dict:
                    raise ValueError("invalid native comment")
                identity = control._uuid(comment.get("id"))
                control._uuid(comment.get("author_id"))
                if comment.get("parent_id") is not None:
                    control._uuid(comment["parent_id"])
                if comment.get("issue_id") != candidate.issue_id or any(
                    comment.get(field)
                    for field in ("content_truncated", "folded_reply_count", "folded_count")
                ):
                    raise ValueError("wrong issue or incomplete native comment")
                if identity in comments:
                    if identity != candidate.thread_id or comments[identity] != comment:
                        raise ValueError("duplicate or changing native comment")
                comments[identity] = comment
            cursor_lines = [line for line in stderr.splitlines() if "cursor" in line.lower()]
            if not cursor_lines:
                break
            if len(cursor_lines) != 1 or not (match := CURSOR.fullmatch(cursor_lines[0])):
                raise ValueError("invalid native reply cursor")
            cursor = match.groups()
            _timestamp(cursor[0])
            control._uuid(cursor[1])
            if cursor in seen_cursors:
                raise ValueError("native reply cursor cycle")
            seen_cursors.add(cursor)
        else:
            raise ValueError("native reply pagination budget exceeded")
        root = comments.get(candidate.thread_id)
        if root is None or root.get("parent_id") is not None:
            raise ValueError("protected review thread root is missing")
        for comment in comments.values():
            current = comment
            visited = set()
            while current["id"] != candidate.thread_id:
                if current["id"] in visited or current.get("parent_id") not in comments:
                    raise ValueError("comment is outside complete protected thread")
                visited.add(current["id"])
                current = comments[current["parent_id"]]
        runs, stderr = self._read(["issue", "runs", candidate.issue_id], policy)
        if type(runs) is not list or len(runs) > control.MAX_ENTRIES or "truncat" in stderr.lower():
            raise ValueError("native run history is incomplete or exceeds bound")
        if any(type(run) is not dict for run in runs):
            raise ValueError("invalid native run history")
        if len({control._uuid(run.get("id")) for run in runs}) != len(runs):
            raise ValueError("duplicate native run")
        return tuple(comments.values()), runs


def _candidate_state(policy, candidate, state):
    control._number(state.repository_id)
    control._number(state.pr)
    control._sha(state.head_sha)
    control._sha(state.base_sha)
    if (
        (state.repository_id, state.pr, state.head_sha, state.base_sha)
        != (policy.repository_id, candidate.pr, candidate.head_sha, candidate.base_sha)
        or state.state != "open"
        or state.ci_passed is not True
    ):
        raise ValueError("candidate changed, closed or required CI is not passing")


def _lab(value):
    normalized = unicodedata.normalize("NFKC", value).casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", normalized):
        raise ValueError("lab provenance requires a canonical ASCII key")
    return normalized


def _verify(policy, candidate, verdict_id, reader, now):
    control._uuid(verdict_id)
    comments, runs = reader.snapshot(policy, candidate)
    selected = [comment for comment in comments if comment["id"] == verdict_id]
    if len(selected) != 1:
        raise ValueError("exact native verdict is missing")
    comment = selected[0]
    reviewer = next(
        (entry for entry in policy.reviewers if entry.id == comment.get("author_id")), None
    )
    if (
        reviewer is None
        or comment.get("author_type") != "agent"
        or comment.get("type") != "comment"
    ):
        raise ValueError("native signing author is not an allowlisted reviewer")
    authors = [entry for entry in policy.authors if entry.id in candidate.author_ids]
    if len(authors) != len(candidate.author_ids) or any(
        author.id == reviewer.id or _lab(author.lab) == _lab(reviewer.lab) for author in authors
    ):
        raise ValueError("reviewer must be independent and from a different lab than every author")
    content = comment.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > control.MAX_BYTES:
        raise ValueError("receipt body is missing or exceeds bound")
    body = _json(content)
    control._fields(body, RECEIPT_FIELDS)
    control._version(body)
    for field in ("repository_id", "pr", "policy_revision"):
        control._number(body[field])
    expected = {
        "decision": "PASS",
        "repository": "cheese-work/artemis",
        "repository_id": policy.repository_id,
        "pr": candidate.pr,
        "head_sha": candidate.head_sha,
        "base_sha": candidate.base_sha,
        "author_ids": list(candidate.author_ids),
        "reviewer_id": reviewer.id,
        "reviewer_model": reviewer.model,
        "reviewer_lab": reviewer.lab,
        "policy_revision": policy.policy_revision,
        "same_origin_scope": "trusted-root-origin-insiders",
        "scope": "path-preview-admission",
    }
    if any(body[field] != value for field, value in expected.items()) or (
        body["complete"] is not True
        or type(body["unresolved_p0_p1"]) is not int
        or body["unresolved_p0_p1"] != 0
    ):
        raise ValueError("receipt is not a complete, exact-candidate PASS without unresolved P0/P1")
    control._number(comment.get("revision"))
    source_task_id = control._uuid(comment.get("source_task_id"))
    mapped = [run for run in runs if run["id"] == source_task_id]
    if len(mapped) != 1:
        raise ValueError("native verdict has no unique source run")
    run = mapped[0]
    if any(
        run.get(field) != expected
        for field, expected in (
            ("agent_id", reviewer.id),
            ("issue_id", candidate.issue_id),
            ("workspace_id", policy.workspace_id),
            ("status", "completed"),
            ("kind", "comment"),
            ("trigger_comment_id", comment["parent_id"]),
        )
    ):
        raise ValueError("native run does not match completed protected reviewer dispatch")
    delivered = run.get("delivered_comment_ids")
    if type(delivered) is not list or comment["parent_id"] not in delivered:
        raise ValueError("native reviewer run did not receive the exact dispatch")
    created = _timestamp(comment.get("created_at"))
    updated = _timestamp(comment.get("updated_at"))
    if (
        not _timestamp(run.get("started_at"))
        <= created
        <= updated
        <= _timestamp(run.get("completed_at"))
        <= now
    ):
        raise ValueError("verdict timestamp is outside the completed reviewer run")
    reviewer_ids = {entry.id for entry in policy.reviewers}
    for other in comments:
        if (
            other["id"] != verdict_id
            and other.get("author_type") == "agent"
            and other.get("author_id") in reviewer_ids
        ):
            if (
                max(_timestamp(other.get("created_at")), _timestamp(other.get("updated_at")))
                >= updated
            ):
                raise ValueError("newer reviewer activity requires a fresh receipt")
    return Receipt(
        verdict_id,
        comment["revision"],
        _hash(content),
        comment["created_at"],
        comment["updated_at"],
        source_task_id,
        reviewer.id,
        reviewer.model,
        reviewer.lab,
        policy.repository_id,
        candidate.pr,
        candidate.head_sha,
        candidate.base_sha,
        candidate.author_ids,
        candidate.issue_id,
        candidate.thread_id,
        policy.policy_revision,
    )


def prepare(policy, pr, verdict_id, state, reader, *, now=None):
    observed = int(time.time()) if now is None else control._number(now)
    control._scope_window(policy.scope_checked_at, policy.scope_expires_at, observed)
    candidate = policy.candidate(pr)
    _candidate_state(policy, candidate, state)
    receipt = _verify(policy, candidate, verdict_id, reader, observed)
    return Admission(
        1,
        str(uuid.uuid4()),
        _fingerprint(policy),
        receipt,
        observed,
        min(observed + TTL_SECONDS, policy.scope_expires_at),
    )


def revalidate(record, policy, state, reader, *, now=None):
    if record.status == "invalidated":
        return record
    try:
        observed = int(time.time()) if now is None else control._number(now)
        if (
            record.status != "pending-human"
            or not record.prepared_at <= observed < record.expires_at
        ):
            raise ValueError("admission record is stale or unavailable")
        if record.control_sha256 != _fingerprint(policy):
            raise ValueError("protected policy or provenance changed")
        fresh = prepare(
            policy, record.receipt.pr, record.receipt.comment_id, state, reader, now=observed
        )
        if fresh.receipt != record.receipt:
            raise ValueError("native verdict revision, content or attribution changed")
        return record
    except (ValueError, OSError, subprocess.SubprocessError):
        return replace(
            record,
            status="invalidated",
            reason="candidate, policy or native evidence failed revalidation",
        )


def require_admitted(record, *, now=None):
    observed = int(time.time()) if now is None else control._number(now)
    if (
        record.status != "admitted"
        or record.approval is None
        or not record.prepared_at <= observed < record.expires_at
    ):
        raise ValueError("admission requires a live, verified L5a3 human GitHub anchor")
    _record(_json(json.dumps(asdict(record))))
    return record


def _record(document):
    control._fields(document, set(Admission.__dataclass_fields__))
    control._version(document)
    receipt = document["receipt"]
    control._fields(receipt, set(Receipt.__dataclass_fields__))
    control._uuid(document["id"])
    if not isinstance(document["control_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", document["control_sha256"]
    ):
        raise ValueError("invalid record fingerprint")
    start = control._number(document["prepared_at"])
    end = control._number(document["expires_at"])
    if not start < end <= start + TTL_SECONDS:
        raise ValueError("invalid admission expiry")
    if type(document["status"]) is not str or document["status"] not in {
        "pending-human",
        "invalidated",
        "admitted",
    }:
        raise ValueError("unsupported admission status")
    approval = document["approval"]
    if document["status"] == "admitted" and approval is None:
        raise ValueError("admitted status requires verified human approval")
    if document["status"] == "pending-human" and approval is not None:
        raise ValueError("pending status cannot contain a human approval")
    if approval is not None:
        control._fields(approval, set(HumanApproval.__dataclass_fields__))
        for field in ("review_id", "approver_id"):
            control._number(approval[field])
        control._sha(approval["commit_sha"])
        if not isinstance(approval["content_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", approval["content_sha256"]
        ):
            raise ValueError("invalid human content hash")
        if (
            approval["commit_sha"] != receipt["head_sha"]
            or not _timestamp(receipt["updated_at"])
            <= _timestamp(approval["created_at"])
            <= _timestamp(approval["submitted_at"])
            <= _timestamp(approval["updated_at"])
            <= start
        ):
            raise ValueError("human approval is outside the admitted tuple or timestamps")
        approval = HumanApproval(**approval)
    reason = document["reason"]
    if (
        not isinstance(reason, str)
        or len(reason) > 256
        or (document["status"] == "invalidated") != bool(reason)
    ):
        raise ValueError("invalid invalidation reason")
    for field in ("comment_id", "source_task_id", "author_id", "issue_id", "thread_id"):
        control._uuid(receipt[field])
    for field in ("revision", "repository_id", "pr", "policy_revision"):
        control._number(receipt[field])
    for field in ("head_sha", "base_sha"):
        control._sha(receipt[field])
    if not isinstance(receipt["content_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", receipt["content_sha256"]
    ):
        raise ValueError("invalid native content hash")
    if not _timestamp(receipt["created_at"]) <= _timestamp(receipt["updated_at"]) <= start:
        raise ValueError("invalid record timestamps")
    control._participants(
        [
            {
                "id": receipt["author_id"],
                "model": receipt["reviewer_model"],
                "lab": receipt["reviewer_lab"],
            }
        ]
    )
    author_ids = receipt["author_ids"]
    if type(author_ids) is not list or not 1 <= len(author_ids) <= 256:
        raise ValueError("invalid record authors")
    for identity in author_ids:
        control._uuid(identity)
    if len(set(author_ids)) != len(author_ids) or receipt["author_id"] in author_ids:
        raise ValueError("record reviewer is not independent")
    return Admission(
        **(
            document
            | {
                "receipt": Receipt(**(receipt | {"author_ids": tuple(author_ids)})),
                "approval": approval,
            }
        )
    )


class AdmissionStore:
    def __init__(
        self,
        directory=Path("/var/lib/artemis-preview/admissions"),
        *,
        owner_uid=None,
        anchor=Path("/"),
    ):
        self.directory = Path(directory).absolute()
        self.anchor = Path(anchor).absolute()
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid

    def _open(self):
        parts = self.directory.relative_to(self.anchor).parts
        if ".." in parts:
            raise ValueError("admission path traversal")
        descriptor = os.open(self.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in (None, *parts):
                if part is not None:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    os.close(descriptor)
                    descriptor = child
                metadata = os.fstat(descriptor)
                if metadata.st_uid not in {0, self.owner_uid}:
                    raise ValueError("wrong admission ancestor owner")
                control._protected(metadata, metadata.st_uid, directory=True)
            metadata = os.fstat(descriptor)
            if metadata.st_uid != self.owner_uid or metadata.st_mode & 0o077:
                raise ValueError("admission directory must be daemon-private")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _load(self, descriptor, identity):
        child = os.open(
            f"{control._uuid(identity)}.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        with os.fdopen(child, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            control._protected(metadata, self.owner_uid)
            if metadata.st_mode & 0o077:
                raise ValueError("admission record must be daemon-private")
            content = stream.read(control.MAX_BYTES + 1)
        if len(content) > control.MAX_BYTES:
            raise ValueError("admission record exceeds bound")
        result = _record(_json(content.decode("utf-8")))
        if result.id != identity:
            raise ValueError("admission filename identity mismatch")
        return result

    def load(self, identity):
        descriptor = self._open()
        try:
            return self._load(descriptor, identity)
        finally:
            os.close(descriptor)

    def _write(self, descriptor, record, *, create):
        content = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"))
        _record(_json(content))
        if len(content.encode("utf-8")) > control.MAX_BYTES:
            raise ValueError("admission record exceeds bound")
        temporary = f"{uuid.uuid4()}.tmp"
        child = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        try:
            with os.fdopen(child, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            target = f"{record.id}.json"
            if create:
                os.link(
                    temporary,
                    target,
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                    follow_symlinks=False,
                )
            else:
                os.replace(temporary, target, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            if create:
                os.unlink(temporary, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass

    def create(self, record):
        descriptor = self._open()
        try:
            self._write(descriptor, record, create=True)
        finally:
            os.close(descriptor)

    def invalidate(self, identity, reason):
        descriptor = self._open()
        try:
            record = self._load(descriptor, identity)
            invalid = replace(record, status="invalidated", reason=reason)
            self._write(descriptor, invalid, create=False)
            return invalid
        finally:
            os.close(descriptor)
