from pathlib import Path
import os
import uuid

from apps.admin_console.core.access_control import AccessConfig


def preview_owners(config: AccessConfig) -> tuple[tuple[str, str], str]:
    owners = tuple(
        email.strip().casefold()
        for email in os.environ.get("ARTEMIS_PREVIEW_QA_EMAILS", "").split(",")
    )
    if len(owners) != 2 or len(set(owners)) != 2 or len(config.admin_emails) != 1:
        raise ValueError("A preview requires two QA emails and exactly one explicit admin email.")
    admin = next(iter(config.admin_emails))
    if not os.environ.get("ARTEMIS_ADMIN_EMAILS") or admin in owners:
        raise ValueError("A preview requires two QA emails distinct from its explicit admin email.")
    for email in (*owners, admin):
        if email.count("@") != 1 or any(char.isspace() for char in email):
            raise ValueError("Preview fixture owners must be email identities.")
        local, domain = email.split("@")
        if not local or not domain:
            raise ValueError("Preview fixture owners must be email identities.")
    return (owners[0], owners[1]), admin


def seed_preview_fixtures(root: Path, qa_emails: tuple[str, str], admin_email: str) -> list[dict]:
    from apps.admin_console.database.repositories.run_catalog_repository import (
        RunCatalogRepository,
    )
    from artemis.data_engine.models import SessionMetadata
    from artemis.data_engine.storage import StorageManager

    traces = root / "traces"
    db = traces / "data_engine.db"
    if db.exists() or db.is_symlink() or traces.is_symlink():
        raise ValueError("Preview fixtures never adopt an existing database or trace symlink.")
    storage = StorageManager(db, traces)
    catalog = RunCatalogRepository(db, traces)
    queue = []
    for owner_index, owner in enumerate((*qa_emails, admin_email)):
        for status_index, status in enumerate(("queued", "running", "completed")):
            session_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"artemis-preview/{owner_index}/{status}")
            )
            timestamp = 1_700_000_000.0 + owner_index * 100 + status_index
            goal = f"Synthetic preview: identity {owner_index + 1}, {status} run"
            storage.create_session(
                SessionMetadata(
                    session_id=session_id,
                    initial_goal=goal,
                    start_time=timestamp,
                    end_time=timestamp + 30 if status == "completed" else None,
                    status=status,
                    device_info={"profile": "synthetic-preview"},
                )
            )
            if not catalog.set_meta(session_id, requested_by=owner):
                raise ValueError("Preview fixture ownership could not be recorded.")
            if status != "completed":
                queue.append(
                    {
                        "session_id": session_id,
                        "goal": goal,
                        "status": "pending" if status == "queued" else status,
                        "requested_by": owner,
                        "device_id": None,
                    }
                )
    return queue


def initialize_preview_fixtures(root: Path | None, config: AccessConfig) -> None:
    from apps.admin_console.core.config import DB_PATH, TRACES_PATH
    from apps.admin_console.core.state import state
    from apps.admin_console.routers.preview_synthetic import control

    if (
        root is None
        or DB_PATH != root / "traces" / "data_engine.db"
        or TRACES_PATH != root / "traces"
    ):
        raise ValueError("Preview storage was imported before the private paths were selected.")
    qa_emails, admin_email = preview_owners(config)
    state.queue_items[:] = seed_preview_fixtures(root, qa_emails, admin_email)
    control.paused = False
