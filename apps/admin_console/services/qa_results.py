from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories.step_repository import StepRepository

PENDING_CAP_SECONDS = 30.0
_KNOWN_CATEGORIES = frozenset(
    {
        "smartqa_infra",
        "smartqa_agent",
        "provider",
        "app_defect",
        "user_prompt",
        "qa_environment",
    }
)
_LABEL_STATES = frozenset({"pending", "final", "legacy", "cancelled", "error"})
_VERIFICATION_DISPLAY_LABELS = {
    "self_reported": "Agent tự báo",
    "verified": "Đã kiểm",
    "none": "Chưa xác minh",
    "pending": "Chưa xác minh",
}
_CATEGORY_DISPLAY_LABELS = {
    "smartqa_infra": "Hạ tầng SmartQA",
    "smartqa_agent": "Agent SmartQA",
    "provider": "Nhà cung cấp",
    "app_defect": "Lỗi ứng dụng",
    "user_prompt": "Prompt người dùng",
    "qa_environment": "Môi trường QA",
    "unknown": "Chưa xác định",
}
_IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_SIGNAL_PRIORITY = {"raw_error_text": 0, "stuck_run": 1, "run_step": 2}
_CATALOG = {
    "pass.self_reported": (
        "Agent tự báo hoàn thành",
        "SmartQA-agent",
        "Xem ảnh chụp cuối run",
        None,
    ),
    "pass.verified": ("Đã kiểm", "SmartQA", "Xem kết quả kiểm tra", None),
    "fail.app_defect": (
        "App đang test (agent nhận định)",
        "App đang test (agent nhận định)",
        "Xem bằng chứng rồi báo bug cho app",
        None,
    ),
    "blocked.smartqa_infra": (
        "SmartQA-hạ tầng",
        "SmartQA",
        "Không phải lỗi của bạn — chạy lại",
        None,
    ),
    "blocked.smartqa_agent": (
        "SmartQA-agent",
        "SmartQA",
        "Không phải lỗi của bạn — chạy lại hoặc đổi profile",
        None,
    ),
    "blocked.provider": ("Model", "Model", "Chọn model khác", "/settings/models"),
    "blocked.user_prompt": (
        "Không đạt được mục tiêu (xem lại prompt)",
        "Prompt",
        "Viết lại prompt",
        None,
    ),
    "blocked.qa_environment": (
        "Môi trường QA",
        "Môi trường QA",
        "Kiểm tra môi trường QA rồi chạy lại",
        None,
    ),
    "blocked.unknown": (
        "Chưa rõ — đã ghi nhận",
        "SmartQA",
        "Chạy lại; admin sẽ xem",
        None,
    ),
    "blocked.evidence": (
        "Chưa đủ bằng chứng",
        "SmartQA-agent",
        "Chạy lại để ghi nhận bằng chứng",
        None,
    ),
    "blocked.check": (
        "Chưa đạt trạng thái cần kiểm",
        "SmartQA-agent",
        "Kiểm tra lại trạng thái rồi chạy lại",
        None,
    ),
    "blocked.cancelled": ("Đã huỷ", "QA", "Chạy lại khi đã sẵn sàng", None),
}
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS qa_results (
    turn_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    label_state TEXT NOT NULL CHECK(label_state IN ('pending','final','legacy','cancelled','error')),
    source TEXT NOT NULL,
    category TEXT,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (turn_id, version)
)
"""


def _entry(key, verdict, category, verification, source, *, evidence_ref=None, reason=None):
    label, responsible, action_text, href = _CATALOG[key]
    result = {
        "catalog_key": key,
        "verdict": verdict,
        "verification": verification,
        "category": category,
        "reason": reason or label,
        "action": {"text": action_text, **({"href": href} if href else {})},
        "responsible": responsible,
        "evidence": {"step": None, "screenshot_url": None},
        "source": source,
    }
    if evidence_ref:
        result["_evidence_ref"] = evidence_ref
    return result


def _unknown(source="none", *, key="blocked.unknown", reason=None):
    return _entry(key, "blocked", "unknown", "none", source, reason=reason)


def _blocked_for_category(category, rule, source, evidence_ref=None):
    if category == "app_defect":
        if evidence_ref:
            return _entry(
                "fail.app_defect",
                "fail",
                category,
                "self_reported",
                source,
                evidence_ref=evidence_ref,
            )
        return _unknown(source)
    key = {
        "smartqa_infra": "blocked.smartqa_infra",
        "smartqa_agent": "blocked.smartqa_agent",
        "provider": "blocked.provider",
        "user_prompt": "blocked.user_prompt",
        "qa_environment": "blocked.qa_environment",
    }.get(category)
    if key is None:
        return _unknown(source)
    if category == "smartqa_infra" and rule == "auth_expired":
        return _entry(
            key,
            "blocked",
            category,
            "none",
            source,
            reason="Phiên đăng nhập SmartQA đã hết hạn",
        ) | {"action": {"text": "Đăng nhập lại rồi chạy lại"}}
    if category == "qa_environment":
        if rule == "device_locked":
            return _entry(
                key,
                "blocked",
                category,
                "none",
                source,
                reason="Điện thoại đang khoá",
            ) | {"action": {"text": "Mở khoá rồi chạy lại"}}
        if rule == "usb_disconnected_hidden":
            return _entry(
                key,
                "blocked",
                category,
                "none",
                source,
                reason="Môi trường QA: tab bị ẩn khi USB ngắt kết nối",
            ) | {"action": {"text": "Giữ tab mở rồi chạy lại"}}
    return _entry(key, "blocked", category, "none", source)


def _valid_flash_evidence(outcome):
    reference = outcome.get("evidence_ref")
    if not isinstance(reference, dict):
        return None
    step_id = reference.get("step_id")
    step_number = reference.get("step_number")
    image_name = reference.get("image_name")
    which = reference.get("which")
    if (
        not isinstance(step_id, str)
        or not step_id
        or type(step_number) is not int
        or step_number < 1
        or not isinstance(image_name, str)
        or not image_name
        or not isinstance(which, str)
        or which not in {"pre", "post"}
    ):
        return None
    return {
        "step_id": step_id,
        "step_number": step_number,
        "image_name": image_name,
        "which": which,
        "requires_screenshot": True,
    }


def _known_ledger_row(ledger_rows):
    candidates = []
    for row in ledger_rows or ():
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("category"), str)
            or row["category"] not in _KNOWN_CATEGORIES
        ):
            continue
        try:
            classified_at = float(row.get("classified_at") or 0)
        except (TypeError, ValueError):
            classified_at = 0
        candidates.append(
            (
                classified_at,
                str(row.get("classifier_version") or ""),
                _SIGNAL_PRIORITY.get(row.get("signal"), 0),
                row,
            )
        )
    return max(candidates, default=(0, "", 0, None), key=lambda item: item[:3])[3]


def _failed_items(outcome):
    if "tests" in outcome:
        tests = outcome.get("tests")
        if not isinstance(tests, dict):
            return []
        failed = tests.get("failed_items")
        return (
            [item for item in failed if isinstance(item, dict)] if isinstance(failed, list) else []
        )
    check_items = outcome.get("checker_items") or []
    failed_statuses = {"failed", "fail", "assertion_failed"}
    if not isinstance(check_items, list):
        return []
    return [
        item
        for item in check_items
        if isinstance(item, dict)
        and str(item.get("status") or item.get("verdict") or item.get("result") or "").lower()
        in failed_statuses
    ]


def _pro_evidence(item):
    step_id = item.get("anchor_step_id") or item.get("step_id")
    if not isinstance(step_id, str) or not step_id:
        return None
    step_number = item.get("step_number")
    return {
        "step_id": step_id,
        "step_number": step_number if type(step_number) is int and step_number > 0 else None,
        "requires_screenshot": False,
    }


def _derive_pro(outcome):
    tests = outcome.get("tests")
    tests = tests if isinstance(tests, dict) else {}
    items = _failed_items(outcome)
    assert_item = next(
        (item for item in items if isinstance(item.get("kind"), str) and item["kind"] == "assert"),
        None,
    )
    if assert_item is not None:
        evidence_ref = _pro_evidence(assert_item)
        return _entry(
            "fail.app_defect",
            "fail",
            "app_defect",
            "verified",
            "checker",
            evidence_ref=evidence_ref,
        )
    if any(
        isinstance(item.get("kind"), str) and item["kind"] in {"verify", "check"} for item in items
    ):
        return _unknown("checker", key="blocked.check")
    if outcome.get("task_status") in {"blocked", "partial"}:
        return _unknown("checker")
    try:
        passed = int(tests.get("passed", 0))
        failed = int(tests.get("failed", 0))
        inconclusive = int(tests.get("inconclusive", 0))
        unchecked = int(tests.get("unchecked", 0))
    except (TypeError, ValueError):
        return _unknown("checker")
    if passed > 0 and failed == inconclusive == unchecked == 0:
        return _entry("pass.verified", "pass", None, "verified", "checker")
    return _unknown("checker")


def derive_result(execution_status, outcome, ledger_rows):
    """Build a QA result from one turn's execution, outcome, and ledger facts."""
    outcome = outcome if isinstance(outcome, dict) else {}
    status = "completed" if execution_status == "success" else execution_status
    ledger = _known_ledger_row(ledger_rows)
    is_pro = "tests" in outcome or "checker_items" in outcome
    if status != "completed":
        if ledger:
            return _blocked_for_category(ledger.get("category"), ledger.get("rule"), "ledger")
        cause = outcome.get("cause")
        category = (
            {
                "goal_unreachable": "user_prompt",
                "app_defect": "app_defect",
            }.get(cause)
            if isinstance(cause, str)
            else None
        )
        if category:
            reference = _valid_flash_evidence(outcome) if category == "app_defect" else None
            return _blocked_for_category(category, None, "outcome", reference)
        return _unknown("none")
    if is_pro:
        return _derive_pro(outcome)

    task_status = outcome.get("task_status", outcome.get("status"))
    cause = outcome.get("cause")
    evidence_ref = _valid_flash_evidence(outcome)
    if ledger:
        return _blocked_for_category(
            ledger.get("category"), ledger.get("rule"), "ledger", evidence_ref
        )
    if task_status == "failed":
        if cause == "app_defect":
            if evidence_ref:
                return _entry(
                    "fail.app_defect",
                    "fail",
                    "app_defect",
                    "self_reported",
                    "outcome",
                    evidence_ref=evidence_ref,
                )
            return _unknown("outcome")
        if cause == "goal_unreachable":
            return _blocked_for_category("user_prompt", None, "outcome")
        return _unknown("outcome")
    if task_status == "completed":
        if evidence_ref:
            return _entry(
                "pass.self_reported",
                "pass",
                None,
                "self_reported",
                "outcome",
                evidence_ref=evidence_ref,
            )
        return _unknown("outcome", key="blocked.evidence")
    return _unknown("outcome")


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_CREATE_TABLE)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS qa_results_latest ON qa_results(turn_id, version DESC)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS qa_result_activation ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), activated_at REAL NOT NULL, "
        "admin_emails TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS qa_result_audit ("
        "turn_id TEXT NOT NULL, old_version INTEGER NOT NULL, new_version INTEGER NOT NULL, "
        "actor TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL, "
        "old_result_json TEXT NOT NULL, new_result_json TEXT NOT NULL, "
        "PRIMARY KEY(turn_id, new_version))"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS qa_result_audit_no_{operation.lower()} "
            f"BEFORE {operation} ON qa_result_audit BEGIN "
            "SELECT RAISE(ABORT, 'qa_result_audit is append-only'); END"
        )


def _record(row):
    return {
        "turn_id": row["turn_id"],
        "version": row["version"],
        "label_state": row["label_state"],
        "source": row["source"],
        "result": json.loads(row["result_json"]),
        "created_at": row["created_at"],
    }


def _latest(connection, turn_id):
    row = connection.execute(
        "SELECT * FROM qa_results WHERE turn_id=? ORDER BY version DESC LIMIT 1", (turn_id,)
    ).fetchone()
    return _record(row) if row else None


def read_result(db_path, turn_id):
    with db_session(db_path) as connection:
        ensure_schema(connection)
        return _latest(connection, turn_id)


def record_result(db_path, turn_id, result, *, label_state, source, now=None):
    if label_state not in _LABEL_STATES:
        raise ValueError("invalid_label_state")
    created_at = time.time() if now is None else float(now)
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    category = result.get("category") if isinstance(result, dict) else None
    with db_session(db_path) as connection:
        ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        latest = _latest(connection, turn_id)
        if latest:
            old_category = latest["result"].get("category")
            if (
                latest["source"] == "admin"
                or latest["label_state"] in {"cancelled", "error"}
                or old_category != "unknown"
            ):
                connection.commit()
                return latest
            if category == "unknown" and not (
                latest["label_state"] == "pending" and label_state != "pending"
            ):
                connection.commit()
                return latest
        version = latest["version"] + 1 if latest else 1
        connection.execute(
            "INSERT INTO qa_results "
            "(turn_id, version, label_state, source, category, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn_id, version, label_state, source, category, payload, created_at),
        )
        record = _latest(connection, turn_id)
        connection.commit()
    return record


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _ledger_rows(db_path, turn_id):
    try:
        with db_session(db_path) as connection:
            rows = connection.execute(
                "SELECT category, rule, evidence, signal, classified_at "
                "FROM failure_ledger WHERE session_id=? ORDER BY classified_at DESC",
                (turn_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def _end_time(db_path, turn_id):
    try:
        with db_session(db_path) as connection:
            try:
                row = connection.execute(
                    "SELECT coalesce(s.end_time, o.created_at) AS ended_at "
                    "FROM sessions s LEFT JOIN lifecycle_outbox o ON o.session_id=s.session_id "
                    "WHERE s.session_id=? ORDER BY o.created_at DESC LIMIT 1",
                    (turn_id,),
                ).fetchone()
            except sqlite3.Error:
                row = None
            if row and row["ended_at"] is not None:
                return row["ended_at"]
            try:
                pending = connection.execute(
                    "SELECT created_at FROM qa_results "
                    "WHERE turn_id=? AND label_state='pending' ORDER BY version DESC LIMIT 1",
                    (turn_id,),
                ).fetchone()
            except sqlite3.Error:
                return None
        return pending["created_at"] if pending else None
    except sqlite3.Error:
        return None


def _pending_cap():
    try:
        return max(0.0, float(os.getenv("ARTEMIS_VERDICT_PENDING_CAP_SECONDS", "30")))
    except ValueError:
        return PENDING_CAP_SECONDS


def _unknown_ledger_exists(rows):
    return any(isinstance(row, dict) for row in rows or ())


def _resolve_evidence(db_path, traces_dir, turn_id, result):
    reference = result.get("_evidence_ref")
    empty = {"turn_id": turn_id, "step": None, "screenshot_url": None}
    if not isinstance(reference, dict):
        return empty, True
    try:
        steps = StepRepository(db_path).get_session_steps(turn_id)
    except (OSError, sqlite3.Error):
        return empty, False
    step_id = reference.get("step_id")
    step_number = reference.get("step_number")
    step = next(
        (
            item
            for item in steps
            if (
                step_id
                and item.get("step_id") == step_id
                and (step_number is None or item.get("step_number") == step_number)
            )
            or (not step_id and step_number and item.get("step_number") == step_number)
        ),
        None,
    )
    if step is None:
        return empty, False
    selected = reference.get("image_name")
    image_names = [step.get("post_image_name"), step.get("pre_image_name")]
    image_name = next(
        (name for name in image_names if name and (selected is None or name == selected)),
        None,
    )
    if selected and image_name is None:
        return empty, False
    screenshot_url = None
    if image_name:
        if not _IMAGE_NAME.fullmatch(image_name):
            return empty, False
        filename = image_name if image_name.lower().endswith(".jpg") else f"{image_name}.jpg"
        images_root = (Path(traces_dir) / "images").resolve()
        try:
            image_path = (images_root / filename).resolve(strict=True)
        except (OSError, RuntimeError):
            return empty, False
        if not image_path.is_file() or not image_path.is_relative_to(images_root):
            return empty, False
        screenshot_url = f"/api/images/{quote(filename, safe='')}"
    if reference.get("requires_screenshot") and screenshot_url is None:
        return empty, False
    return {
        "turn_id": turn_id,
        "step": step.get("step_number"),
        "screenshot_url": screenshot_url,
    }, True


def _with_turn_metadata(record, turn_id, turn_index, turn_count):
    result = dict(record["result"])
    result["evidence"] = dict(result.get("evidence") or {})
    result["evidence"].setdefault("turn_id", turn_id)
    result["evidence"].setdefault("step", None)
    result["evidence"].setdefault("screenshot_url", None)
    result["turn_index"] = turn_index
    result["turn_count"] = turn_count
    result["label_state"] = record["label_state"]
    result["source"] = record["source"]
    result["updated"] = record["version"] > 1
    result["display_labels"] = {
        "verification": _VERIFICATION_DISPLAY_LABELS.get(result.get("verification"), "Chưa xác minh"),
    }
    if result.get("category") is not None:
        result["display_labels"]["category"] = _CATEGORY_DISPLAY_LABELS.get(
            result["category"], "Chưa xác định"
        )
    return result


def result_for_turn(
    db_path, traces_dir, turn_id, execution_status, turn_index, turn_count, *, now=None
):
    current_time = time.time() if now is None else float(now)
    outcome = _read_json(Path(traces_dir) / turn_id / "run_outcome.json")
    ledger_rows = _ledger_rows(db_path, turn_id)
    result = derive_result(execution_status, outcome, ledger_rows)
    evidence, evidence_valid = _resolve_evidence(db_path, traces_dir, turn_id, result)
    if result.get("verdict") == "pass" and not evidence_valid:
        result = _unknown("outcome", key="blocked.evidence")
        evidence = {"turn_id": turn_id, "step": None, "screenshot_url": None}
    elif result.get("category") == "app_defect" and not evidence_valid:
        result = _unknown(result.get("source", "outcome"))
        evidence = {"turn_id": turn_id, "step": None, "screenshot_url": None}
    result.pop("_evidence_ref", None)
    result["evidence"] = evidence

    if execution_status == "cancelled":
        result = _entry("blocked.cancelled", "blocked", "unknown", "none", "execution")
        result["evidence"] = {"turn_id": turn_id, "step": None, "screenshot_url": None}
        state = "cancelled"
    elif execution_status in {"queued", "running", "paused"}:
        result = _unknown("pending")
        result["evidence"] = {"turn_id": turn_id, "step": None, "screenshot_url": None}
        state = "pending"
    elif outcome is None and not _unknown_ledger_exists(ledger_rows):
        ended_at = _end_time(db_path, turn_id)
        expired = ended_at is not None and current_time - float(ended_at) >= _pending_cap()
        result = (
            _unknown("legacy", reason="Run cũ — chưa có verdict")
            if expired
            else _unknown("pending")
        )
        result["evidence"] = {"turn_id": turn_id, "step": None, "screenshot_url": None}
        state = "legacy" if expired else "pending"
    elif result.get("category") == "unknown":
        ended_at = _end_time(db_path, turn_id)
        state = (
            "pending"
            if ended_at is None or current_time - float(ended_at) < _pending_cap()
            else "final"
        )
    else:
        state = "final"

    source = result.get("source", "none")
    record = record_result(
        db_path,
        turn_id,
        result,
        label_state=state,
        source=source,
        now=current_time,
    )
    return _with_turn_metadata(record, turn_id, turn_index, turn_count)


DEFAULT_BACKFILL_SINCE = datetime.fromisoformat("2026-10-09T00:00:00+07:00").timestamp()
MAX_MAINTENANCE_ROWS = 1000


def activation(db_path):
    with db_session(db_path) as connection:
        ensure_schema(connection)
        row = connection.execute(
            "SELECT activated_at, admin_emails FROM qa_result_activation"
        ).fetchone()
    return {"activated_at": row[0], "admin_emails": json.loads(row[1])} if row else None


def activate(db_path, admin_emails, *, now=None):
    activated_at = time.time() if now is None else float(now)
    if not math.isfinite(activated_at) or activated_at < 0:
        raise ValueError("invalid_activation_timestamp")
    snapshot = sorted({email.strip().casefold() for email in admin_emails if email.strip()})
    with db_session(db_path) as connection:
        ensure_schema(connection)
        connection.execute(
            "INSERT OR IGNORE INTO qa_result_activation VALUES (1, ?, ?)",
            (activated_at, json.dumps(snapshot)),
        )
        connection.commit()
    return activation(db_path)


def _maintenance_bounds(since, until, limit):
    since = float(since)
    until = time.time() if until is None else float(until)
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_MAINTENANCE_ROWS
        or not math.isfinite(since)
        or not math.isfinite(until)
        or since < 0
        or since >= until
    ):
        raise ValueError("invalid_maintenance_bounds")
    return since, until


def _turns_query(connection):
    has_chat = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turns'"
    ).fetchone()
    chat_ids = " UNION SELECT turn_id FROM chat_turns" if has_chat else ""
    chat_join = (
        "LEFT JOIN chat_turns t ON t.turn_id=ids.turn_id "
        "LEFT JOIN run_meta root_meta ON root_meta.session_id=t.session_id "
        if has_chat
        else ""
    )
    started = "coalesce(s.start_time, t.created_at)" if has_chat else "s.start_time"
    status = (
        "CASE WHEN s.status IN ('completed','failed','cancelled','interrupted') "
        "THEN s.status ELSE coalesce(t.status, s.status) END"
        if has_chat
        else "s.status"
    )
    owner = "coalesce(m.requested_by, root_meta.requested_by)" if has_chat else "m.requested_by"
    index = "coalesce(t.number, 1)" if has_chat else "1"
    count = (
        "CASE WHEN t.turn_id IS NULL THEN 1 ELSE "
        "(SELECT count(*) FROM chat_turns siblings WHERE siblings.session_id=t.session_id) END"
        if has_chat
        else "1"
    )
    return (
        f"WITH ids AS (SELECT session_id AS turn_id FROM sessions{chat_ids}), turns AS ("
        f"SELECT ids.turn_id, {started} AS started_at, {status} AS status, "
        f"{owner} AS requested_by, {index} AS turn_index, {count} AS turn_count, "
        "coalesce(s.end_time, (SELECT max(created_at) FROM lifecycle_outbox o "
        "WHERE o.session_id=ids.turn_id)) AS ended_at, "
        "coalesce(s.interrupt_reason, (SELECT interrupt_reason FROM lifecycle_outbox o "
        "WHERE o.session_id=ids.turn_id ORDER BY created_at DESC LIMIT 1)) AS interrupt_reason "
        "FROM ids LEFT JOIN sessions s ON s.session_id=ids.turn_id "
        "LEFT JOIN run_meta m ON m.session_id=ids.turn_id "
        f"{chat_join}) "
    )


def backfill(db_path, traces_dir, *, since=DEFAULT_BACKFILL_SINCE, until=None, limit=100):
    from apps.admin_console.services import failure_ledger
    from artemis.runtime.lifecycle import ensure_lifecycle_schema

    since, until = _maintenance_bounds(since, until, limit)
    with db_session(db_path) as connection:
        ensure_schema(connection)
        ensure_lifecycle_schema(connection)
        connection.commit()
        rows = connection.execute(
            _turns_query(connection) + "SELECT * FROM turns WHERE ended_at>=? AND ended_at<? "
            "AND status IN ('completed','failed','interrupted','cancelled') "
            "AND NOT EXISTS (SELECT 1 FROM qa_results q WHERE q.turn_id=turns.turn_id) "
            "ORDER BY ended_at, turn_id LIMIT ?",
            (since, until, limit),
        ).fetchall()
    processed = []
    for row in rows:
        failure_ledger.collect(row["turn_id"], db_path=db_path, traces_dir=traces_dir)
        result_for_turn(
            db_path, traces_dir, row["turn_id"], row["status"], row["turn_index"], row["turn_count"]
        )
        processed.append(row["turn_id"])
    return {"processed": len(processed), "turn_ids": processed, "since": since, "until": until}


def replay_abandoned(db_path, *, since=DEFAULT_BACKFILL_SINCE, until=None, limit=100):
    from artemis.runtime.lifecycle import ensure_lifecycle_schema

    since, until = _maintenance_bounds(since, until, limit)
    with db_session(db_path) as connection:
        ensure_lifecycle_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT dedupe_id, abandoned FROM lifecycle_outbox "
            "WHERE created_at>=? AND created_at<? "
            "AND instr(',' || coalesce(abandoned,'') || ',', ',classify,')>0 "
            "ORDER BY created_at, dedupe_id LIMIT ?",
            (since, until, limit),
        ).fetchall()
        for row in rows:
            abandoned = ",".join(
                token for token in row["abandoned"].split(",") if token != "classify"
            )
            connection.execute(
                "UPDATE lifecycle_outbox SET classified_at=NULL, classify_attempts=0, "
                "delivered_at=NULL, abandoned=?, classify_replay_at=? WHERE dedupe_id=?",
                (abandoned or None, time.time(), row["dedupe_id"]),
            )
        connection.commit()
    return len(rows)


def relabel_result(db_path, turn_id, *, category, actor, reason, now=None):
    if category not in _KNOWN_CATEGORIES | {"unknown"} or not actor or not reason.strip():
        raise ValueError("invalid_relabel")
    created_at = time.time() if now is None else float(now)
    with db_session(db_path) as connection:
        ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        previous = _latest(connection, turn_id)
        if previous is None:
            raise KeyError(turn_id)
        if previous["label_state"] in {"pending", "cancelled"}:
            raise ValueError("relabel_requires_terminal_result")
        evidence = previous["result"].get("evidence") or {}
        if category == "app_defect":
            if not evidence.get("step") or not evidence.get("screenshot_url"):
                raise ValueError("app_defect_requires_evidence")
            result = _entry("fail.app_defect", "fail", category, "self_reported", "admin")
        else:
            result = _blocked_for_category(category, None, "admin")
        result["evidence"] = evidence
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        version = previous["version"] + 1
        connection.execute(
            "INSERT INTO qa_results VALUES (?, ?, 'final', 'admin', ?, ?, ?)",
            (turn_id, version, category, payload, created_at),
        )
        connection.execute(
            "INSERT INTO qa_result_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                previous["version"],
                version,
                actor,
                reason.strip(),
                created_at,
                json.dumps(previous["result"], ensure_ascii=False),
                payload,
            ),
        )
        record = _latest(connection, turn_id)
        connection.commit()
    return record


def gate_a(db_path, *, now=None):
    from artemis.runtime.lifecycle import ensure_lifecycle_schema

    snapshot = activation(db_path)
    if snapshot is None:
        raise ValueError("qa_results_not_activated")
    current_time = time.time() if now is None else float(now)
    started = snapshot["activated_at"]
    end = started + 7 * 86400
    admins = frozenset(snapshot["admin_emails"])
    with db_session(db_path) as connection:
        ensure_lifecycle_schema(connection)
        connection.commit()
        rows = connection.execute(
            _turns_query(connection)
            + "SELECT turns.*, q.label_state, q.result_json FROM turns LEFT JOIN qa_results q "
            "ON q.turn_id=turns.turn_id AND q.version="
            "(SELECT max(version) FROM qa_results latest WHERE latest.turn_id=turns.turn_id) "
            "WHERE started_at>=? AND started_at<? AND started_at<=?",
            (started, end, current_time),
        ).fetchall()
    qa_turns = [row for row in rows if (row["requested_by"] or "").casefold() not in admins]
    denominator = [
        row for row in qa_turns if row["status"] not in {"cancelled", "running", "queued", "paused"}
    ]
    numerator = 0
    for row in denominator:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        if (
            row["label_state"] == "final"
            and result.get("verdict") in {"pass", "fail", "blocked"}
            and (
                result.get("category") in _KNOWN_CATEGORIES
                or result.get("verdict") == "pass"
                and result.get("category") is None
            )
            and result.get("catalog_key") not in {"blocked.evidence", "blocked.check"}
        ):
            numerator += 1
    cancelled = sum(row["status"] == "cancelled" for row in qa_turns)
    deploy_killed = sum(row["interrupt_reason"] == "deploy_cut" for row in rows)
    total = len(denominator)
    return {
        "activated_at": started,
        "window_end": end,
        "window_complete": current_time >= end,
        "N": total,
        "denominator": total,
        "numerator": numerator,
        "coverage": numerator / total if total else 0.0,
        "cancelled": cancelled,
        "qa_turns": len(qa_turns),
        "cancel_rate": cancelled / len(qa_turns) if qa_turns else 0.0,
        "legacy": sum(row["label_state"] == "legacy" for row in qa_turns),
        "pending": sum(
            row["label_state"] == "pending"
            or (row["result_json"] is None and row["status"] != "cancelled")
            or row["status"] in {"running", "queued", "paused"}
            for row in qa_turns
        ),
        "deploy_killed": deploy_killed,
        "passed": total > 0 and numerator * 5 >= total * 4 and deploy_killed == 0,
    }
