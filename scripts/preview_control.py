"""Read protected preview configuration; never admit, provision or start a preview."""

import argparse
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


MAX_BYTES = 128 * 1024
MAX_ENTRIES = 1000
MAX_SCOPE_WINDOW = 24 * 60 * 60
DOCUMENT_NAMES = ("policy.json", "registry.json", "scope.json")
READ_PERMISSIONS = frozenset({"issue.read", "comment.read", "task.read", "agent.read"})
POLICY_FIELDS = {
    "schema_version",
    "revision",
    "workspace_id",
    "project_id",
    "repository_id",
    "repository",
    "controller_sha",
    "same_origin_scope",
    "authors",
    "reviewers",
    "platform_identity_id",
    "platform_credential_id",
}
SCOPE_FIELDS = {
    "schema_version",
    "identity_id",
    "credential_id",
    "workspace_id",
    "project_ids",
    "permissions",
    "complete",
    "supported_identity",
    "exclusive_custody",
    "checked_at",
    "expires_at",
}


@dataclass(frozen=True)
class Participant:
    id: str
    model: str
    lab: str


@dataclass(frozen=True)
class Candidate:
    pr: int
    issue_id: str
    thread_id: str
    head_sha: str
    base_sha: str
    author_ids: tuple[str, ...]


@dataclass(frozen=True)
class Control:
    policy_revision: int
    repository_id: int
    controller_sha: str
    workspace_id: str
    project_id: str
    platform_identity_id: str
    platform_credential_id: str
    scope_checked_at: int
    scope_expires_at: int
    authors: tuple[Participant, ...]
    reviewers: tuple[Participant, ...]
    candidates: tuple[Candidate, ...]

    def candidate(self, pr: int) -> Candidate:
        _number(pr)
        for candidate in self.candidates:
            if candidate.pr == pr:
                return candidate
        raise ValueError("PR is not registered in protected policy")


def _number(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("expected a bounded positive integer")
    return value


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).int == 0:
        raise ValueError("expected a canonical nonzero UUID")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("expected a full lowercase commit SHA")
    return value


def _fields(document, fields):
    if type(document) is not dict or set(document) != fields:
        raise ValueError("unknown or missing schema fields")


def _version(document):
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported schema version")


def _scope_window(checked_at, expires_at, observed_at):
    _number(checked_at)
    _number(expires_at)
    if not checked_at <= observed_at < expires_at:
        raise ValueError("credential-scope assessment is stale or future-dated")
    if expires_at - checked_at > MAX_SCOPE_WINDOW:
        raise ValueError("credential-scope assessment window exceeds 24 hours")


def _participants(entries):
    if type(entries) is not list or not 1 <= len(entries) <= 256:
        raise ValueError("participant registry must be bounded and nonempty")
    result = []
    for entry in entries:
        _fields(entry, {"id", "model", "lab"})
        _uuid(entry["id"])
        for field in ("model", "lab"):
            if not isinstance(entry[field], str) or not re.fullmatch(
                r"[\w.-]{1,128}", entry[field]
            ):
                raise ValueError("invalid participant provenance")
        result.append(Participant(**entry))
    if len({entry.id for entry in result}) != len(result):
        raise ValueError("duplicate participant")
    return tuple(result)


def validate_documents(policy, registry, scope, *, now=None) -> Control:
    _fields(policy, POLICY_FIELDS)
    _version(policy)
    for field in ("revision", "repository_id"):
        _number(policy[field])
    for field in ("workspace_id", "project_id", "platform_identity_id", "platform_credential_id"):
        _uuid(policy[field])
    _sha(policy["controller_sha"])
    if policy["repository"] != "cheese-work/artemis":
        raise ValueError("repository outside preview policy")
    if policy["same_origin_scope"] != "trusted-root-origin-insiders":
        raise ValueError("same-origin risk acceptance scope is missing")
    authors = _participants(policy["authors"])
    reviewers = _participants(policy["reviewers"])
    author_ids = {entry.id for entry in authors}
    reviewer_ids = {entry.id for entry in reviewers}
    if author_ids & reviewer_ids or policy["platform_identity_id"] in author_ids | reviewer_ids:
        raise ValueError("daemon identity and author/reviewer identities must be distinct")

    _fields(scope, SCOPE_FIELDS)
    _version(scope)
    for scope_field, policy_field in (
        ("identity_id", "platform_identity_id"),
        ("credential_id", "platform_credential_id"),
        ("workspace_id", "workspace_id"),
    ):
        if scope[scope_field] != policy[policy_field]:
            raise ValueError("credential identity or workspace scope mismatch")
    if scope["project_ids"] != [policy["project_id"]]:
        raise ValueError("credential must be scoped to the preview project only")
    permissions = scope["permissions"]
    if (
        type(permissions) is not list
        or any(type(permission) is not str for permission in permissions)
        or len(permissions) != len(READ_PERMISSIONS)
        or set(permissions) != READ_PERMISSIONS
    ):
        raise ValueError("credential requires exactly the four verification reads, no writes")
    if any(
        scope[field] is not True
        for field in ("complete", "supported_identity", "exclusive_custody")
    ):
        raise ValueError("platform owner has not verified effective scope and exclusive custody")
    checked_at = _number(scope["checked_at"])
    expires_at = _number(scope["expires_at"])
    observed_at = time.time() if now is None else now
    _scope_window(checked_at, expires_at, observed_at)

    _fields(registry, {"schema_version", "policy_revision", "entries"})
    _version(registry)
    if _number(registry["policy_revision"]) != policy["revision"]:
        raise ValueError("registry policy revision mismatch")
    entries = registry["entries"]
    if type(entries) is not list or len(entries) > MAX_ENTRIES:
        raise ValueError("PR registry exceeds bound")
    candidates = []
    for entry in entries:
        _fields(entry, {"pr", "issue_id", "thread_id", "head_sha", "base_sha", "author_ids"})
        _number(entry["pr"])
        _uuid(entry["issue_id"])
        _uuid(entry["thread_id"])
        _sha(entry["head_sha"])
        _sha(entry["base_sha"])
        if (
            type(entry["author_ids"]) is not list
            or not 1 <= len(entry["author_ids"]) <= len(authors)
            or any(type(identity) is not str for identity in entry["author_ids"])
            or len(set(entry["author_ids"])) != len(entry["author_ids"])
            or not set(entry["author_ids"]) <= author_ids
        ):
            raise ValueError("candidate authors must come from protected policy")
        candidates.append(Candidate(**(entry | {"author_ids": tuple(entry["author_ids"])})))
    if len({entry.pr for entry in candidates}) != len(candidates):
        raise ValueError("duplicate PR registration")
    return Control(
        policy["revision"],
        policy["repository_id"],
        policy["controller_sha"],
        policy["workspace_id"],
        policy["project_id"],
        policy["platform_identity_id"],
        policy["platform_credential_id"],
        checked_at,
        expires_at,
        authors,
        reviewers,
        tuple(candidates),
    )


def _protected(metadata, owner_uid, *, directory=False):
    correct_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        not correct_type
        or metadata.st_uid != owner_uid
        or metadata.st_mode & 0o022
        or (not directory and (metadata.st_nlink != 1 or metadata.st_size > MAX_BYTES))
    ):
        raise ValueError(
            "control paths must be protected, owned, bounded regular files/directories"
        )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def load_control(
    directory=Path("/etc/artemis-preview"), *, owner_uid=0, anchor=Path("/"), now=None
):
    directory = Path(directory).absolute()
    anchor = Path(anchor).absolute()
    parts = directory.relative_to(anchor).parts
    if ".." in parts:
        raise ValueError("control path traversal")
    descriptor = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _protected(os.fstat(descriptor), owner_uid, directory=True)
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _protected(os.fstat(descriptor), owner_uid, directory=True)
        documents = []
        for name in DOCUMENT_NAMES:
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(child, "rb") as stream:
                _protected(os.fstat(stream.fileno()), owner_uid)
                content = stream.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError("control document exceeds size bound")
                documents.append(
                    json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
                )
        return validate_documents(*documents, now=now)
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.parse_args()
    try:
        result = load_control()
    except (OSError, ValueError) as error:
        parser.exit(1, f"preview control check failed: {error}\n")
    print(f"Protected configuration revision {result.policy_revision} validated; admission NOT-RUN")


if __name__ == "__main__":
    main()
