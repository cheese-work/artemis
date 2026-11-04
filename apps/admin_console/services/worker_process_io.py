# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Low-level worker subprocess I/O plumbing for the task queue.

These helpers are deliberately free of admin-console state: they deal only
with spawning options, output forwarding, and process-exit observation for a
single worker subprocess. TaskQueueService exposes them as its private
static methods so existing callers and tests keep working unchanged.
"""

import asyncio
import codecs
import os
import psutil
import subprocess
import sys
from typing import Any
import logging

from artemis.utils.redaction import LineRedactor

logger = logging.getLogger(__name__)


def subprocess_creation_kwargs() -> dict[str, Any]:
    """Isolate task workers from the UI server's Windows console.

    A new process group alone is insufficient on Windows: the worker still
    shares the parent's console, so a CTRL_C_EVENT generated anywhere in
    that console can reach the UI server. CREATE_NO_WINDOW removes that
    shared console boundary.

    Output is captured on every platform so it can be forwarded to the
    server terminal and teed into the trace's stdout.log (the daemon itself
    is often spawned with its stdio discarded, so inheriting would lose the
    worker's logs entirely).
    """
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return kwargs


async def forward_worker_output(
    stream: asyncio.StreamReader | None, log_path: str | None = None
) -> None:
    """Forward a worker's combined output without corrupting UTF-8.

    When ``log_path`` is given, the output is also teed into that file so
    the trace's advertised stdout.log actually exists for diagnostics.
    """
    if stream is None:
        return

    log_file = None
    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_file = open(log_path, "w", buffering=1, encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            logger.exception("event=worker_log_open_failed")

    def _emit(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        if log_file is not None:
            try:
                log_file.write(text)
            except (OSError, ValueError):
                # Best-effort tee into stdout.log; console output above
                # already carried the text.
                logger.exception("event=worker_log_write_failed")

    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        lines = LineRedactor()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                _emit(lines.feed(text))

        tail = decoder.decode(b"", final=True)
        _emit(lines.feed(tail, final=True))
    finally:
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                # Flush-on-close of the best-effort tee failed; nothing to do.
                logger.exception("event=worker_log_close_failed")


async def finish_output_forwarder(output_task: asyncio.Task[None] | None) -> None:
    """Drain final worker output without allowing inherited handles to stall the queue."""
    if output_task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(output_task), timeout=2.0)
    except asyncio.CancelledError:
        if output_task.cancelled():
            return
        raise
    except TimeoutError:
        output_task.cancel()
        try:
            await output_task
        except asyncio.CancelledError:
            pass
    except Exception as exc:
        logger.exception("event=worker_output_forward_failed")


async def wait_for_worker_process(proc: asyncio.subprocess.Process) -> int:
    """Wait for worker process to exit, with watchdog fallback if PID was reaped externally."""
    while True:
        try:
            return await asyncio.wait_for(proc.wait(), timeout=1.0)
        except TimeoutError:
            pid = getattr(proc, "pid", None)
            if pid:
                try:
                    p = psutil.Process(pid)
                    if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                        return proc.returncode if proc.returncode is not None else -15
                except (psutil.NoSuchProcess, ProcessLookupError):
                    return proc.returncode if proc.returncode is not None else -15
                except psutil.Error:
                    # Transient probe failure (e.g. AccessDenied): keep waiting.
                    pass
            else:
                return proc.returncode if proc.returncode is not None else -15
