"""Opt-in native assertion-exit contract (CHE-3645).

Maps the native ``test_summary`` returned by ``Agent.run_task`` to a process
exit code. Fail closed: only complete, well-formed native evidence yields 0
(PASS) or 1 (completed assertion FAIL); anything else is ``EXIT_INCONCLUSIVE``.
2 also collides with CLI usage errors; both are non-success.
"""

import os

EXIT_PASS = 0
EXIT_ASSERTION_FAIL = 1
EXIT_INCONCLUSIVE = 2

_COUNTERS = ("passed", "failed", "inconclusive", "unchecked", "retired")


def _valid_failed_item(item) -> bool:
    return (
        isinstance(item, dict)
        and item.get("kind") in ("assert", "verify")
        and all(isinstance(item.get(k), str) and item[k].strip() for k in ("item_text", "evidence"))
    )


def native_assertion_exit_code(result) -> int:
    summary = result.get("test_summary") if isinstance(result, dict) else None
    if not isinstance(summary, dict) or summary.get("task_status") != "completed":
        return EXIT_INCONCLUSIVE
    if any(type(summary.get(name)) is not int or summary[name] < 0 for name in _COUNTERS):
        return EXIT_INCONCLUSIVE
    if summary["inconclusive"] or summary["unchecked"] or summary["retired"]:
        return EXIT_INCONCLUSIVE
    if summary.get("retired_items") != []:
        return EXIT_INCONCLUSIVE
    failed_items = summary.get("failed_items")
    if (
        not isinstance(failed_items, list)
        or len(failed_items) != summary["failed"]
        or not all(map(_valid_failed_item, failed_items))
    ):
        return EXIT_INCONCLUSIVE
    if summary["failed"]:
        return EXIT_ASSERTION_FAIL
    return EXIT_PASS if summary["passed"] > 0 else EXIT_INCONCLUSIVE


def require_native_scope(profile, device_serial, locked_app_package) -> None:
    """Reject unsupported invocations before any device discovery or Agent creation.

    Only the graph ('pro') profile attaches native evidence; the flash runner
    returns model tool-call arguments as its report, which a model could use to
    supply a PASS summary.
    """
    if str(profile).lower() != "pro":
        raise ValueError("assertion-exit requires --profile pro")
    if not all(isinstance(v, str) and v.strip() for v in (device_serial, locked_app_package)):
        raise ValueError("assertion-exit requires explicit device_serial and locked_app_package")
    if os.getenv("ARTEMIS_CHAT_SESSION_ID"):
        # The chat-session worker returns before the native exit mapping.
        raise ValueError("assertion-exit does not support chat sessions")
