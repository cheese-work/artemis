"""Plan isolated preview jobs and prepare exclusive trees; never register or start runners."""

import fcntl
import json
import os
import re
import secrets
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts import preview_control as control
from scripts import preview_receipts as receipts


RUN_ROOT = Path("/var/lib/artemis-preview-runs")
RUNNER_GROUP = "artemis-preview-controller"
WORKFLOW = "cheese-work/artemis/.github/workflows/preview-controller.yml"
LABELS = frozenset({"self-hosted", "cheese-x99", "cheese-x99-artemis-preview"})


@dataclass(frozen=True)
class RunnerPin:
    repository_id: int
    controller_sha: str
    group_id: int


@dataclass(frozen=True)
class Job:
    repository_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    job_id: int
    runner_id: int
    runner_group_id: int
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Reservation:
    admission_id: str
    repository_id: int
    pr: int
    head_sha: str
    base_sha: str
    controller_sha: str
    policy_revision: int
    run_id: int
    run_attempt: int
    runner_group_id: int
    controller_uid: int
    builder_uid: int
    nonce: str


@dataclass(frozen=True)
class Allocation(Reservation):
    job_id: int
    runner_id: int


def verify_group(policy, group, repositories) -> RunnerPin:
    if type(group) is not dict:
        raise ValueError("native runner group is missing")
    group_id = control._number(group.get("id"))
    if (
        type(repositories) is not list
        or len(repositories) != 1
        or type(repositories[0]) is not dict
    ):
        raise ValueError("runner group must select exactly one native repository")
    repository = repositories[0]
    if (
        control._number(repository.get("id")) != policy.repository_id
        or repository.get("full_name") != "cheese-work/artemis"
        or type(repository.get("private")) is not bool
    ):
        raise ValueError("native selected repository does not match protected policy")
    if (
        group.get("name") != RUNNER_GROUP
        or group.get("visibility") != "selected"
        or group.get("allows_public_repositories") is not (not repository["private"])
        or group.get("restricted_to_workflows") is not True
        or group.get("inherited") is not False
        or group.get("selected_workflows") != [f"{WORKFLOW}@{policy.controller_sha}"]
    ):
        raise ValueError("runner group must enforce the exclusive repository and full-SHA workflow")
    return RunnerPin(policy.repository_id, policy.controller_sha, group_id)


def _uid(value):
    if type(value) is not int or not 10000 <= value < 2**31:
        raise ValueError("runner and builder need reserved, non-root, non-agent UIDs")
    return value


def _allocation(allocation, *, bound=False):
    if type(allocation) not in (Reservation, Allocation) or (
        bound and type(allocation) is not Allocation
    ):
        raise ValueError("a bound job allocation is required")
    control._uuid(allocation.admission_id)
    for field in (
        "repository_id",
        "pr",
        "policy_revision",
        "run_id",
        "run_attempt",
        "runner_group_id",
    ):
        control._number(getattr(allocation, field))
    if type(allocation) is Allocation:
        control._number(allocation.job_id)
        control._number(allocation.runner_id)
    for field in ("head_sha", "base_sha", "controller_sha"):
        control._sha(getattr(allocation, field))
    _uid(allocation.controller_uid)
    _uid(allocation.builder_uid)
    if allocation.controller_uid == allocation.builder_uid:
        raise ValueError("controller and PR builder must use different UIDs")
    if type(allocation.nonce) is not str or not re.fullmatch(r"[0-9a-f]{32}", allocation.nonce):
        raise ValueError("invalid run nonce")


def reserve(
    policy, record, pin, *, run_id, run_attempt, controller_uid, builder_uid, now=None
) -> Reservation:
    observed = int(time.time()) if now is None else control._number(now)
    control._scope_window(policy.scope_checked_at, policy.scope_expires_at, observed)
    receipts.require_admitted(record, now=observed)
    candidate = policy.candidate(record.receipt.pr)
    if (
        record.control_sha256 != receipts._fingerprint(policy)
        or record.receipt.repository_id != policy.repository_id
        or record.receipt.policy_revision != policy.policy_revision
        or any(
            getattr(record.receipt, field) != getattr(candidate, field)
            for field in ("pr", "head_sha", "base_sha", "author_ids", "issue_id", "thread_id")
        )
        or pin.repository_id != policy.repository_id
        or pin.controller_sha != policy.controller_sha
    ):
        raise ValueError("admission or runner pin is outside the protected candidate/policy")
    reservation = Reservation(
        record.id,
        policy.repository_id,
        candidate.pr,
        candidate.head_sha,
        candidate.base_sha,
        policy.controller_sha,
        policy.policy_revision,
        run_id,
        run_attempt,
        pin.group_id,
        controller_uid,
        builder_uid,
        secrets.token_hex(16),
    )
    _allocation(reservation)
    return reservation


def allocate(policy, record, pin, job, *, reservation, peer_uid, now=None) -> Allocation:
    _allocation(reservation)
    if type(reservation) is not Reservation:
        raise ValueError("job is already bound")
    expected = reserve(
        policy,
        record,
        pin,
        run_id=reservation.run_id,
        run_attempt=reservation.run_attempt,
        controller_uid=reservation.controller_uid,
        builder_uid=reservation.builder_uid,
        now=now,
    )
    if (
        asdict(reservation) | {"nonce": expected.nonce} != asdict(expected)
        or type(peer_uid) is not int
        or peer_uid != reservation.controller_uid
    ):
        raise ValueError("reserved run and runner peer UID must match current admission/policy")
    for field in (
        "repository_id",
        "run_id",
        "run_attempt",
        "job_id",
        "runner_id",
        "runner_group_id",
    ):
        control._number(getattr(job, field))
    if (
        job.repository_id != policy.repository_id
        or job.run_id != reservation.run_id
        or job.run_attempt != reservation.run_attempt
        or job.workflow_ref != f"{WORKFLOW}@{policy.controller_sha}"
        or job.workflow_sha != policy.controller_sha
        or job.runner_group_id != pin.group_id
        or type(job.labels) is not tuple
        or len(job.labels) > 32
        or any(type(label) is not str or len(label) > 128 for label in job.labels)
        or not LABELS <= set(job.labels)
    ):
        raise ValueError("job is not the pinned controller on the dedicated runner group")
    allocation = Allocation(**asdict(reservation), job_id=job.job_id, runner_id=job.runner_id)
    _allocation(allocation, bound=True)
    return allocation


def run_name(allocation):
    _allocation(allocation)
    return f"{allocation.repository_id}-{allocation.run_id}-{allocation.run_attempt}-{allocation.nonce}"


def run_identity(allocation):
    _allocation(allocation, bound=True)
    return {
        field: getattr(allocation, field)
        for field in (
            "admission_id",
            "repository_id",
            "run_id",
            "run_attempt",
            "job_id",
            "runner_id",
            "controller_sha",
            "nonce",
        )
    }


def check_identity(allocation, request, *, peer_uid):
    expected = run_identity(allocation)
    control._fields(request, set(expected))
    if (
        type(peer_uid) is not int
        or peer_uid != allocation.controller_uid
        or any(
            type(request[field]) is not type(value) or request[field] != value
            for field, value in expected.items()
        )
    ):
        raise ValueError("peer UID and complete request identity must match the allocated run")


def _open_root(root, owner_uid, anchor):
    parts = Path(root).absolute().relative_to(Path(anchor).absolute()).parts
    if ".." in parts:
        raise ValueError("run root traversal")
    descriptor = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        control._protected(os.fstat(descriptor), owner_uid, directory=True)
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            control._protected(os.fstat(descriptor), owner_uid, directory=True)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise ValueError("run root must be private to its protected owner")
        return descriptor
    except (OSError, ValueError):
        os.close(descriptor)
        raise


def _directory(parent, name, uid, *, mode=0o700):
    os.mkdir(name, mode=0o700, dir_fd=parent)
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        os.fchmod(child, mode)
        os.fchown(child, uid, uid)
    except OSError:
        os.close(child)
        raise
    return child


def _write_record(directory, name, allocation, owner_uid):
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        os.fchown(stream.fileno(), owner_uid, owner_uid)
        json.dump(asdict(allocation), stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


def _read_record(directory, name, record_type, owner_uid):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        control._protected(metadata, owner_uid)
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("run record must be private to its protected owner")
        content = stream.read(control.MAX_BYTES + 1)
        if len(content) > control.MAX_BYTES:
            raise ValueError("run record exceeds size bound")
        document = receipts._json(content)
    control._fields(document, set(record_type.__dataclass_fields__))
    result = record_type(**document)
    _allocation(result)
    return result


def _run_tree(descriptor, allocation, owner_uid):
    child = os.open(
        run_name(allocation),
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=descriptor,
    )
    try:
        control._protected(os.fstat(child), owner_uid, directory=True)
        if stat.S_IMODE(os.fstat(child).st_mode) != 0o700:
            raise ValueError("run tree must be private to its protected owner")
        stored = _read_record(child, "reservation.json", Reservation, owner_uid)
        if asdict(stored) != {
            field: getattr(allocation, field) for field in Reservation.__dataclass_fields__
        }:
            raise ValueError("protected run reservation does not match allocation")
        return child
    except (OSError, ValueError):
        os.close(child)
        raise


def prepare_workspace(allocation, *, root=RUN_ROOT, owner_uid=0, anchor=Path("/")) -> Path:
    if type(allocation) is not Reservation:
        raise ValueError("workspace must be reserved before a runner receives a job")
    name = run_name(allocation)
    descriptor = _open_root(root, owner_uid, anchor)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        claims = (
            f"uid-{allocation.controller_uid}",
            f"uid-{allocation.builder_uid}",
            f"job-{allocation.repository_id}-{allocation.run_id}-{allocation.run_attempt}",
            name,
        )
        for claim in claims:
            try:
                os.stat(claim, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("run or UID already has a protected claim")
        for claim in claims[:-1]:
            os.mkdir(claim, mode=0o700, dir_fd=descriptor)
        os.fsync(descriptor)
        tree = _directory(descriptor, name, owner_uid)
        try:
            for branch, uid, children in (
                ("controller", allocation.controller_uid, ("home", "work", "runtime")),
                ("builder", allocation.builder_uid, ("home", "work", "runtime", "state")),
                ("handoff", allocation.builder_uid, ()),
            ):
                branch_fd = _directory(tree, branch, owner_uid)
                try:
                    for child in children:
                        child_fd = _directory(branch_fd, child, uid)
                        os.close(child_fd)
                    if not children:
                        os.fchown(branch_fd, uid, uid)
                    os.fsync(branch_fd)
                finally:
                    os.close(branch_fd)
            _write_record(tree, "reservation.json", allocation, owner_uid)
            os.fsync(tree)
        finally:
            os.close(tree)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return Path(root) / name


def bind_workspace(allocation, *, root=RUN_ROOT, owner_uid=0, anchor=Path("/")):
    _allocation(allocation, bound=True)
    descriptor = _open_root(root, owner_uid, anchor)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        tree = _run_tree(descriptor, allocation, owner_uid)
        try:
            for claim in (f"github-job-{allocation.job_id}", f"runner-{allocation.runner_id}"):
                os.mkdir(claim, mode=0o700, dir_fd=descriptor)
            os.fsync(descriptor)
            _write_record(tree, "allocation.json", allocation, owner_uid)
        finally:
            os.close(tree)
    finally:
        os.close(descriptor)


def load_allocation(reservation, *, root=RUN_ROOT, owner_uid=0, anchor=Path("/")):
    _allocation(reservation)
    descriptor = _open_root(root, owner_uid, anchor)
    try:
        tree = _run_tree(descriptor, reservation, owner_uid)
        try:
            result = _read_record(tree, "allocation.json", Allocation, owner_uid)
            if {
                field: getattr(result, field) for field in Reservation.__dataclass_fields__
            } != asdict(reservation):
                raise ValueError("protected bound allocation does not match reservation")
            return result
        finally:
            os.close(tree)
    finally:
        os.close(descriptor)


def runner_command(allocation) -> list[str]:
    if type(allocation) is not Reservation:
        raise ValueError("runner launch requires an unbound reservation")
    path = RUN_ROOT / run_name(allocation)
    mounts = " ".join(
        f"{path / source}:{destination}"
        for source, destination in (
            ("controller/work", "/work"),
            ("controller/home", "/home/controller"),
            ("controller/runtime", "/run/controller"),
        )
    )
    return [
        "systemd-run",
        f"--unit=artemis-preview-controller-{run_name(allocation)}",
        "--service-type=exec",
        "--collect",
        f"--uid={allocation.controller_uid}",
        f"--gid={allocation.controller_uid}",
        "--property=SupplementaryGroups=artemis-preview-runner",
        "--property=RootDirectory=/usr/local/lib/artemis-preview-controller",
        "--property=MountAPIVFS=yes",
        f"--property=BindPaths={mounts}",
        "--property=BindReadOnlyPaths=/run/artemis-preview/controller.sock",
        "--property=ProtectSystem=strict",
        "--property=ReadWritePaths=/work /home/controller /run/controller",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectProc=invisible",
        "--property=PrivateDevices=yes",
        "--property=PrivateTmp=yes",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=",
        "--property=RestrictSUIDSGID=yes",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "--property=UMask=0077",
        "--property=KillMode=control-group",
        "--property=TasksMax=512",
        "--property=MemoryMax=1G",
        "--property=TimeoutStopSec=30",
        "--working-directory=/work",
        "--setenv=HOME=/home/controller",
        "--setenv=XDG_RUNTIME_DIR=/run/controller",
        f"--setenv=ARTEMIS_PREVIEW_NONCE={allocation.nonce}",
        "--",
        "/usr/local/libexec/artemis-preview/artemis-preview-controller",
        "runner",
        "--repository-id",
        str(allocation.repository_id),
        "--run-id",
        str(allocation.run_id),
        "--run-attempt",
        str(allocation.run_attempt),
        "--workflow-sha",
        allocation.controller_sha,
    ]


def builder_command(allocation) -> list[str]:
    _allocation(allocation, bound=True)
    path = RUN_ROOT / run_name(allocation)
    mounts = " ".join(
        f"{path / source}:{destination}"
        for source, destination in (
            ("builder/work", "/work"),
            ("builder/home", "/home/builder"),
            ("builder/runtime", "/run/builder"),
            ("builder/state", "/state"),
            ("handoff", "/handoff"),
        )
    )
    return [
        "systemd-run",
        f"--unit=artemis-preview-build-{run_name(allocation)}",
        "--service-type=exec",
        "--collect",
        f"--uid={allocation.builder_uid}",
        f"--gid={allocation.builder_uid}",
        "--property=RootDirectory=/usr/local/lib/artemis-preview-builder",
        "--property=MountAPIVFS=yes",
        f"--property=BindPaths={mounts}",
        "--property=ProtectSystem=strict",
        "--property=ReadWritePaths=/work /home/builder /run/builder /state /handoff",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectProc=invisible",
        "--property=PrivateDevices=yes",
        "--property=PrivateTmp=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=NoNewPrivileges=no",
        "--property=CapabilityBoundingSet=CAP_SETUID CAP_SETGID",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "--property=IPAddressDeny=any",
        "--property=UMask=0077",
        "--property=KillMode=control-group",
        "--property=TasksMax=512",
        "--property=MemoryMax=4G",
        "--property=TimeoutStopSec=30",
        "--working-directory=/work",
        "--setenv=HOME=/home/builder",
        "--setenv=XDG_RUNTIME_DIR=/run/builder",
        "--setenv=BUILDKIT_HOST=unix:///run/builder/buildkitd.sock",
        "--",
        "/usr/bin/rootlesskit",
        "--net=none",
        "--state-dir=/run/builder/rootlesskit",
        "--copy-up=/etc",
        "--copy-up=/run",
        "/usr/bin/buildkitd",
        "--root",
        "/state",
        "--addr",
        "unix:///run/builder/buildkitd.sock",
        "--oci-worker-net=host",
        "--oci-worker-snapshotter=native",
        "--containerd-worker=false",
    ]
