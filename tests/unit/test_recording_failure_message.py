import pytest

from apps.admin_console.routers import media as media_router
from artemis.utils import video as video_utils

PUSH_LOG = (
    "/usr/share/scrcpy/scrcpy-server: 1 file pushed, 0 skipped. 141.9 MB/s (41650 bytes in 0.000s)"
)
CLIPBOARD_ERROR = (
    "java.lang.NoSuchMethodException: "
    "android.content.IClipboard$Stub$Proxy.addPrimaryClipChangedListener "
    "[interface android.content.IOnPrimaryClipChangedListener, class java.lang.String, int]\n"
    "\tat com.genymobile.scrcpy.Device.<init>(Device.java:100)\n"
)
CLIPBOARD_MESSAGE = (
    "The screen recorder was not compatible with this Android version. "
    "Fixed on 8 Oct 2026 (CHE-1264); new runs record normally."
)
GENERIC_MESSAGE = "The screen recorder could not start on this phone."


def test_clipboard_incompatibility_is_explained():
    assert video_utils.describe_recording_failure(f"{PUSH_LOG}\n{CLIPBOARD_ERROR}") == (
        "recorder_android_incompatible",
        CLIPBOARD_MESSAGE,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "ERROR: Server connection failed",
        "java.lang.NoSuchMethodException: some.other.Method",
        f"{PUSH_LOG}\nsomething odd",
    ],
)
def test_unknown_failure_gets_generic_message(raw):
    assert video_utils.describe_recording_failure(raw) == ("recorder_start_failed", GENERIC_MESSAGE)


@pytest.mark.parametrize("raw", [None, "", "  \n "])
def test_empty_failure_gets_generic_message(raw):
    assert video_utils.describe_recording_failure(raw) == ("recorder_start_failed", GENERIC_MESSAGE)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            f"{PUSH_LOG}\njava.lang.IllegalStateException: boom\n\tat x",
            "java.lang.IllegalStateException: boom",
        ),
        (f"{PUSH_LOG}\nERROR: Server connection failed", "ERROR: Server connection failed"),
        (f"{PUSH_LOG}\ndevice disconnected\n\n", "device disconnected"),
        (PUSH_LOG, ""),
        ("", ""),
    ],
)
def test_error_line_skips_push_log(raw, expected):
    assert video_utils.recording_error_line(raw) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            f"scrcpy failed to start: {PUSH_LOG}\n{CLIPBOARD_ERROR}",
            {
                "message": CLIPBOARD_MESSAGE,
                "reason": "recorder_android_incompatible",
                "detail": f"{PUSH_LOG}\n{CLIPBOARD_ERROR}",
            },
        ),
        (
            f"scrcpy failed to start: {PUSH_LOG}\nERROR: nope",
            {
                "message": GENERIC_MESSAGE,
                "reason": "recorder_start_failed",
                "detail": f"{PUSH_LOG}\nERROR: nope",
            },
        ),
    ],
)
def test_failed_video_response_classifies_historical_scrcpy_rows(monkeypatch, error, expected):
    recording = {"session_id": "s1", "status": "failed", "error": error}
    monkeypatch.setattr(
        media_router,
        "session_repo",
        type(
            "Repo",
            (),
            {
                "get_video_recordings_map": lambda self: {},
                "get_session_by_id": lambda self, sid: {"session_id": sid},
                "get_video_recording_for_session": lambda self, sid: recording,
            },
        )(),
        raising=False,
    )
    monkeypatch.setattr(media_router.media_service, "build_video_index", lambda: {})
    monkeypatch.setattr(media_router.media_service, "resolve_video_url", lambda *a: None)

    response = media_router._get_session_video_sync("s1")

    assert response["status"] == "failed"
    assert {key: response[key] for key in expected} == expected


def _serve_failed_recording(monkeypatch, recording):
    monkeypatch.setattr(
        media_router,
        "session_repo",
        type(
            "Repo",
            (),
            {
                "get_video_recordings_map": lambda self: {},
                "get_session_by_id": lambda self, sid: {"session_id": sid},
                "get_video_recording_for_session": lambda self, sid: recording,
            },
        )(),
        raising=False,
    )
    monkeypatch.setattr(media_router.media_service, "build_video_index", lambda: {})
    monkeypatch.setattr(media_router.media_service, "resolve_video_url", lambda *a: None)
    return media_router._get_session_video_sync("s1")


def test_failed_video_response_uses_stored_reason(monkeypatch):
    # The stored reason wins over read-time classification of the raw output.
    response = _serve_failed_recording(
        monkeypatch,
        {
            "session_id": "s1",
            "status": "failed",
            "error": f"scrcpy failed to start: {PUSH_LOG}\n{CLIPBOARD_ERROR}",
            "reason": "recorder_start_failed",
        },
    )

    assert response["reason"] == "recorder_start_failed"
    assert response["message"] == GENERIC_MESSAGE
    assert response["detail"] == f"{PUSH_LOG}\n{CLIPBOARD_ERROR}"


def test_failed_video_response_without_stored_reason_is_classified_on_read(monkeypatch):
    response = _serve_failed_recording(
        monkeypatch,
        {
            "session_id": "s1",
            "status": "failed",
            "error": f"scrcpy failed to start: {CLIPBOARD_ERROR}",
            "reason": None,
        },
    )

    assert response["reason"] == "recorder_android_incompatible"
    assert response["message"] == CLIPBOARD_MESSAGE


def test_recording_failure_message_falls_back_for_unknown_reason():
    assert video_utils.recording_failure_message("recorder_android_incompatible") == (
        CLIPBOARD_MESSAGE
    )
    assert video_utils.recording_failure_message("something_new") == GENERIC_MESSAGE
