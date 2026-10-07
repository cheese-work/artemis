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
        (
            "recorder failed\n" + "stack trace\n" * 1000,
            "recorder_failed",
            "No recording: recorder failed",
        ),
        ("x" * 4000, "recorder_failed", "No recording: " + "x" * 200),
        ("\n\n", "recorder_failed", "No recording: the recorder could not start."),
        (
            "java.lang.AssertionError: java.lang.NoSuchMethodException: "
            "android.view.SurfaceControl.createDisplay [class java.lang.String, boolean]",
            "recorder_incompatible",
            "No recording: scrcpy is incompatible with this phone's Android version.",
        ),
        (
            "Unsupported scrcpy 1.25: scrcpy too old for this phone's Android version; "
            "recording requires scrcpy 2.4 or newer",
            "recorder_incompatible",
            "No recording: scrcpy too old for this phone's Android version.",
        ),
    ],
    ids=["clipboard", "missing_binary", "empty", "multiline", "long", "blank", "display", "old"],
)
def test_recording_startup_failure_publishes_a_clear_reason(error, reason, message):
    with patch("artemis.sdk.agent.publish_startup_progress") as publish:
        Agent._report_recording_unavailable(error, "session-1")

    publish.assert_called_once_with(
        "recording_unavailable", message, session_id="session-1", reason=reason
    )
