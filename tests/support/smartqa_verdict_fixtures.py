from dataclasses import dataclass
import json
import sqlite3
import time
import uuid

from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime.lifecycle import ensure_lifecycle_schema
from apps.admin_console.routers import chats, media
from apps.admin_console.services import failure_ledger, media_service, run_media
from apps.admin_console.database.repositories import session_repository, step_repository


@dataclass(frozen=True)
class SmartQAVerdictCase:
    chat_id: str
    turn_ids: tuple[str, ...]
    evidence_step_id: str | None
    evidence_image_name: str | None


def build_smartqa_verdict_case(
    library,
    monkeypatch,
    *,
    status="completed",
    follow_up=False,
    ledger_rows=(),
    screenshot_name=None,
    requested_by="qa@example.com",
):
    monkeypatch.setattr(chats, "DB_PATH", library.db)
    monkeypatch.setattr(chats, "TRACES_PATH", library.traces)
    monkeypatch.setattr(media, "IMAGES_DIR", library.images)
    monkeypatch.setattr(media, "TRACES_PATH", library.traces)
    monkeypatch.setattr(media_service, "TRACES_PATH", library.traces)
    monkeypatch.setattr(run_media, "library_paths", lambda: (library.db, library.traces))
    monkeypatch.setattr(session_repository.session_repo, "db_path", library.db)
    monkeypatch.setattr(step_repository.step_repo, "db_path", library.db)

    chat_id = library.seed(status="completed" if follow_up else status)
    store = ChatStore(library.db)
    turn_ids = [chat_id]
    if follow_up:
        message = store.send(chat_id, "Follow-up run", "smartqa-follow-up")
        turn_id = message["turn_id"]
        library.seed(sid=turn_id, status=status)
        store.transition(turn_id, status)
        turn_ids.append(turn_id)

    evidence_step_id = None
    if screenshot_name:
        library.image(screenshot_name)
        evidence_step_id = library.step(turn_ids[-1], 1, post=screenshot_name)

    with sqlite3.connect(library.db) as connection:
        ensure_lifecycle_schema(connection)
        failure_ledger._ensure(connection)
        for turn_id in turn_ids:
            connection.execute(
                "UPDATE run_meta SET requested_by=? WHERE session_id=?",
                (requested_by, turn_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO lifecycle_outbox "
                "(dedupe_id, session_id, status, created_at) VALUES (?, ?, ?, ?)",
                (f"smartqa-fixture:{turn_id}", turn_id, status, time.time()),
            )
        for row in ledger_rows:
            connection.execute(
                "INSERT INTO failure_ledger "
                "(session_id, step_number, signal, scope, category, rule, cause, evidence, "
                "device, source, occurred_at, classified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("session_id", turn_ids[-1]),
                    row.get("step_number", 0),
                    row.get("signal", "run_step"),
                    row.get("scope", "run"),
                    row.get("category", "unknown"),
                    row.get("rule", "no_match"),
                    row.get("cause", "fixture"),
                    row.get("evidence", "fixture evidence"),
                    row.get("device"),
                    row.get("source", "fixture"),
                    row.get("occurred_at", time.time()),
                    row.get("classified_at", time.time()),
                ),
            )

    for turn_id in turn_ids:
        library.write(
            turn_id,
            "status.json",
            json.dumps({"status": status, "terminal_error": None}),
        )

    return SmartQAVerdictCase(
        chat_id=chat_id,
        turn_ids=tuple(turn_ids),
        evidence_step_id=evidence_step_id,
        evidence_image_name=screenshot_name,
    )
