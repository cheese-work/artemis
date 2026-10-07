from unittest.mock import patch

import pytest

from artemis.sdk.agent import Agent


@pytest.mark.parametrize(
    ("error", "reason", "message"),
    [
        (
            "scrcpy failed to start: java.lang.NoSuchMethodException: "
            "android.content.IClipboard$Stub$Proxy.addPrimaryClipChangedListener",
            "recorder_incompatible",
            "No recording: scrcpy is incompatible with this phone's Android version.",
        ),
        (
            "scrcpy executable was not found",
            "recorder_failed",
            "No recording: scrcpy executable was not found",
        ),
        ("", "recorder_failed", "No recording: the recorder could not start."),
    ],
)
def test_recording_startup_failure_publishes_a_clear_reason(error, reason, message):
    with patch("artemis.sdk.agent.publish_startup_progress") as publish:
        Agent._report_recording_unavailable(error, "session-1")

    publish.assert_called_once_with(
        "recording_unavailable", message, session_id="session-1", reason=reason
    )
