"""A runner report of ``interrupted`` ends the session as interrupted, not failed (CHE-1089)."""

from unittest.mock import MagicMock

import pytest

from artemis.runtime.lifecycle import InterruptReason
from artemis.sdk.agent import Agent


def test_interrupted_report_ends_the_session_interrupted_with_its_reason():
    engine = MagicMock()
    Agent._end_session_for_report(
        engine, {"status": "interrupted", "interrupt_reason": "device_offline"}
    )
    engine.end_session.assert_called_once_with("interrupted", interrupt_reason="device_offline")


def test_interrupted_report_without_a_reason_defaults_to_device_offline():
    engine = MagicMock()
    Agent._end_session_for_report(engine, {"status": "interrupted"})
    engine.end_session.assert_called_once_with(
        "interrupted", interrupt_reason=InterruptReason.DEVICE_OFFLINE
    )


@pytest.mark.parametrize("report", [{"status": "failed"}, {"status": "blocked"}, {}])
def test_other_reports_end_the_session_failed(report):
    engine = MagicMock()
    Agent._end_session_for_report(engine, report)
    engine.end_session.assert_called_once_with("failed")
