import asyncio
import io
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from artemis.utils.redaction import (
    Redactor,
    RedactingStream,
    bind_session,
    goal_metadata,
    read_goal_file,
    write_goal_file,
)
from artemis.utils.logger import ArtemisLogger, DataEngineHandler
from apps.admin_console.services.worker_process_io import forward_worker_output


@pytest.fixture(autouse=True)
def reset_log_context():
    bind_session(None)
    yield
    bind_session(None)


def test_own_handlers_redact_message_exception_and_data_engine(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "configured-credential-sentinel")
    bind_session("s5-test", "Login with password=goal-credential-sentinel")
    logger = ArtemisLogger("s5-test-logger")
    assert logger.logger.propagate is False
    assert all(
        any(isinstance(item, Redactor) for item in handler.filters)
        for handler in logger.logger.handlers
    )
    record = logging.LogRecord(
        "s5", logging.ERROR, __file__, 1, "%s", ("configured-credential-sentinel",), None
    )
    try:
        raise ValueError("goal-credential-sentinel")
    except ValueError:
        record.exc_info = sys.exc_info()
    redactor = Redactor()
    assert redactor.filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    assert "configured-credential-sentinel" not in rendered
    assert "goal-credential-sentinel" not in rendered
    assert "ValueError" in rendered
    assert "session_id=s5-test" in rendered
    assert any(isinstance(handler, DataEngineHandler) for handler in logger.logger.handlers)


def test_redactor_fails_closed_on_bad_record():
    class BrokenMessage:
        def __str__(self):
            raise RuntimeError("cannot render")

    record = logging.LogRecord("s5", logging.ERROR, __file__, 1, BrokenMessage(), (), None)
    assert Redactor().filter(record)
    assert "REDACTED" in record.getMessage()


def test_standalone_short_bearer_tokens_are_redacted():
    assert "tiny" not in Redactor().redact("Bearer tiny")


def test_working_directory_environment_values_are_not_credentials(monkeypatch):
    monkeypatch.setenv("PWD", "/ordinary-current-directory")
    monkeypatch.setenv("OLDPWD", "/ordinary-previous-directory")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-credential")
    rendered = Redactor().redact(
        "/ordinary-current-directory /ordinary-previous-directory private-api-credential"
    )
    assert "/ordinary-current-directory" in rendered
    assert "/ordinary-previous-directory" in rendered
    assert "private-api-credential" not in rendered


def test_short_goal_lines_do_not_mask_ordinary_diagnostics():
    bind_session("line-test", "Login page\nI\npassword=private-typed-value")
    assert Redactor().redact("INFO ordinary I diagnostics") == "INFO ordinary I diagnostics"
    assert "Login page" not in Redactor().redact("Login page")
    assert "private-typed-value" not in Redactor().redact("private-typed-value")


def test_redacting_stream_finishes_a_tee_without_closed_property():
    output = io.StringIO()
    tee = SimpleNamespace(write=output.write, flush=output.flush)
    stream = RedactingStream(tee)
    stream.write("password=stream-private-value")
    stream.finish()
    assert "stream-private-value" not in output.getvalue()
    assert "REDACTED" in output.getvalue()


@pytest.mark.parametrize("backend", ["adb", "ime"])
@pytest.mark.asyncio
async def test_text_input_failure_logs_length_not_the_text(monkeypatch, backend):
    from artemis.drivers.android import adb_driver, input_ime

    text = "unknown-user-input-value"
    device = MagicMock()
    device.shell.side_effect = ValueError(f"input failed: {text}")
    if backend == "adb":
        driver = adb_driver.AndroidAdbDriver("mock-device", MagicMock())
        driver._device = device
        operation = driver.input_text
        module = adb_driver
    else:
        operation = input_ime.AndroidInputIME(device).type_text
        module = input_ime
    logger = MagicMock()
    monkeypatch.setattr(module, "logger", logger)
    assert await operation(text) is False
    message = str(logger.error.call_args)
    assert text not in message
    assert f"text_length={len(text)}" in message


def test_uvicorn_handlers_are_redacted():
    from apps.admin_console.server import REDACTED_UVICORN_LOGGING
    import uvicorn

    uvicorn.Config("apps.admin_console.server:proxy_aware_app", log_config=REDACTED_UVICORN_LOGGING)
    for name in ("uvicorn", "uvicorn.access"):
        assert all(
            any(isinstance(item, Redactor) for item in handler.filters)
            for handler in logging.getLogger(name).handlers
        )


@pytest.mark.parametrize(
    ("request_path", "expected_path"),
    [
        ("/health", "/health"),
        ("/login?token=access-query-sentinel", "/login?token=[REDACTED]"),
        ("/configured-access-sentinel", "/[REDACTED]"),
        ("/typed-goal-sentinel", "/[REDACTED]"),
    ],
)
def test_uvicorn_access_records_are_emitted_and_redacted(
    monkeypatch, capsys, request_path, expected_path
):
    from apps.admin_console.server import REDACTED_UVICORN_LOGGING
    import uvicorn

    monkeypatch.setenv("OPENAI_API_KEY", "configured-access-sentinel")
    bind_session("access-regression", "password=typed-goal-sentinel")
    uvicorn.Config(
        "apps.admin_console.server:proxy_aware_app",
        log_config=REDACTED_UVICORN_LOGGING,
        use_colors=False,
    )
    capsys.readouterr()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:12345", "GET", request_path, "1.1", 200
    )

    output = capsys.readouterr()
    assert "127.0.0.1:12345" in output.out
    assert f"GET {expected_path} HTTP/1.1" in output.out
    assert "200" in output.out
    assert "session_id=access-regression" in output.out
    for secret in ("access-query-sentinel", "configured-access-sentinel", "typed-goal-sentinel"):
        assert secret not in output.out + output.err
    assert output.err == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and symlink boundary")
def test_goal_file_refuses_public_files_and_symlinks(tmp_path):
    public = tmp_path / "public-goal"
    public.write_text("private goal")
    public.chmod(0o644)
    with pytest.raises(ValueError):
        read_goal_file(str(public))
    assert public.exists()
    link = tmp_path / "linked-goal"
    link.symlink_to(public)
    with pytest.raises(OSError):
        read_goal_file(str(link))


def test_data_engine_payload_is_redacted(monkeypatch):
    engine = MagicMock(current_session_id="s5-data-engine")
    monkeypatch.setitem(
        sys.modules, "artemis.data_engine.engine", SimpleNamespace(_CURRENT_DATA_ENGINE=engine)
    )
    monkeypatch.setenv("OPENAI_API_KEY", "data-engine-secret-sentinel")
    logger = ArtemisLogger("s5-data-engine-logger")
    bind_session("s5-data-engine", "password=typed-secret-sentinel")
    logger.error("data-engine-secret-sentinel typed-secret-sentinel")
    handler = next(item for item in logger.logger.handlers if isinstance(item, DataEngineHandler))
    handler._log_queue.join()
    payload = engine.record_trace.call_args.kwargs["payload"]
    assert "data-engine-secret-sentinel" not in str(payload)
    assert "typed-secret-sentinel" not in str(payload)
    assert "session_id=s5-data-engine" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_keep_separate_contexts():
    async def run(session_id):
        bind_session(session_id, f"password={session_id}-secret")
        await asyncio.sleep(0)
        record = logging.LogRecord(
            "s5", logging.INFO, __file__, 1, f"{session_id}-secret", (), None
        )
        Redactor().filter(record)
        return record.getMessage()

    first, second = await asyncio.gather(run("first"), run("second"))
    assert first == "session_id=first [REDACTED]"
    assert second == "session_id=second [REDACTED]"


@pytest.mark.asyncio
async def test_forwarder_discards_oversized_lines(capsys):
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 70000 + b"password=oversized-secret\nnormal line\n")
    stream.feed_eof()
    await forward_worker_output(stream)
    output = capsys.readouterr().out
    assert "oversized-secret" not in output
    assert len(output) < 100
    assert "normal line" in output


def test_goal_file_is_private_single_use_and_metadata_has_no_goal(tmp_path):
    goal = "Login with password=sentinel-file-value"
    path = write_goal_file(goal, directory=tmp_path)
    if os.name != "nt":
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    assert read_goal_file(path) == goal
    assert not Path(path).exists()
    assert goal not in goal_metadata(goal)
    assert "goal_length=" in goal_metadata(goal)


def test_goal_file_write_failure_removes_partial_file(tmp_path):
    with pytest.raises(TypeError):
        write_goal_file(None, directory=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_forwarder_reports_invalid_log_path_without_losing_redacted_output(capsys):
    stream = asyncio.StreamReader()
    stream.feed_data(b"password=invalid-path-secret\n")
    stream.feed_eof()
    await forward_worker_output(stream, "invalid\0/stdout.log")
    output = capsys.readouterr().out
    assert "invalid-path-secret" not in output
    assert "REDACTED" in output


@pytest.mark.asyncio
async def test_forwarder_redacts_chunk_splits_and_tail(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "split-secret-sentinel")
    stream = asyncio.StreamReader()
    task = asyncio.create_task(forward_worker_output(stream, str(tmp_path / "stdout.log")))
    stream.feed_data(b"password=split-sec")
    await asyncio.sleep(0)
    assert capsys.readouterr().out == ""
    stream.feed_data(b"ret-sentinel\nBearer abcdefghijklmnop")
    stream.feed_eof()
    await task
    output = capsys.readouterr().out
    assert "split-secret-sentinel" not in output
    assert "abcdefghijklmnop" not in output
    assert "REDACTED" in output
    assert (tmp_path / "stdout.log").read_text() == output


def test_real_worker_consumes_goal_file_without_goal_in_argv_or_error(tmp_path):
    goal = "Login with password=real-worker-sentinel"
    path = write_goal_file(goal, directory=tmp_path)
    repository = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "ARTEMIS_TASK_WORKER": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": str(repository),
    }
    command = [sys.executable, "-m", "artemis.main", "--goal-file", path, "--unknown-s5-option"]
    result = subprocess.run(
        command, env=environment, cwd=repository, capture_output=True, text=True, timeout=60
    )
    assert result.returncode != 0
    assert not Path(path).exists()
    assert goal not in " ".join(command)
    assert "real-worker-sentinel" not in result.stdout + result.stderr
    assert "unknown-s5-option" in result.stderr
