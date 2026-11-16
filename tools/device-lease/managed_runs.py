"""Multica v1 host registry, cooperative same-UID trust only.

The owner selects the directory; RPC clients supply only an opaque binding ID.
A private registry is not authentication against a malicious host owner.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat

from broker import process_identity

FIELDS = {
    "version",
    "binding_id",
    "agent_id",
    "principal",
    "task_id",
    "issue_id",
    "pid",
    "process_start",
    "boot_id",
    "pid_namespace",
}


class ManagedRuns:
    def __init__(self, directory):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("managed registry must be absolute")
        with self._directory() as fd:
            info = os.fstat(fd)
            self.identity = (info.st_dev, info.st_ino)

    @contextmanager
    def _directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("managed registry must be private and owner-only")
            if hasattr(self, "identity") and self.identity != (info.st_dev, info.st_ino):
                raise ValueError("managed registry replaced")
            yield fd
        finally:
            os.close(fd)

    def resolve(self, binding_id, *, principal, agent_id, issue_id, run_id):
        if not isinstance(binding_id, str) or not re.fullmatch(r"[0-9a-f]{64}", binding_id):
            raise ValueError("invalid managed reference")
        with self._directory() as directory:
            fd = os.open(
                binding_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 8192
                ):
                    raise ValueError("invalid managed record")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(8193)
                if len(data) > 8192:
                    raise ValueError("oversized managed record")
                record = json.loads(data)
            finally:
                os.close(fd)
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ValueError("invalid managed schema")
        if (
            type(record["version"]) is not int
            or record["version"] != 1
            or type(record["pid"]) is not int
            or record["pid"] <= 1
        ):
            raise ValueError("invalid managed identity")
        if any(
            not isinstance(record[k], str) or not record[k] for k in FIELDS - {"version", "pid"}
        ):
            raise ValueError("incomplete managed identity")
        expected = dict(
            binding_id=binding_id,
            principal=principal,
            agent_id=agent_id,
            issue_id=issue_id,
            task_id=run_id,
        )
        if (
            any(record[k] != value for k, value in expected.items())
            or record["principal"] != record["agent_id"]
        ):
            raise ValueError("managed association mismatch")
        if record["pid_namespace"] != os.readlink("/proc/self/ns/pid"):
            raise ValueError(
                "managed registry requires the launcher's host PID namespace; use gated R1 for isolation"
            )
        observed = process_identity(record["pid"])
        if any(record[k] != observed[k] for k in ("pid", "process_start", "boot_id")):
            raise ValueError("managed holder no longer live")
        return {"mode": "managed", **record}

    def alive(self, binding):
        try:
            observed = self.resolve(
                binding["binding_id"],
                principal=binding["principal"],
                agent_id=binding["agent_id"],
                issue_id=binding["issue_id"],
                run_id=binding["run_id"],
            )
            return observed == {k: v for k, v in binding.items() if k != "run_id"}
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return False
