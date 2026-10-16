import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from fastapi import HTTPException

from artemis.config import PAUSE_FILE

admission_lock = threading.RLock()


def drain_file() -> Path:
    return Path(
        os.environ.get("ARTEMIS_DRAIN_FILE", str(PAUSE_FILE.with_name("deploy-drain.json")))
    )


def read_drain() -> dict | None:
    try:
        payload = json.loads(drain_file().read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload.get("owner"):
            raise ValueError("invalid drain owner")
        if not math.isfinite(float(payload["deadline"])):
            raise ValueError("invalid drain deadline")
        if not isinstance(payload.get("queued_items", []), list):
            raise ValueError("invalid held queue")
        return payload
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return {"owner": "unreadable-drain", "deadline": time.time() + 5}


def write_drain(payload: dict) -> None:
    path = drain_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ServerDraining(RuntimeError):
    code = "server_draining"

    def __init__(self, record: dict | None = None) -> None:
        self.deadline = record["deadline"] if record else None
        self.retry_after_seconds = max(1, math.ceil(self.deadline - time.time())) if record else 5
        super().__init__("SmartQA đang cập nhật. Your prompt is kept; retry after the update.")

    def http_error(self) -> HTTPException:
        return HTTPException(
            status_code=503,
            detail={"code": self.code, "message": str(self), "deadline": self.deadline},
            headers={"Retry-After": str(self.retry_after_seconds)},
        )
