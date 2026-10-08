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

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
from typing import Any
import uuid

try:
    from admin_console.core.state import IN_FLIGHT_STATUSES, state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.services import worker_process_io
    from admin_console.services.host_admission import RunPhase, WaitReason, host_admission
    from admin_console.services.host_admission import enabled as host_agent_enabled
    from admin_console.services.media_service import media_service
except ImportError:
    from apps.admin_console.core.state import IN_FLIGHT_STATUSES, state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.services import worker_process_io
    from apps.admin_console.services.host_admission import RunPhase, WaitReason, host_admission
    from apps.admin_console.services.host_admission import enabled as host_agent_enabled
    from apps.admin_console.services.media_service import media_service

from apps.admin_console.services import run_images
from artemis.config import (
    PAUSE_FILE,
    TEST_DATA_DIR,
    TEST_OUTPUTS_DIR,
    WORKSPACE_ROOT,
)
from artemis.runtime import (
    AdbEndpoint,
    AdbTarget,
    DeviceExecutionLock,
    clear_cancel_request,
    current_adb_endpoint,
    pid_is_alive,
    process_supervisor,
    request_cancel,
    trace_store,
)
from artemis.runtime.adb_endpoint import InvalidAdbEndpoint
from artemis.runtime.host_endpoints import host_endpoints
from artemis.runtime.lifecycle import TERMINAL_STATUSES
from artemis.runtime.run_device_binding import RunDeviceBinding
from artemis.utils.redaction import bind_session, goal_metadata, write_goal_file

logger = logging.getLogger(__name__)

_STOPPED_FROM_FRONTEND = "Task stopped from the Artemis frontend."
_CANCELLED_WHILE_QUEUED = "Queued task cancelled from the Artemis frontend."
# Cadence of the sweep that fails running sessions whose worker vanished.
_VANISHED_WORKER_SWEEP_SECONDS = 15.0


class TaskEndpointUnavailable(RuntimeError):
    """A queued task's adb endpoint snapshot cannot be turned into an endpoint."""


class ServerDraining(RuntimeError):
    """Admission is closed: the server is draining for a deploy."""

    code = "server_draining"
    retry_after_seconds = 5

    def __init__(self) -> None:
        super().__init__("Server is draining for a deploy; retry shortly.")


class TaskQueueService:
    """Service managing FIFO task execution, background worker, subprocess lifecycle,
    and startup tasks.
    """

    # Strong references to in-flight _execute_task_item tasks (asyncio itself only
    # keeps weak references to running tasks).
    _run_tasks: set[asyncio.Task] = set()
    # Deadline enforcers for graceful stops (see _stop_worker_gracefully).
    _forced_stop_tasks: set[asyncio.Task] = set()
    # In-flight run coroutines by session id, so a NACKed start can be cancelled.
    _run_tasks_by_session: dict[str, asyncio.Task] = {}

    DEFAULT_CANCEL_GRACE_SECONDS = 45.0

    @classmethod
    def _cancel_grace_seconds(cls) -> float:
        """How long a worker may finalize itself before it is killed.

        ``ARTEMIS_CANCEL_GRACE_SECONDS=0`` restores the legacy immediate kill.
        """
        raw = os.getenv("ARTEMIS_CANCEL_GRACE_SECONDS")
        if raw is None or not raw.strip():
            return cls.DEFAULT_CANCEL_GRACE_SECONDS
        try:
            return max(0.0, float(raw))
        except ValueError:
            return cls.DEFAULT_CANCEL_GRACE_SECONDS

    @staticmethod
    def _hard_kill(pid: int, process_created_at: float = 0.0) -> bool:
        if not pid:
            return False
        if process_created_at and process_created_at > 0:
            return process_supervisor.terminate_tree_verified(pid, process_created_at)
        try:
            return process_supervisor.terminate_tree(pid)
        except Exception:
            return False

    @classmethod
    def _stop_worker_gracefully(
        cls,
        pid: Any,
        process_created_at: float = 0.0,
        session_id: str | None = None,
        reason: str = "Task stopped from the Artemis frontend.",
    ) -> tuple[bool, bool]:
        """Ask a worker to cancel itself; hard-kill it once the grace period lapses.

        Workers are isolated from the daemon's console (and may belong to
        another ingress process), so instead of a signal the daemon drops a
        cancel marker the worker polls for. Honouring it runs the worker's
        normal cancellation path: the screen recording is stopped and remuxed,
        the trace folder is compiled, and the device lease is released. A
        worker that never picks the marker up is killed after the grace period.

        Returns ``(stopped, deferred)``: ``stopped`` mirrors the legacy kill
        result, ``deferred`` is True when the kill was handed to the deadline
        enforcer instead of happening now.
        """
        try:
            pid_int = int(pid) if pid else 0
        except (TypeError, ValueError):
            pid_int = 0
        grace = cls._cancel_grace_seconds()
        if not pid_int or grace <= 0:
            return cls._hard_kill(pid_int, process_created_at), False

        created_at = float(process_created_at or 0.0)
        if created_at <= 0:
            # PID markers carry the creation time so a leftover marker can never
            # cancel a future process that reuses this PID.
            try:
                import psutil

                created_at = float(psutil.Process(pid_int).create_time())
            except Exception:
                created_at = 0.0
        if not pid_is_alive(pid_int, created_at or None):
            return True, False

        written = request_cancel(
            session_id=str(session_id) if session_id else None,
            pid=pid_int,
            process_created_at=created_at,
            reason=reason,
        )
        if not written:
            return cls._hard_kill(pid_int, created_at), False

        print(
            f"[stop_tasks] Cancel requested for worker {pid_int}"
            f" (session {session_id or 'n/a'}); forcing termination after {grace:.0f}s"
        )
        cls._schedule_forced_stop(pid_int, created_at, grace, session_id)
        return True, True

    @classmethod
    def _stop_proc_gracefully(cls, proc: Any, session_id: Any) -> bool:
        """Graceful variant for a locally spawned process; True when deferred."""
        pid = getattr(proc, "pid", None)
        if not pid or cls._cancel_grace_seconds() <= 0:
            return False
        try:
            _stopped, deferred = cls._stop_worker_gracefully(
                pid, session_id=str(session_id) if session_id else None
            )
        except Exception:
            return False
        return deferred

    @classmethod
    def _schedule_forced_stop(
        cls, pid: int, process_created_at: float, grace: float, session_id: Any
    ) -> None:
        """Kill ``pid`` if it is still alive once ``grace`` seconds have passed."""

        def _still_alive() -> bool:
            return pid_is_alive(pid, process_created_at or None)

        def _force() -> None:
            print(
                f"[stop_tasks] Worker {pid} (session {session_id or 'n/a'}) did not exit"
                f" within {grace:.0f}s of the cancel request; forcing termination."
            )
            cls._hard_kill(pid, process_created_at)

        async def _enforce_async() -> None:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not _still_alive():
                    break
                await asyncio.sleep(0.5)
            else:
                _force()
            clear_cancel_request(session_id=str(session_id) if session_id else None, pid=pid)

        def _enforce_sync() -> None:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not _still_alive():
                    break
                time.sleep(0.5)
            else:
                _force()
            clear_cancel_request(session_id=str(session_id) if session_id else None, pid=pid)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(_enforce_async())
            cls._forced_stop_tasks.add(task)
            task.add_done_callback(cls._forced_stop_tasks.discard)
            return
        threading.Thread(
            target=_enforce_sync, name=f"artemis-forced-stop-{pid}", daemon=True
        ).start()

    @staticmethod
    def _scheduling_lock_key(task_item: dict[str, Any]) -> str:
        """Lock key for the scheduler; never raises.

        A snapshot that can no longer be turned into an endpoint (a host task queued
        before the host agent flag went off) still has an identity to schedule on. The
        task then fails alone when it launches, instead of the scheduler loop dying.
        """
        try:
            return TaskQueueService._task_target(task_item).lock_key
        except TaskEndpointUnavailable:
            snapshot = task_item.get("adb_endpoint")
            identity = snapshot.get("identity") if isinstance(snapshot, dict) else None
            serial = task_item.get("device_serial")
            return f"{identity or 'unavailable'}/{serial or 'pending'}"

    @staticmethod
    def _task_target(task_item: dict[str, Any], *, resolve_host: bool = False) -> AdbTarget:
        """The task's adb target from its queued snapshot.

        A host endpoint is snapshotted by host id; its tunnel port is only valid *now*, so
        ``resolve_host=True`` (used when the worker launches) swaps in the host's live
        endpoint and raises :class:`HostOffline` when it has none. The scheduler keeps
        the snapshot: host-scoped lock keys do not depend on the port.
        """
        endpoint_data = task_item.get("adb_endpoint")
        try:
            endpoint = (
                AdbEndpoint.from_mapping(endpoint_data)
                if isinstance(endpoint_data, dict)
                else current_adb_endpoint()
            )
        except InvalidAdbEndpoint as exc:
            raise TaskEndpointUnavailable(
                f"The task's adb endpoint {endpoint_data!r} cannot be used: {exc}"
            ) from exc
        serial = task_item.get("device_serial")
        target = AdbTarget(
            endpoint=endpoint,
            serial=str(serial) if serial else None,
            host_id=task_item.get("host_id"),
        )
        binding_data = task_item.get("device_binding")
        binding = None
        if binding_data is not None:
            try:
                binding = RunDeviceBinding.from_mapping(binding_data)
                binding.require_selection(target)
            except (KeyError, TypeError, ValueError) as exc:
                raise TaskEndpointUnavailable(f"Invalid run device binding: {exc}") from exc
            if resolve_host:
                try:
                    session = session_repo.read_session(str(task_item.get("session_id")))
                except (OSError, sqlite3.Error) as exc:
                    raise TaskEndpointUnavailable("Cannot verify durable run admission") from exc
                if session is None:
                    raise TaskEndpointUnavailable("Bound run has no durable admission")
                if session.get("status") in TERMINAL_STATUSES:
                    raise TaskEndpointUnavailable(f"Bound run is already {session['status']}")
                try:
                    stored = json.loads(session.get("device_info") or "{}").get("device_binding")
                except (TypeError, ValueError) as exc:
                    raise TaskEndpointUnavailable("Invalid durable run device binding") from exc
                if stored != binding_data:
                    raise TaskEndpointUnavailable("Run binding differs from accepted identity")
            if resolve_host and binding.bridge_session_id:
                try:
                    from admin_console.services.bridge_session_service import bridge_session_service
                except ImportError:
                    from apps.admin_console.services.bridge_session_service import (
                        bridge_session_service,
                    )

                if not any(
                    lease.session_id == binding.bridge_session_id
                    and lease.serial == target.serial
                    and not lease.revoked
                    for lease in bridge_session_service.live_sessions()
                ):
                    TaskQueueService.interrupt_device_binding(
                        str(task_item.get("session_id")), "bridge_closed"
                    )
                    raise TaskEndpointUnavailable("Bound browser lease is unavailable")
        if resolve_host and endpoint.is_host:
            target = AdbTarget(
                host_endpoints.resolve(str(endpoint.host_id)), target.serial, target.host_id
            )
            if binding is not None:
                try:
                    binding.require_selection(target, recovery=True)
                except ValueError as exc:
                    raise TaskEndpointUnavailable(f"Invalid run device binding: {exc}") from exc
        return target

    @classmethod
    def interrupt_device_binding(cls, session_id: str, reason: str) -> None:
        outcome = session_repo.lifecycle.interrupt(session_id, reason)
        if not outcome.committed:
            return
        for run_key, run in list(state.active_runs.items()):
            if str(run.get("session_id") or run_key) != session_id:
                continue
            process = run.get("process")
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    @classmethod
    def interrupt_bridge_binding(cls, bridge_session_id: str) -> None:
        for item in list(state.queue_items):
            if item.get("bridge_session_id") == bridge_session_id:
                cls.interrupt_device_binding(str(item["session_id"]), "bridge_closed")

    @classmethod
    def validate_host_device_inventory(
        cls, host_id: str, generation: int, shared_serials: set[str]
    ) -> None:
        for item in list(state.queue_items):
            binding = item.get("device_binding") or {}
            if (
                binding.get("host_id") == host_id
                and generation >= binding["endpoint"]["generation"]
                and binding.get("serial") not in shared_serials
            ):
                cls.interrupt_device_binding(str(item["session_id"]), "device_offline")

    @classmethod
    def _broadcast_event(
        cls, event_type: str, data: Any, delivered: set[tuple[int, str]] | None = None
    ) -> bool:
        """Broadcasts an event safely to all registered subscribers.

        With ``delivered`` (outcome events), subscribers already recorded there
        are skipped and each success is recorded, so a retry reaches only the
        subscribers that failed. Returns False if any subscriber failed.
        """
        all_ok = True
        for cb in list(state.ipc_subscribers):
            if delivered is not None and (id(cb), event_type) in delivered:
                continue
            try:
                cb(event_type, data)
            except Exception:
                # One broken subscriber must not block the others, but a
                # silent drop hides it entirely.
                all_ok = False
                logger.warning(
                    "Event subscriber %r failed for event %s",
                    cb,
                    event_type,
                    exc_info=True,
                )
            else:
                if delivered is not None:
                    delivered.add((id(cb), event_type))
        return all_ok

    @classmethod
    def _broadcast_startup_progress(cls, session_id: str | None, stage: str, message: str) -> None:
        if not session_id:
            return
        data = {
            "session_id": str(session_id),
            "stage": stage,
            "message": message,
            "timestamp": time.time(),
        }
        state.record_startup_progress(data)
        cls._broadcast_event("startup_progress", data)

    @classmethod
    def _get_next_pending_task(cls) -> dict[str, Any] | None:
        """Finds and returns the first pending task from queue_items."""
        for item in state.queue_items:
            if isinstance(item, dict) and item.get("status") == "pending":
                return item
        return None

    @classmethod
    def _remove_task(cls, session_id: str | None):
        """Removes a task from queue_items by session_id."""
        if not session_id:
            return
        removed_items = [
            t
            for t in state.queue_items
            if isinstance(t, dict) and t.get("session_id") == session_id
        ]
        for item in removed_items:
            DeviceExecutionLock.cancel_reservation(item.get("queue_ticket"))
            if item.get("host_id") and item.get("device_binding"):
                from apps.admin_console.services.host_tunnel import host_tunnels

                host_tunnels.release_run(item["host_id"], str(session_id))
        state.queue_items = [
            t
            for t in state.queue_items
            if not (isinstance(t, dict) and t.get("session_id") == session_id)
        ]

    # Worker subprocess I/O plumbing lives in worker_process_io; the historical
    # private names stay bound here so callers and tests keep working unchanged.
    _subprocess_creation_kwargs = staticmethod(worker_process_io.subprocess_creation_kwargs)
    _forward_worker_output = staticmethod(worker_process_io.forward_worker_output)
    _finish_output_forwarder = staticmethod(worker_process_io.finish_output_forwarder)
    _wait_for_worker_process = staticmethod(worker_process_io.wait_for_worker_process)

    @staticmethod
    async def _terminate_worker_process(proc: asyncio.subprocess.Process | None) -> None:
        """Stop a worker without changing the established POSIX behavior."""
        if proc is None or proc.returncode is not None:
            return
        if sys.platform == "win32":
            await process_supervisor.stop_process(proc)
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @classmethod
    def ensure_worker_running(cls):
        """Guarantees that the background queue worker task is active and running."""
        try:
            loop = asyncio.get_running_loop()
            if state.worker_task is None or state.worker_task.done():
                state.worker_task = loop.create_task(cls.queue_worker())
                try:
                    state.wake_event.set()
                except AttributeError:
                    # Partially initialized state (unit tests): the worker
                    # loop polls anyway.
                    pass
        except RuntimeError:
            pass

    @classmethod
    def _concurrency_limit(cls) -> int:
        """Return 0 for per-device concurrency, or the global task limit."""
        return DeviceExecutionLock.resolve_env_concurrency()

    @classmethod
    def _reap_vanished_workers(cls) -> list[str]:
        """Fail running sessions that no live worker, lock or run owns.

        This is the writer-side replacement for the repair that used to run
        inside ``GET /api/sessions``; it covers workers started outside this
        server (CLI, MCP) that died without finalizing. It also redelivers any
        outcome event left pending by a crash or an earlier delivery failure.
        """
        cls._drain_outcome_events()
        try:
            owner_sids = {
                str(owner.session_id)
                for owner in DeviceExecutionLock.get_active_owners().values()
                if owner.session_id
            }
        except Exception:
            return []  # lock state unknown: never declare a worker dead on a guess
        in_flight = {
            str(i.get("session_id"))
            for i in state.queue_items
            if isinstance(i, dict) and i.get("status") in IN_FLIGHT_STATUSES
        }

        def owned(sid: str) -> bool:
            return (
                sid in owner_sids
                or sid in in_flight
                or sid in state.active_runs
                or sid in state.active_connections
                or (state.is_running and sid == str(state.active_session_id))
            )

        swept = session_repo.lifecycle.fail_vanished_workers(session_repo.process_is_alive, owned)
        for sid in swept:
            print(f"[QueueWorker] Failed vanished running session {sid}")
            cls._deliver_outcome(sid)
        return swept

    @classmethod
    async def queue_worker(cls):
        """Persistent dispatcher scheduling pending tasks onto devices.

        Scans the queue and launches each eligible task as an independent
        coroutine. Admission is governed by _concurrency_limit(): 0 admits one
        task per device so distinct devices execute concurrently, N>=1 admits
        at most N tasks across all devices. Per-device FIFO ordering and
        cross-process mutual exclusion remain enforced by DeviceExecutionLock
        inside each worker process.
        """
        print(
            f"[QueueWorker] Dispatcher initialized (concurrency limit: {cls._concurrency_limit()})."
        )
        try:
            DeviceExecutionLock.cleanup_stale_locks()
        except Exception as exc:
            print(f"[QueueWorker] Initial stale lock cleanup notice: {exc}")

        next_sweep = time.monotonic()
        try:
            while True:
                try:
                    cls._dispatch_pending_tasks()
                except Exception as exc:
                    print(f"[QueueWorker] Dispatch error: {exc}")
                if time.monotonic() >= next_sweep:
                    next_sweep = time.monotonic() + _VANISHED_WORKER_SWEEP_SECONDS
                    try:
                        cls._reap_vanished_workers()
                    except Exception as exc:
                        print(f"[QueueWorker] Vanished-worker sweep error: {exc}")
                try:
                    await asyncio.wait_for(state.wake_event.wait(), timeout=0.3)
                    state.wake_event.clear()
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            print("[QueueWorker] Dispatcher received cancellation signal.")
            for run in list(state.active_runs.values()):
                run_proc = run.get("process")
                if run_proc is not None and run_proc.returncode is None:
                    await cls._terminate_worker_process(run_proc)
            raise

    @classmethod
    def _dispatch_pending_tasks(cls) -> None:
        """Launch every pending task admissible under the current concurrency limit."""
        limit = cls._concurrency_limit()
        state.prune_finished_runs()
        cls._promote_started_runs()
        if limit == 1 and state.is_running:
            return

        # Dispatched-but-not-yet-spawned runs are only visible as queue items in
        # "running" state, so admission must count those too -- active_runs alone
        # lags behind by the subprocess startup latency.
        in_flight = [
            i
            for i in state.queue_items
            if isinstance(i, dict) and i.get("status") in IN_FLIGHT_STATUSES
        ]
        capacity = None
        if limit >= 1:
            # Registered workers still have running queue rows until finalization.
            active_pids = {
                getattr(run.get("process"), "pid", None) for run in state.active_runs.values()
            } - {None}
            starting = sum(
                str(item.get("session_id")) not in state.active_runs
                and item.get("pid") not in active_pids
                for item in in_flight
            )
            capacity = limit - len(state.active_runs) - starting
            if capacity <= 0:
                return
        busy_devices = state.busy_device_ids | {
            cls._scheduling_lock_key(i) for i in in_flight if i.get("device_serial")
        }
        dispatched_any = False
        loop = asyncio.get_running_loop()
        for item in list(state.queue_items):
            if not (isinstance(item, dict) and item.get("status") == "pending"):
                continue
            sess_id = item.get("session_id")
            if sess_id and str(sess_id) in state.cancelled_session_ids:
                cls._remove_task(sess_id)
                continue

            device = item.get("device_serial")
            lock_key = cls._scheduling_lock_key(item)
            if limit == 0:
                # A task without a resolved device may bind to any serial, so it
                # only launches on an otherwise idle scheduler; the device lock
                # then allocates freely without contending against active runs.
                if device is None and (state.active_runs or in_flight or dispatched_any):
                    continue
                if device is not None and lock_key in busy_devices:
                    continue
            elif limit > 1 and device is not None and lock_key in busy_devices:
                # A second worker for this device would wait on its lock.
                continue

            if item.get("host_id") and host_agent_enabled() and not cls._host_device_eligible(item):
                # Another owner holds the device lock or is ahead in its queue (any
                # ingress): waiting here must not hold one of the host's run slots.
                item["wait_reason"] = str(WaitReason.DEVICE_BUSY)
                continue
            # The single host-admission hook: a host that is not active, is full or
            # shares an ambiguous device keeps the row waiting, in place.
            if (reason := host_admission.admit(item)) is not None:
                item["wait_reason"] = str(reason)
                continue
            item.pop("wait_reason", None)

            item["status"] = "starting" if host_agent_enabled() else "running"
            dispatched_any = True
            if device is not None:
                busy_devices.add(lock_key)
            # Count from scheduling, not from the coroutine's first step: a stop
            # can drop the queue row, or cancel the task, before it ever runs.
            run_key = str(sess_id) if sess_id else uuid.uuid4().hex
            state.executing_run_keys.add(run_key)
            run_task = loop.create_task(cls._execute_task_item(item))
            run_task.add_done_callback(
                lambda _t, key=run_key: state.executing_run_keys.discard(key)
            )
            cls._run_tasks_by_session[run_key] = run_task
            # A run cancelled before its first step never reaches its own cleanup.
            run_task.add_done_callback(
                lambda t, key=run_key: (
                    host_admission.release(key),
                    cls._run_tasks_by_session.pop(key, None)
                    if cls._run_tasks_by_session.get(key) is t
                    else None,
                )
            )
            # Hold a strong reference: asyncio keeps only weak refs to running
            # tasks, and a collected run would strand its queue item forever.
            cls._run_tasks.add(run_task)
            run_task.add_done_callback(cls._run_tasks.discard)
            if capacity is not None:
                capacity -= 1
                if capacity <= 0:
                    break

    @classmethod
    def _held_lock_session_ids(cls) -> set[str] | None:
        """Sessions whose worker holds a device lock; None when lock state is unreadable."""
        try:
            return {
                str(owner.session_id)
                for owner in DeviceExecutionLock.get_active_owners().values()
                if owner.session_id
            }
        except OSError:
            return None

    @classmethod
    def _host_device_eligible(cls, item: dict[str, Any]) -> bool:
        """The authoritative lock and FIFO state lets this row start now; unknown means no."""
        target = cls._task_target(item)
        device = str(item.get("device_serial"))
        try:
            if DeviceExecutionLock.get_active_owner(device, target.lock_scope) is not None:
                return False
            head = DeviceExecutionLock.queue_head_token(device, target.lock_scope)
        except OSError:
            return False
        ticket = item.get("queue_ticket")
        return not (ticket and head and head != ticket)

    @classmethod
    def _promote_started_runs(cls) -> None:
        """Move "starting" rows to "running" once their worker holds the device lock."""
        starting = [
            i for i in state.queue_items if isinstance(i, dict) and i.get("status") == "starting"
        ]
        held = cls._held_lock_session_ids() if starting else None
        if not held:
            return
        now = time.time()
        for item in starting:
            sid = str(item.get("session_id"))
            if sid not in held:
                continue
            item["status"] = "running"
            item["execution_started_at"] = now
            host_admission.mark(sid, RunPhase.RUNNING)
            try:
                session_repo.lifecycle.mark_execution_started(sid, now)
            except (OSError, sqlite3.Error):
                logger.exception("Could not persist execution_started_at for %s", sid)

    @classmethod
    def _begin_task_run(
        cls,
        task_item: dict[str, Any],
        run_key: str,
        sess_id: Any,
        goal: str,
        profile: str,
    ) -> None:
        """Announce the launch; the row is "running" now, or once it holds the lock."""
        if not host_agent_enabled():
            task_item["status"] = "running"
        task_item["start_time"] = time.time()

        # A fresh launch clears a stale stop request left over for this run
        # key from a previous task. Manual stops are tracked per run in
        # manually_stopped_run_ids so stopping one device's task never
        # affects concurrent runs. Per-session stops are tracked in
        # cancelled_session_ids and are unaffected.
        state.manually_stopped_run_ids.discard(run_key)
        state.current_goal = goal
        state.current_profile = profile
        state.active_session_id = sess_id

        cls._broadcast_startup_progress(sess_id, "launching", "Starting the execution process")

        # Broadcast session_started so all connected clients know the task has started
        cls._broadcast_event(
            "session_started",
            {
                "session_id": sess_id,
                "initial_goal": goal,
                "profile": profile,
                "device_serial": task_item.get("device_serial"),
            },
        )

    @classmethod
    def _build_worker_invocation(
        cls,
        task_item: dict[str, Any],
        run_key: str,
        sess_id: Any,
        goal: str,
        profile: str,
        target: AdbTarget,
        base_environment: dict[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Assemble the worker subprocess command line and environment."""
        expected_output = task_item.get("expected_output")
        enable_outputter = task_item.get("enable_outputter")
        verification_level = task_item.get("verification_level")
        explorer_mode = task_item.get("explorer_mode")
        locked_app = task_item.get("locked_app_package") or task_item.get("locked_app")
        app_path = task_item.get("app_path")
        run_id = task_item.get("run_id")

        test_name = f"web_{int(time.time())}_{run_key[:8]}"
        env = dict(base_environment) if base_environment is not None else os.environ.copy()
        env["PYTHON_DOTENV_DISABLED"] = "1"
        pythonpath_parts = [
            str(WORKSPACE_ROOT),
            str(WORKSPACE_ROOT / "apps" / "admin_console"),
            str(WORKSPACE_ROOT / "apps" / "cloud_service"),
            env.get("PYTHONPATH", ""),
        ]
        env["PYTHONPATH"] = os.pathsep.join([p for p in pythonpath_parts if p])
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        if state.ipc_port is not None:
            env["ARTEMIS_IPC_PORT"] = str(state.ipc_port)
        if sess_id:
            env["ARTEMIS_SESSION_ID"] = str(sess_id)
        env["ARTEMIS_TASK_INGRESS"] = str(task_item.get("ingress", "frontend"))
        env["ARTEMIS_TASK_WORKER"] = "1"
        target.endpoint.apply_to_environment(env)
        env[DeviceExecutionLock.LOCK_SCOPE_ENV] = target.lock_scope
        queue_ticket = task_item.get("queue_ticket")
        if queue_ticket:
            env[DeviceExecutionLock.QUEUE_TICKET_ENV] = str(queue_ticket)

        cmd = [
            sys.executable,
            "-m",
            "artemis.main",
            "--goal-file",
            write_goal_file(goal),
            "--profile",
            profile,
            "--test-name",
            test_name,
        ]
        if sess_id:
            cmd.extend(["--session-id", str(sess_id)])
        if run_id:
            cmd.extend(["--run-id", str(run_id)])
        if expected_output:
            cmd.extend(["--output-description", str(expected_output)])
        if enable_outputter is not None:
            cmd.append("--enable-outputter" if enable_outputter else "--disable-outputter")
        if verification_level:
            cmd.extend(["--verification-level", str(verification_level)])
        if explorer_mode:
            cmd.extend(["--explorer-pro-mode", str(explorer_mode)])
        if locked_app:
            cmd.extend(["--locked-app", str(locked_app)])
        if app_path:
            cmd.extend(["--app-path", str(app_path)])
        device_serial = task_item.get("device_serial")
        if device_serial:
            cmd.extend(["--device-serial", str(device_serial)])
            env["ADB_DEVICE_SERIAL"] = str(device_serial)
        if task_item.get("goal_images") and sess_id:
            env.update(run_images.worker_environment(str(sess_id), task_item["goal_images"]))
        return cmd, env

    @classmethod
    def _register_worker_run(
        cls,
        task_item: dict[str, Any],
        run_key: str,
        sess_id: Any,
        goal: str,
        profile: str,
        target: AdbTarget,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Record the spawned worker in shared state and hand it the device reservation."""
        device_serial = task_item.get("device_serial")
        state.current_process = proc
        task_item["pid"] = proc.pid
        state.active_runs[run_key] = {
            "process": proc,
            "session_id": str(sess_id) if sess_id else run_key,
            "device_id": str(device_serial) if device_serial else None,
            "lock_key": target.lock_key if device_serial else None,
            "adb_endpoint": target.endpoint.to_dict(),
            "goal": goal,
            "profile": profile,
            "host_id": target.host_id,
            "device_binding": task_item.get("device_binding"),
            "bridge_session_id": task_item.get("bridge_session_id"),
        }
        if target.host_id and sess_id:
            from apps.admin_console.services.host_tunnel import host_tunnels

            host_tunnels.bind_run(target.host_id, str(sess_id))
        if sess_id:
            try:
                # Preserve status updates written concurrently by the worker.
                trace_store.update_trace_pid(str(sess_id), proc.pid)
            except OSError as exc:
                # Status probes fall back to DB PID bookkeeping, but note it.
                print(
                    f"[QueueWorker] Could not record worker pid in status.json for {sess_id}: {exc}"
                )
        cls._broadcast_startup_progress(sess_id, "process_ready", "Execution process started")
        ingress_type = str(task_item.get("ingress", "frontend"))
        queue_ticket = task_item.get("queue_ticket")
        if queue_ticket:
            transferred = DeviceExecutionLock.transfer_reservation(
                str(queue_ticket),
                proc.pid,
                description=f"{ingress_type} task: {goal_metadata(goal)}",
                device_id=device_serial or "pending",
                session_id=str(sess_id) if sess_id else None,
                ingress=ingress_type,
                lock_scope=target.lock_scope,
            )
            if not transferred:
                print(
                    f"[QueueWorker] Could not transfer queue ticket {queue_ticket} to worker"
                    f" pid {proc.pid}; the worker will queue a fresh ticket itself."
                )

    @classmethod
    def _start_output_forwarder(
        cls, sess_id: Any, proc: asyncio.subprocess.Process
    ) -> asyncio.Task[None] | None:
        """Start forwarding the worker's output; returns the forwarder task, if any."""
        # Forward the worker's combined output and tee it into the trace's
        # stdout.log so the log paths advertised by the MCP API exist.
        log_path = None
        if sess_id:
            try:
                log_path = trace_store.get_trace_stdout_log_path(str(sess_id))
            except Exception:
                log_path = None
        if isinstance(proc.stdout, asyncio.StreamReader):
            return asyncio.create_task(cls._forward_worker_output(proc.stdout, log_path))
        return None

    @classmethod
    async def _terminate_if_cancelled_during_launch(
        cls, run_key: str, sess_id: Any, proc: asyncio.subprocess.Process
    ) -> None:
        """Terminate a worker whose task was cancelled while it was being launched."""
        if sess_id and cls._queue_item_for(sess_id).get("device_binding"):
            try:
                session = session_repo.read_session(str(sess_id))
            except (OSError, sqlite3.Error) as exc:
                await cls._terminate_worker_process(proc)
                raise TaskEndpointUnavailable("Cannot verify durable run admission") from exc
            if session is None:
                await cls._terminate_worker_process(proc)
                raise TaskEndpointUnavailable("Bound run has no durable admission")
            if session.get("status") in TERMINAL_STATUSES:
                await cls._terminate_worker_process(proc)
                return
        if sess_id and session_repo.get_session_status(str(sess_id)) == "interrupted":
            await cls._terminate_worker_process(proc)
        elif sess_id and (
            str(sess_id) in getattr(state, "cancelled_session_ids", set())
            or run_key in state.manually_stopped_run_ids
        ):
            print(f"[QueueWorker] Task [{sess_id}] was cancelled during launch. Terminating.")
            await cls._terminate_worker_process(proc)

    @classmethod
    async def _persist_terminal_session_status(
        cls, sess_id: Any, returncode: int, manual_stop: bool
    ) -> str:
        """Settle the session after its worker exits and return the published status.

        The worker's exit code is only a fallback: the lifecycle authority keeps
        any outcome already committed (completed, cancelled, interrupted, ...).

        Settlement owns the run's outcome from this call on: a host NACK is
        refused once ``settling`` is set, and a requeued row is never settled.
        """
        item = cls._queue_item_for(sess_id)
        if item.get("requeue"):
            return "queued"
        item["settling"] = True  # before the first await: the NACK check cannot miss it
        if item.get("host_id") and not manual_stop:
            from apps.admin_console.services.host_tunnel import host_tunnels

            await host_tunnels.wait_for_run(str(sess_id))
        try:
            outcome = await asyncio.to_thread(
                session_repo.lifecycle.settle_worker_exit, str(sess_id), returncode, manual_stop
            )
        except Exception:
            logger.exception("[QueueWorker] Could not settle terminal status for %s", sess_id)
            outcome = None
        if outcome is not None and outcome.status:
            logger.info("event=task_finished session_id=%s status=%s", sess_id, outcome.status)
            return outcome.status
        fallback = "cancelled" if manual_stop else ("completed" if returncode == 0 else "failed")
        logger.error(
            "[QueueWorker] Could not persist terminal DB status '%s' for session %s",
            fallback,
            sess_id,
        )
        return fallback

    @classmethod
    async def _recover_or_fail_recording(cls, sess_id: Any) -> None:
        """Finalize a recording whose worker died before it could, else mark it failed.

        A worker that stops gracefully finalizes its own recording and this is
        a no-op. A hard-killed (or crashed) worker leaves the raw scrcpy file it
        registered at recording start; remuxing that file is all that is
        needed to publish the full video.
        """
        rec_info = session_repo.get_video_recording_for_session(sess_id)
        rec_status = (rec_info or {}).get("status")
        if rec_status == "ready":
            return

        recovered_url = None
        # 1. Direct recovery from the recording row written at recording start.
        try:
            local_video_path = (rec_info or {}).get("local_video_path")
            if local_video_path:
                start_time = (rec_info or {}).get("start_time")
                if not start_time:
                    session_row = session_repo.get_session_by_id(sess_id)
                    start_time = dict(session_row).get("start_time") if session_row else None
                final_path = await asyncio.to_thread(
                    media_service.recover_orphaned_recording,
                    local_video_path,
                    start_time,
                )
                if final_path:
                    session_repo.mark_recording_ready(sess_id, str(final_path))
                    recovered_url = media_service.path_to_video_url(Path(final_path))
        except Exception as rec_err:
            print(f"[QueueWorker] Error finalizing orphaned recording: {rec_err}")

        # 2. Fallback: locate an already finalized file by folder / session naming.
        if not recovered_url:
            try:
                video_rec_map = session_repo.get_video_recordings_map()
                video_idx = await asyncio.to_thread(media_service.build_video_index)
                fallback_url = await asyncio.to_thread(
                    media_service.resolve_video_url,
                    {"session_id": sess_id},
                    video_rec_map,
                    video_idx,
                )
                if fallback_url:
                    session_repo.mark_recording_ready(sess_id, fallback_url)
                    recovered_url = fallback_url
            except Exception as rec_err:
                print(f"[QueueWorker] Error attempting recording recovery: {rec_err}")

        if recovered_url:
            cls._broadcast_event(
                "recording_ready",
                {"session_id": sess_id, "video_url": recovered_url},
            )
            return

        recording_error = "Task worker exited before recording finalization completed"
        if session_repo.mark_recording_failed_if_pending(sess_id, recording_error):
            cls._broadcast_event(
                "recording_failed",
                {"session_id": sess_id, "error": recording_error},
            )

    # Outcome events are delivered at-least-once from the durable outbox, and
    # every effect is idempotent by ``event_id`` where it lands: the broadcast
    # step first records the event (``record_event``, insert-if-absent) and fans
    # out live only for a new record; the notifiers write-if-absent by event id
    # (FileNotifier) or pass it as an idempotency key (webhook, script). A
    # consumer is marked delivered after its effect, the row is acknowledged
    # once every consumer is done, and a replay after a crash is a no-op. A
    # failing consumer is retried with a persisted attempt count and, after the
    # cap, abandoned durably so the row still acknowledges.
    _MAX_DELIVERY_ATTEMPTS = 5
    _delivery_lock = threading.Lock()
    _delivered_to: dict[str, set[tuple[int, str]]] = {}  # event_id -> (subscriber, event type)
    _fanout_pending: set[str] = set()  # event ids whose live fanout still needs a retry

    @classmethod
    def _forget_delivery_memory(cls) -> None:
        """Drop in-process delivery memory (what a restart does)."""
        cls._delivered_to.clear()
        cls._fanout_pending.clear()

    @classmethod
    def _deliver_outcome(
        cls, sess_id: Any = None, task_item: dict[str, Any] | None = None, goal: str | None = None
    ) -> None:
        """Deliver the pending outcome event of ``sess_id`` (every session when None).

        Every finalizer (worker exit, stop, sweep, startup, shutdown) may call this.
        """
        if sess_id is not None:
            state.active_connections.pop(sess_id, None)
        lifecycle = session_repo.lifecycle
        with cls._delivery_lock:
            try:
                events = lifecycle.pending_events(str(sess_id) if sess_id is not None else None)
            except sqlite3.Error:
                logger.warning("Could not read pending outcome events", exc_info=True)
                return
            delivered: list[str] = []
            for event in events:
                item = task_item
                if item is None or str(item.get("session_id")) != str(event["session_id"]):
                    item = cls._queue_item_for(event["session_id"])
                if cls._deliver_event(lifecycle, event, item, goal):
                    delivered.append(event["dedupe_id"])
            try:
                lifecycle.acknowledge(delivered)
            except sqlite3.Error:
                logger.warning("Could not acknowledge outcome events", exc_info=True)
                return
            for event_id in delivered:
                cls._delivered_to.pop(event_id, None)
                cls._fanout_pending.discard(event_id)

    @classmethod
    def _deliver_event(
        cls, lifecycle: Any, event: dict[str, Any], task_item: dict[str, Any], goal: str | None
    ) -> bool:
        """Run each consumer not yet delivered; False leaves the event pending.

        A failing consumer never blocks the others: each is tried in turn and
        the event stays pending if any is still owed a retry.
        """
        event_id = event["dedupe_id"]
        steps = (
            ("broadcast", "broadcast_at", lambda: cls._broadcast_outcome(lifecycle, event)),
            (
                "notify",
                "notified_at",
                lambda: cls._notify_session_end(
                    lifecycle, task_item, event["session_id"], goal, event["status"], event_id
                ),
            ),
        )
        pending = False
        for consumer, column, effect in steps:
            if event.get(column) is not None:
                continue  # delivered earlier, possibly before a restart
            try:
                ok = effect()
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error):
                logger.warning("Could not %s %s", consumer, event_id, exc_info=True)
                ok = False
            try:
                if ok:
                    lifecycle.mark_delivered(event_id, consumer)
                    continue
                failures = lifecycle.note_failed_attempt(event_id, consumer)
                if failures < cls._MAX_DELIVERY_ATTEMPTS:
                    pending = True
                    continue
                logger.error(
                    "Abandoning %s of %s after %d failed attempts", consumer, event_id, failures
                )
                lifecycle.mark_delivered(event_id, consumer, abandoned=True)
            except sqlite3.Error:
                logger.warning("Could not record %s of %s", consumer, event_id, exc_info=True)
                pending = True
        return not pending

    @classmethod
    def _drain_outcome_events(cls) -> None:
        """Deliver every pending outcome event (startup, shutdown and the periodic sweep)."""
        cls._deliver_outcome(None)

    @staticmethod
    def _queue_item_for(session_id: Any) -> dict[str, Any]:
        return next(
            (
                i
                for i in state.queue_items
                if isinstance(i, dict) and str(i.get("session_id")) == str(session_id)
            ),
            {},
        )

    @classmethod
    def _broadcast_outcome(cls, lifecycle: Any, event: dict[str, Any]) -> bool:
        """Record the event durably, then fan it out live; True once every subscriber took it.

        Recording is insert-if-absent by event id, so only the first delivery
        fans out; a replay after a crash finds the record and does nothing.
        Clients that missed the live fanout replay from ``/api/sessions/{id}/events``.
        """
        event_id = event["dedupe_id"]
        sess_id, status, reason = event["session_id"], event["status"], event["interrupt_reason"]
        messages: list[tuple[str, dict[str, Any]]] = [
            (
                "session_ended",
                {
                    "event_id": event_id,
                    "session_id": sess_id,
                    "status": status,
                    "was_stopped_manually": status == "cancelled",
                    **({"interrupt_reason": reason} if reason else {}),
                },
            )
        ]
        if status == "interrupted":
            messages.append(
                (
                    "run_interrupted",
                    {
                        "event_id": event_id,
                        "session_id": sess_id,
                        "interrupt_reason": reason,
                        "interrupted_at": event["created_at"],
                    },
                )
            )
        fanout = event_id in cls._fanout_pending
        for event_type, data in messages:
            if lifecycle.record_event(event_id, event_type, sess_id, data):
                fanout = True
        if not fanout:
            return True
        done = cls._delivered_to.setdefault(event_id, set())
        ok = all([cls._broadcast_event(t, d, delivered=done) for t, d in messages])
        if ok:
            cls._fanout_pending.discard(event_id)
        else:
            cls._fanout_pending.add(event_id)
        return ok

    @classmethod
    def _notify_session_end(
        cls,
        lifecycle: Any,
        task_item: dict[str, Any],
        sess_id: Any,
        goal: str | None,
        status: str,
        event_id: str | None = None,
    ) -> bool:
        """Dispatch the external notification; False when the notifier did not take it.

        Who to notify comes from the in-memory queue item, else from the context
        persisted with the session, so a restarted server still notifies.
        """
        context = task_item if task_item and task_item.get("conversation_id") else None
        if context is None and not (task_item and task_item.get("ingress") == "mcp"):
            context = lifecycle.get_notify_context(str(sess_id)) or task_item or {}
        context = context or task_item or {}
        conversation_id = context.get("conversation_id")
        if not (conversation_id or context.get("ingress") == "mcp"):
            return True  # nobody to notify
        goal = goal or context.get("goal") or ""
        try:
            from mcp_server.notifiers import notify

            return bool(
                notify(
                    conversation_id=conversation_id or "",
                    message=f"Artemis autonomous task {goal_metadata(goal)} finished with status '{status}'.\nTrace ID: {sess_id}",
                    title=f"Task {status.capitalize()}: {sess_id}",
                    event_type=status,
                    payload={
                        "event_id": event_id,
                        "trace_id": sess_id,
                        "session_id": sess_id,
                        "status": status,
                        "goal_length": len(goal),
                    },
                )
            )
        except Exception as notif_err:
            print(f"[QueueWorker] Notification dispatch notice: {notif_err}")
            return False

    @classmethod
    def _release_run_slot(
        cls,
        sess_id: Any,
        run_key: str,
        proc: asyncio.subprocess.Process | None,
        *,
        keep_row: bool = False,
    ) -> None:
        """Clean up the finished task and release this run's scheduling slot.

        ``keep_row`` leaves the queue row and its ticket for a requeued start.
        """
        if sess_id and not keep_row:
            cls._remove_task(sess_id)
            state.cancelled_session_ids.discard(str(sess_id))
        host_admission.release(run_key)
        state.cancelled_session_ids.discard(run_key)
        state.manually_stopped_run_ids.discard(run_key)
        try:
            clear_cancel_request(
                session_id=str(sess_id) if sess_id else None,
                pid=getattr(proc, "pid", None),
            )
        except (OSError, TypeError, ValueError):
            # Marker cleanup is best-effort: an unwritable temp dir or an odd
            # pid value must not block releasing the run slot.
            pass
        released_run = state.active_runs.pop(run_key, None)
        if released_run and released_run.get("host_id") and sess_id:
            from apps.admin_console.services.host_tunnel import host_tunnels

            host_tunnels.release_run(released_run["host_id"], str(sess_id))
        if proc is not None and state.current_process is proc:
            state.current_process = None
        if not state.active_runs:
            state.active_session_id = None
            state.current_goal = None
            state.current_profile = None
            # No runs left: any not-yet-consumed manual-stop markers are
            # stale and must not leak into future runs.
            state.manually_stopped_run_ids.clear()
        state.wake_event.set()

    @classmethod
    async def _execute_task_item(cls, task_item: dict[str, Any]) -> None:
        """Run one queued task to completion in its own worker subprocess."""
        sess_id = task_item.get("session_id")
        run_key = str(sess_id) if sess_id else uuid.uuid4().hex
        goal = task_item.get("goal")
        bind_session(str(sess_id) if sess_id else None, goal if isinstance(goal, str) else None)
        profile = task_item.get("profile", "flash")
        proc: asyncio.subprocess.Process | None = None
        output_task: asyncio.Task[None] | None = None
        config_snapshot = None
        cmd = None
        state.executing_run_keys.add(run_key)
        try:
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("Queued task must contain a non-empty string goal.")
            cls._begin_task_run(task_item, run_key, sess_id, goal, profile)

            target = cls._task_target(task_item, resolve_host=True)
            from apps.admin_console.services.config_store import get_config_store

            config_snapshot = await get_config_store().snapshot_for_spawn()
            target = cls._task_target(task_item, resolve_host=True)
            cmd, env = cls._build_worker_invocation(
                task_item,
                run_key,
                sess_id,
                goal,
                profile,
                target,
                base_environment=config_snapshot.environment,
            )

            device_serial = task_item.get("device_serial")
            logger.info(
                "event=task_started session_id=%s %s profile=%s",
                sess_id,
                goal_metadata(goal),
                profile,
            )
            # From here a worker process may exist before it is registered: the host
            # NACK (requeue_starting) must treat this run as spawned.
            task_item["worker_spawned"] = True
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(WORKSPACE_ROOT),
                env=env,
                **cls._subprocess_creation_kwargs(),
            )
            cls._register_worker_run(task_item, run_key, sess_id, goal, profile, target, proc)
            output_task = cls._start_output_forwarder(sess_id, proc)
            await cls._terminate_if_cancelled_during_launch(run_key, sess_id, proc)

            # 3. Await subprocess completion
            returncode = await cls._wait_for_worker_process(proc)
            logger.info("event=task_exited session_id=%s returncode=%s", sess_id, returncode)

            manual_stop = run_key in state.manually_stopped_run_ids or bool(
                sess_id and str(sess_id) in state.cancelled_session_ids
            )

            # 4. Perform fallback database status update and notification
            if sess_id:
                await cls._persist_terminal_session_status(sess_id, returncode, manual_stop)
                await cls._recover_or_fail_recording(sess_id)
                cls._deliver_outcome(sess_id, task_item, goal)

        except asyncio.CancelledError:
            print(f"[QueueWorker] Task [{sess_id}] received cancellation signal.")
            if proc is not None and proc.returncode is None:
                await cls._terminate_worker_process(proc)
            raise
        except Exception:
            logger.exception(f"[QueueWorker] Unexpected error executing task [{sess_id}]")
            # A spawn failure must also leave a terminal session status.
            if sess_id:
                try:
                    await cls._persist_terminal_session_status(
                        sess_id, returncode=1, manual_stop=False
                    )
                    cls._deliver_outcome(sess_id, task_item, goal)
                except (OSError, RuntimeError, ValueError):
                    logger.exception(
                        f"[QueueWorker] Could not persist failure status for [{sess_id}]"
                    )
        finally:
            host_admission.mark(run_key, RunPhase.CLEANING_UP)
            await cls._finish_output_forwarder(output_task)
            if cmd is not None and "--goal-file" in cmd:
                try:
                    Path(cmd[cmd.index("--goal-file") + 1]).unlink(missing_ok=True)
                except OSError:
                    logger.exception("event=worker_goal_cleanup_failed")
            if config_snapshot is not None:
                try:
                    config_snapshot.config_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary run config snapshot")
            # 5. Clean up the finished task and release this run's scheduling slot
            try:
                cls._release_run_slot(
                    sess_id, run_key, proc, keep_row=bool(task_item.get("requeue"))
                )
            finally:
                state.executing_run_keys.discard(run_key)

    @classmethod
    def active_run_count(cls) -> int:
        """Accepted work that has not finished cleanup, one count per session.

        A queue row lives until its run slot is released; ``executing_run_keys``
        outlives the row for runs removed early (stop) and ``active_runs`` covers
        registered workers, so a run is counted from acceptance to cleanup end.
        """
        keys = {
            str(item.get("session_id") or f"item:{id(item)}")
            for item in state.queue_items
            if isinstance(item, dict)
        }
        keys.update(state.active_runs)
        keys.update(state.executing_run_keys)
        return len(keys)

    @classmethod
    def set_draining(cls, draining: bool) -> None:
        """Close or reopen admission; running and pending work is untouched."""
        state.draining = draining

    @classmethod
    def drain_status(cls) -> dict[str, Any]:
        return {"draining": state.draining, "active_run_count": cls.active_run_count()}

    @staticmethod
    def require_admission_open() -> None:
        if state.draining:
            raise ServerDraining

    @classmethod
    def _find_duplicate_submission(
        cls,
        goals: list[str],
        session_id: str | None,
        device_serial: str | None,
        endpoint: AdbEndpoint,
        now: float,
        host_id: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the short-circuit response for a duplicate submission, if any."""
        # 1. Deduplication by session_id: if session_id is already running or queued, do not re-enqueue
        if session_id and len(goals) == 1:
            target_sid = str(session_id)
            if state.active_session_id and str(state.active_session_id) == target_sid:
                return {
                    "status": "started",
                    "tasks": [{"session_id": target_sid, "status": "running"}],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }
            existing_item = next(
                (
                    item
                    for item in state.queue_items
                    if isinstance(item, dict) and str(item.get("session_id")) == target_sid
                ),
                None,
            )
            if existing_item:
                return {
                    "status": existing_item.get("status", "queued"),
                    "tasks": [existing_item],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }

        # 2. Debounce duplicate rapid submissions (e.g. UI double-click or network retry within 1s)
        if len(goals) == 1:
            first_goal = goals[0]
            recent_duplicate = next(
                (
                    item
                    for item in reversed(state.queue_items)
                    if isinstance(item, dict)
                    and item.get("status") == "pending"
                    and item.get("requested_by") == requested_by
                    and item.get("goal") == first_goal
                    and (not device_serial or item.get("device_serial") == device_serial)
                    and (
                        host_id is not None
                        or item.get("adb_endpoint", {}).get("identity") == endpoint.identity
                    )
                    and item.get("host_id") == host_id
                    and (now - float(item.get("created_at", 0))) < 1.0
                ),
                None,
            )
            if recent_duplicate:
                return {
                    "status": "queued",
                    "tasks": [recent_duplicate],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }
        return None

    @classmethod
    async def _reject_unavailable_device(
        cls, device_serial: str | None, endpoint: AdbEndpoint | None = None
    ) -> dict[str, Any] | None:
        """Return the rejection response for an unattached explicit serial, if any."""
        # Strict device binding: reject an explicitly requested serial that is not
        # attached and authorized, instead of silently running on another device.
        # The shared validator fails open on an indeterminate/empty enumeration so
        # the task can proceed and fail downstream with a clear no-device error.
        if device_serial:
            try:
                from artemis.runtime import device_pool

                pool = device_pool.pool_for(endpoint) if endpoint else device_pool
                rejection = await pool.validate_explicit_serial_async(device_serial)
            except Exception:
                rejection = None
            if rejection:
                return {
                    "status": "rejected",
                    "error": rejection,
                    "tasks": [],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }
        return None

    @classmethod
    def _create_queue_item(
        cls,
        goal: str,
        index: int,
        now: float,
        endpoint: AdbEndpoint,
        single_session_id: str | None,
        profile: str,
        expected_output: str | None,
        enable_outputter: bool | None,
        locked_app_package: str | None,
        app_path: str | None,
        device_serial: str | None,
        ingress: str,
        conversation_id: str | None,
        verification_level: str | None = None,
        explorer_mode: str | None = None,
        run_id: str | None = None,
        host_id: str | None = None,
        requested_by: str | None = None,
        bridge_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve a device slot and build one pending queue item for a goal."""
        sess_id = single_session_id if single_session_id else str(uuid.uuid4())
        # enqueue_tasks resolves the device before creating queue items.
        assigned_serial = device_serial
        try:
            from admin_console.services.bridge_session_service import bridge_session_service
        except ImportError:
            from apps.admin_console.services.bridge_session_service import bridge_session_service

        leases = [
            lease
            for lease in bridge_session_service.live_sessions()
            if not host_id
            and endpoint.is_local_default
            and lease.serial == assigned_serial
            and not lease.revoked
        ]
        # An explicit bridge id (validated at admission) picks its own lease among same-serial ones.
        lease = next(
            (lease for lease in leases if lease.session_id == bridge_session_id),
            leases[0] if leases else None,
        )
        binding = RunDeviceBinding(
            AdbTarget(endpoint, assigned_serial, host_id), lease.session_id if lease else None
        )

        queue_ticket = DeviceExecutionLock.reserve(
            description=f"{ingress} task: {goal_metadata(goal)}",
            device_id=assigned_serial or "pending",
            session_id=sess_id,
            ingress=ingress,
            lock_scope=AdbTarget(endpoint, assigned_serial, host_id).lock_scope,
        )
        return {
            "session_id": sess_id,
            "goal": goal,
            "profile": profile or "flash",
            "expected_output": expected_output,
            "enable_outputter": enable_outputter,
            "verification_level": verification_level,
            "explorer_mode": explorer_mode,
            "locked_app_package": locked_app_package,
            "app_path": app_path,
            "device_serial": assigned_serial,
            "adb_endpoint": endpoint.to_dict(),
            "device_binding": binding.to_dict(),
            "ingress": ingress,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "host_id": host_id,
            "requested_by": requested_by,
            # The bridge lease a browser-held phone's run was admitted under; None otherwise.
            "bridge_session_id": binding.bridge_session_id or bridge_session_id,
            "status": "pending",
            "queue_ticket": queue_ticket,
            "created_at": now + index * 0.001,
            "start_time": now + index * 0.001,
        }

    @classmethod
    async def enqueue_tasks(
        cls,
        goals: list[str],
        profile: str = "flash",
        expected_output: str | None = None,
        enable_outputter: bool | None = None,
        locked_app_package: str | None = None,
        app_path: str | None = None,
        device_serial: str | None = None,
        ingress: str = "frontend",
        session_id: str | None = None,
        conversation_id: str | None = None,
        verification_level: str | None = None,
        explorer_mode: str | None = None,
        run_id: str | None = None,
        host_id: str | None = None,
        requested_by: str | None = None,
        goal_images: list[run_images.ValidatedImage] | None = None,
        bridge_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Enqueues one or more goals and wakes up the background worker.

        ``goal_images`` are validated pictures for the single goal; they are stored
        with the run and the queue item names them (never their server paths).

        ``requested_by`` is the verified identity that owns the new runs (None:
        no owner); it is persisted with each session and shown on the queue item.

        ``host_id`` binds the run to a host-agent computer: it requires
        ``ARTEMIS_HOST_AGENT`` and an explicit ``device_serial`` (the opaque device
        ref), skips the local ADB checks, and waits on that host's admission.

        ``verification_level`` and ``explorer_mode`` are Pro-profile tuning knobs
        forwarded to the worker as ``--verification-level`` / ``--explorer-pro-mode``;
        they are normalised here so the queue item and the CLI see one spelling.

        ``run_id`` is the Gate 1 batch-grouping key (see
        ``artemis.config.attempt_lifecycle_hooks``); it is forwarded to the
        spawned worker as ``--run-id`` so daemon-dispatched attempts get the
        same manifest/reconciliation evidence as standalone runs.
        """
        if session_id:
            run_images.require_safe_session_id(str(session_id))  # before any side effect
        verification_level = (
            str(verification_level).strip().lower() or None if verification_level else None
        )
        explorer_mode = str(explorer_mode).strip().lower() or None if explorer_mode else None
        if host_id and not host_agent_enabled():
            raise ValueError("Host runs need ARTEMIS_HOST_AGENT to be enabled.")
        if host_id and not device_serial:
            raise ValueError("A host run must name its device (device_ref).")
        cls.ensure_worker_running()

        enqueued_tasks = []
        now = time.time()
        endpoint = current_adb_endpoint()

        duplicate_response = cls._find_duplicate_submission(
            goals,
            session_id,
            device_serial,
            endpoint,
            now,
            host_id=host_id,
            requested_by=requested_by,
        )
        if duplicate_response is not None:
            return duplicate_response

        if host_id:
            endpoint = host_endpoints.resolve(host_id)

        # Cheap early refusal; the authoritative check follows the last await.
        cls.require_admission_open()

        rejection_response = (
            None if host_id else await cls._reject_unavailable_device(device_serial, endpoint)
        )
        if rejection_response is not None:
            return rejection_response

        single_session_id = session_id if (session_id and len(goals) == 1) else None
        if not device_serial:
            # Device enumeration may block on ADB.
            from artemis.runtime import device_pool

            try:
                device_serial = await device_pool.pool_for(endpoint).select_device_async()
            except Exception:
                device_serial = None
        # Last await is behind us: from here to the queue append nothing yields,
        # so a drain enabled during the awaits above is seen before any session
        # or device reservation exists.
        cls.require_admission_open()
        if not device_serial:
            return {
                "status": "rejected",
                "error": "No device could be selected. Select a device before submitting a run.",
                "tasks": [],
                "enqueued_count": 0,
                "total_queued": len(state.queue_tasks),
            }
        for i, goal in enumerate(goals):
            task_item = cls._create_queue_item(
                goal,
                i,
                now,
                endpoint,
                single_session_id,
                profile,
                expected_output,
                enable_outputter,
                locked_app_package,
                app_path,
                device_serial,
                ingress,
                conversation_id,
                verification_level=verification_level,
                explorer_mode=explorer_mode,
                run_id=run_id,
                host_id=host_id,
                requested_by=requested_by,
                bridge_session_id=bridge_session_id,
            )
            session_id = str(task_item["session_id"])
            existing_trace = trace_store.read_status(session_id)
            trace_created = existing_trace is None
            existing_trace_is_terminal = bool(
                existing_trace
                and existing_trace.get("status") in {"completed", "failed", "cancelled", "success"}
            )
            try:
                if existing_trace_is_terminal:
                    raise RuntimeError(f"Session {session_id} already has a terminal trace")
                if trace_created:
                    trace_store.init_trace(
                        session_id,
                        goal,
                        task_item["profile"],
                        task_item.get("conversation_id"),
                        task_item.get("device_serial"),
                    )
                notify_context = (
                    {
                        "conversation_id": task_item.get("conversation_id"),
                        "ingress": task_item.get("ingress"),
                        "goal": goal,
                    }
                    if task_item.get("conversation_id") or task_item.get("ingress") == "mcp"
                    else None
                )
                if not session_repo.create_queued_session(
                    session_id,
                    goal,
                    task_item["profile"],
                    task_item.get("device_serial"),
                    task_item.get("start_time"),
                    notify_context,
                    requested_by,
                    device_binding=task_item["device_binding"],
                ):
                    raise RuntimeError(f"Could not persist queued session {session_id}")
                if goal_images:
                    # Synchronous on purpose: nothing may yield before the queue append.
                    task_item["goal_images"] = run_images.store(session_id, goal_images)
            except (OSError, RuntimeError) as exc:
                DeviceExecutionLock.cancel_reservation(task_item.get("queue_ticket"))
                if trace_created or (
                    not existing_trace_is_terminal
                    and session_repo.get_session_by_id(session_id) is None
                ):
                    try:
                        session_repo.lifecycle.finish(session_id, "failed", error=str(exc))
                    except (OSError, sqlite3.Error):
                        logger.exception(
                            "Could not mark queue setup failure for session %s", session_id
                        )
                for enqueued_task in enqueued_tasks:
                    enqueued_session_id = str(enqueued_task["session_id"])
                    session_repo.update_session_status(
                        enqueued_session_id,
                        "failed",
                        time.time(),
                        error="Task batch could not be queued.",
                    )
                    cls._remove_task(enqueued_session_id)
                raise
            state.queue_items.append(task_item)
            if host_id:
                from apps.admin_console.services.host_tunnel import host_tunnels

                host_tunnels.bind_run(host_id, session_id)
            enqueued_tasks.append(task_item)
            cls._broadcast_startup_progress(
                task_item["session_id"], "queued", "Task received and queued"
            )

        # Wake worker immediately
        state.wake_event.set()

        return {
            "status": "queued" if state.is_running else "started",
            "tasks": enqueued_tasks,
            "enqueued_count": len(goals),
            "total_queued": len(state.queue_tasks),
        }

    @staticmethod
    def clear_pause_marker() -> bool:
        """Remove the shared pause marker; True if one was removed.

        The one place the console deletes it. The marker is global, so callers
        outside the service must first prove the requester may resume every run
        it affects (``routers.tasks._pause_authority``); stop paths take
        ``clear_pause=False`` when they cannot.
        """
        if not PAUSE_FILE.exists():
            return False
        try:
            PAUSE_FILE.unlink()
        except OSError:
            # A leftover marker only pauses until the next resume request.
            return False
        return True

    @classmethod
    def _terminate_all_device_owners(cls) -> None:
        """Terminate every device lock owner and persist each cancellation."""
        active_owners: dict[str, Any] = {}
        try:
            active_owners = DeviceExecutionLock.get_active_owners()
        except OSError as exc:
            # Unreadable lock dir: fall back to the single-owner probe below.
            print(f"[stop_tasks] Could not enumerate device owners: {exc}")
        fallback = DeviceExecutionLock.get_active_owner()
        if fallback and not active_owners:
            active_owners["default"] = fallback

        for dev_owner in list(active_owners.values()):
            if dev_owner and DeviceExecutionLock.is_active_owner(dev_owner):
                cls._stop_worker_gracefully(
                    dev_owner.pid,
                    dev_owner.process_created_at,
                    session_id=dev_owner.session_id,
                )
                DeviceExecutionLock.cleanup_stale_locks(dev_owner.device_id)
                sid = dev_owner.session_id
                if sid:
                    session_repo.update_session_status(
                        str(sid), "cancelled", time.time(), error=_STOPPED_FROM_FRONTEND
                    )
                    cls._deliver_outcome(sid)

    @classmethod
    def _kill_all_local_runs(cls) -> None:
        """Mark every locally-managed run manually stopped and kill its process."""
        for run_key, run in list(state.active_runs.items()):
            # Mark each run individually: the global "stopped manually"
            # boolean was shared process-wide and polluted the terminal
            # status of unrelated concurrent runs.
            state.manually_stopped_run_ids.add(str(run_key))
            # Also track it per session so each finalizer resolves its
            # terminal status as cancelled.
            state.cancelled_session_ids.add(str(run_key))
            run_proc = run.get("process")
            if run_proc is not None and run_proc.returncode is None:
                if not cls._stop_proc_gracefully(run_proc, run.get("session_id") or run_key):
                    try:
                        run_proc.kill()
                    except (ProcessLookupError, OSError):
                        # Process already exited between the check and the kill.
                        pass
        # active_runs entries are popped by each run's finalizer once the
        # process exit is observed; clearing them here would free the device
        # slots before the processes are actually gone.
        if state.current_process:
            if not cls._stop_proc_gracefully(state.current_process, state.active_session_id):
                try:
                    state.current_process.kill()
                except (ProcessLookupError, OSError):
                    # Process already exited between the check and the kill.
                    pass
            state.current_process = None

    @classmethod
    def _stop_all_tasks(cls) -> bool:
        """Terminate all active device owners and clear pending queue submissions."""
        # 1. Cancel local queue reservations
        for item in state.queue_items:
            if isinstance(item, dict) and item.get("status") not in IN_FLIGHT_STATUSES:
                DeviceExecutionLock.cancel_reservation(item.get("queue_ticket"))
        state.clear_queue()

        # 2. Terminate all active owners across all devices
        cls._terminate_all_device_owners()
        cls._kill_all_local_runs()

        state.active_connections.clear()
        state.active_session_id = None
        state.current_goal = None
        state.current_profile = None

        cls.clear_pause_marker()  # every run was just terminated: nothing left to pause

        cls.ensure_worker_running()
        state.wake_event.set()
        return True

    @classmethod
    def _resolve_stop_owner(
        cls,
        active_owners: dict[str, Any],
        target_sid: str | None,
        target_device: str | None,
    ) -> Any:
        """Resolve the device lock owner targeted by this stop request, if any."""
        owner = None
        if target_sid:
            for dev_owner in active_owners.values():
                if dev_owner.session_id and str(dev_owner.session_id) == target_sid:
                    owner = dev_owner
                    break
        if owner is None and target_device:
            clean_target_dev = DeviceExecutionLock._normalize_device_id(target_device)
            for dev_key, dev_owner in active_owners.items():
                if dev_key == clean_target_dev or dev_owner.device_id == target_device:
                    owner = dev_owner
                    break
        if owner is None:
            # Covers scoped/legacy lock records that get_active_owners cannot
            # key by session or device. A stale owner for a different session
            # is never adopted: with no live owner, a session-targeted stop
            # falls through to the queue/session-repository fallbacks below.
            fallback_owner = DeviceExecutionLock.get_active_owner(target_device)
            if fallback_owner and (
                not target_sid
                or (fallback_owner.session_id and str(fallback_owner.session_id) == target_sid)
            ):
                owner = fallback_owner
        return owner

    @classmethod
    def _resolve_local_run(
        cls, target_sid: str | None, target_device: str | None
    ) -> tuple[str | None, dict[str, Any] | None, Any]:
        """Resolve the locally-managed run for this target.

        Concurrent scheduling keeps one subprocess per run in
        state.active_runs. Returns ``(local_run_key, local_run, local_proc)``.
        """
        local_run = None
        local_run_key: str | None = None
        if target_sid:
            local_run = state.active_runs.get(target_sid)
            if local_run is not None:
                local_run_key = target_sid
        elif target_device:
            local_run_key, local_run = next(
                (
                    (key, run)
                    for key, run in state.active_runs.items()
                    if run.get("device_id") and str(run["device_id"]) == target_device
                ),
                (None, None),
            )
        elif len(state.active_runs) == 1:
            # Untargeted stop (legacy single-device gesture): with exactly one
            # live run the target is unambiguous.
            local_run_key, local_run = next(iter(state.active_runs.items()))
        local_proc = (local_run or {}).get("process")
        if local_proc is None and not target_sid and not target_device:
            # Untargeted legacy fallback only: the last-started process may
            # stand in when no per-run entry resolved. An explicitly targeted
            # stop that matched no run must never grab current_process -- it
            # mirrors the newest run, which can be an unrelated concurrent one.
            local_proc = state.current_process
        if local_run is None and local_proc is not None:
            # Legacy path: current_process without a resolved run entry. Find
            # the run that owns this process so a manual stop can be attributed
            # to it instead of to the whole scheduler.
            local_run_key = next(
                (
                    str(key)
                    for key, run in state.active_runs.items()
                    if run.get("process") is local_proc
                ),
                None,
            )
        return local_run_key, local_run, local_proc

    @classmethod
    def _find_target_queue_item(cls, target_sid: str | None) -> dict[str, Any] | None:
        """Find the running (preferred) or pending queue item for this target."""
        running_item = next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict)
                and item.get("status") in IN_FLIGHT_STATUSES
                and (not target_sid or str(item.get("session_id")) == target_sid)
            ),
            None,
        )
        return running_item or next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict)
                and item.get("status") == "pending"
                and (not target_sid or str(item.get("session_id")) == target_sid)
            ),
            None,
        )

    @classmethod
    def _resolve_stopped_session_id(
        cls,
        owner: Any,
        target_sid: str | None,
        local_run_key: str | None,
        is_local_owner: bool,
        local_item: dict[str, Any] | None,
    ) -> Any:
        """Attribute the stop request to a session id, if one can be resolved."""
        # active_session_id mirrors the most recently launched run, so with
        # concurrent runs the resolved local run key (== its session id) must
        # take precedence to avoid attributing the stop to an unrelated run.
        return (
            owner.session_id
            if owner and owner.session_id
            else target_sid
            if target_sid
            else local_run_key
            if local_run_key is not None
            else state.active_session_id
            if is_local_owner
            else local_item.get("session_id")
            if local_item
            else None
        )

    @classmethod
    def _terminate_stop_target(
        cls,
        owner: Any,
        owner_record_exists: bool,
        target_sid: str | None,
        local_run: dict[str, Any] | None,
        local_run_key: str | None,
        local_proc: Any,
        local_pid: Any,
        local_item: dict[str, Any] | None,
        is_local_owner: bool,
    ) -> tuple[bool, bool, bool] | None:
        """Terminate the resolved stop target.

        Returns ``(stopped, is_local_owner, reservation_cancelled)``, or
        ``None`` when the caller must refuse the stop outright.
        """
        stopped = False
        reservation_cancelled = False
        if owner and DeviceExecutionLock.is_active_owner(owner):
            stopped, _deferred = cls._stop_worker_gracefully(
                owner.pid,
                owner.process_created_at,
                session_id=owner.session_id or target_sid,
            )
            DeviceExecutionLock.cleanup_stale_locks(owner.device_id)
        elif owner is None and owner_record_exists and not target_sid:
            # Never fall back to a frontend PID while another process has an
            # owner record that is still being published or cannot be parsed.
            return None
        elif (
            owner is None
            and local_proc is not None
            and (
                local_run is not None
                or not target_sid
                or str(state.active_session_id) == target_sid
            )
        ):
            # The locally-managed worker can be stopped during its short
            # initialization window before Agent acquires the device lease.
            if target_sid:
                state.cancelled_session_ids.add(target_sid)
            elif local_run_key is not None:
                state.manually_stopped_run_ids.add(str(local_run_key))
            deferred = False
            if local_pid:
                try:
                    stopped, deferred = cls._stop_worker_gracefully(
                        local_pid,
                        session_id=target_sid or (local_run or {}).get("session_id"),
                    )
                except Exception:
                    stopped = False
            if not deferred:
                try:
                    local_proc.kill()
                    stopped = True
                except (ProcessLookupError, OSError):
                    # Process already exited; the stop above may have got it.
                    pass
            stopped = True
            is_local_owner = True
        elif (
            owner is None
            and local_item
            and (not target_sid or str(local_item.get("session_id")) == target_sid)
        ):
            # Cancel a frontend submission before its worker has started. This
            # does not touch pending reservations created by other ingresses.
            DeviceExecutionLock.cancel_reservation(local_item.get("queue_ticket"))
            reservation_cancelled = True
            stopped = True
            is_local_owner = True
        elif target_sid:
            # Fallback 1: check global device queue
            global_queued = DeviceExecutionLock.get_queued_tasks()
            for q_item in global_queued:
                if str(q_item.get("session_id")) == target_sid:
                    # get_queued_tasks exposes the reservation as "token".
                    stopped = DeviceExecutionLock.cancel_reservation(q_item.get("token"))
                    break
            # Fallback 2: check session repository for a running session with a live worker PID
            if not stopped:
                row = session_repo.get_session_by_id(target_sid)
                if row and row.get("status") == "running":
                    row_pid = row.get("pid")
                    if row_pid and session_repo.process_is_alive(row_pid):
                        try:
                            cls._stop_worker_gracefully(int(row_pid), session_id=target_sid)
                        except Exception as exc:
                            # Report it: a surviving worker keeps the device busy.
                            print(f"[stop_tasks] Could not terminate worker pid {row_pid}: {exc}")
                    DeviceExecutionLock.cleanup_stale_locks()
                    stopped = True
        return stopped, is_local_owner, reservation_cancelled

    @classmethod
    def _finalize_targeted_stop(
        cls,
        stopped_session_id: Any,
        owner: Any,
        owner_pid: Any,
        is_local_owner: bool,
        local_run_key: str | None,
        local_proc: Any,
    ) -> None:
        """Propagate the stop into shared state, the DB, and event subscribers."""
        if is_local_owner:
            if stopped_session_id:
                # Per-session cancellation: the run's own finalizer resolves the
                # terminal status without affecting other concurrent runs.
                state.cancelled_session_ids.add(str(stopped_session_id))
            elif local_run_key is not None:
                state.manually_stopped_run_ids.add(str(local_run_key))
            if local_proc is not None and state.current_process is local_proc:
                state.current_process = None

        for sid, conn_info in list(state.active_connections.items()):
            if (stopped_session_id and str(sid) == str(stopped_session_id)) or (
                owner_pid and conn_info.get("pid") == owner_pid
            ):
                state.active_connections.pop(sid, None)

        if stopped_session_id:
            session_repo.update_session_status(
                str(stopped_session_id), "cancelled", time.time(), error=_STOPPED_FROM_FRONTEND
            )
            cls._deliver_outcome(stopped_session_id)

        if state.active_session_id and (
            not stopped_session_id or str(state.active_session_id) == str(stopped_session_id)
        ):
            state.active_session_id = None
            state.current_goal = None
            state.current_profile = None

    @classmethod
    def _remove_stopped_queue_item(
        cls, stopped_session_id: Any, reservation_cancelled: bool
    ) -> None:
        """Drop the stopped session's queue item and cancel its reservation."""
        if not stopped_session_id:
            return
        stopped_item = next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict) and str(item.get("session_id")) == str(stopped_session_id)
            ),
            None,
        )
        if stopped_item and not reservation_cancelled:
            DeviceExecutionLock.cancel_reservation(stopped_item.get("queue_ticket"))
        state.queue_items = [
            item
            for item in state.queue_items
            if not (
                isinstance(item, dict) and str(item.get("session_id")) == str(stopped_session_id)
            )
        ]

    @classmethod
    def active_session_ids(cls, running_only: bool = False) -> set[str | None]:
        """Every run that is queued or running, in this process or any other.

        Covers the in-process queue and workers plus live device-lock owners
        (other processes: CLI, SDK, MCP) and, unless ``running_only``, their
        global queue tickets. A ``None`` member is a lock record that names no
        session, or is still being published: a run that cannot be attributed.
        """
        wanted = IN_FLIGHT_STATUSES if running_only else IN_FLIGHT_STATUSES | {"pending"}
        ids: set[str | None] = {
            str(item["session_id"])
            for item in state.queue_items
            if isinstance(item, dict) and item.get("session_id") and item.get("status") in wanted
        }
        ids.update(str(sid) for sid in state.active_runs)
        if state.active_session_id:
            ids.add(str(state.active_session_id))
        try:
            owners = list(DeviceExecutionLock.get_active_owners().values())
            fallback = DeviceExecutionLock.get_active_owner()  # scoped/legacy records
            queued = [] if running_only else DeviceExecutionLock.get_queued_tasks()
            # get_active_owners skips unreadable files, so a readable owner cannot
            # vouch for the whole directory: ask about unreadable records too.
            record_unreadable = DeviceExecutionLock.has_unreadable_owner_record()
        except OSError:
            owners, fallback, queued, record_unreadable = [], None, [], True
        if fallback is not None:
            owners.append(fallback)
        ids.update(str(owner.session_id) if owner.session_id else None for owner in owners)
        ids.update(str(t["session_id"]) for t in queued if t.get("session_id"))
        if record_unreadable:
            ids.add(None)
        return ids

    @classmethod
    def sessions_on_device(cls, device_id: str) -> set[str | None]:
        """Runs a device-targeted stop would reach, via the stop resolver itself.

        A ``None`` entry is a lock record that names no session: unattributable.
        """
        ids: set[str | None] = {
            str(sid) for sid, run in state.active_runs.items() if run.get("device_id") == device_id
        }
        ids.update(
            str(item["session_id"])
            for item in state.queue_items
            if isinstance(item, dict)
            and item.get("session_id")
            and item.get("device_serial") == device_id
        )
        try:
            owners = DeviceExecutionLock.get_active_owners()
        except OSError:
            owners = {}
        owner = cls._resolve_stop_owner(owners, None, device_id)
        if owner is not None:
            ids.add(str(owner.session_id) if owner.session_id else None)
        elif DeviceExecutionLock.has_owner_record(device_id):
            ids.add(None)
        return ids

    @classmethod
    def _stop_targeted_task(
        cls, target_sid: str | None, target_device: str | None, clear_pause: bool = True
    ) -> bool:
        """Stop a specific task (or default single-device active task)."""
        active_owners = {}
        try:
            active_owners = DeviceExecutionLock.get_active_owners()
        except OSError as exc:
            # Unreadable lock dir: the local-process fallbacks below still apply.
            print(f"[stop_tasks] Could not enumerate device owners: {exc}")

        owner = cls._resolve_stop_owner(active_owners, target_sid, target_device)

        owner_record_exists = DeviceExecutionLock.has_owner_record(target_device)
        owner_pid = owner.pid if owner else None

        local_run_key, local_run, local_proc = cls._resolve_local_run(target_sid, target_device)
        local_pid = getattr(local_proc, "pid", None)
        is_local_owner = bool(owner_pid and local_pid and owner_pid == local_pid)

        local_item = cls._find_target_queue_item(target_sid)
        stopped_session_id = cls._resolve_stopped_session_id(
            owner, target_sid, local_run_key, is_local_owner, local_item
        )

        outcome = cls._terminate_stop_target(
            owner,
            owner_record_exists,
            target_sid,
            local_run,
            local_run_key,
            local_proc,
            local_pid,
            local_item,
            is_local_owner,
        )
        if outcome is None:
            return False
        stopped, is_local_owner, reservation_cancelled = outcome

        if not stopped and not target_sid:
            return False

        cls._finalize_targeted_stop(
            stopped_session_id,
            owner,
            owner_pid,
            is_local_owner,
            local_run_key,
            local_proc,
        )
        cls._remove_stopped_queue_item(stopped_session_id, reservation_cancelled)

        if clear_pause:
            cls.clear_pause_marker()

        cls.ensure_worker_running()
        state.wake_event.set()
        return True

    @classmethod
    def cancel_queued(cls, session_id: str) -> str:
        """Cancel a run only while it waits; never stops one that has started.

        Returns ``cancelled``, ``already_started``, ``not_found`` or ``retry`` (the
        session could not be read or the cancellation not persisted; nothing
        changed). Only a session confirmed absent is cancelled in memory. Runs on
        the event loop without yielding, so it is ordered against dispatch.
        """
        sid = str(session_id)
        item = cls._queue_item_for(sid)
        try:
            row = session_repo.read_session(sid)
        except (sqlite3.Error, OSError):
            logger.exception("[QueueWorker] Could not read session %s to cancel it", sid)
            return "retry"  # a failed read is not proof the session is absent
        waiting = (item and item.get("status") == "pending") or (
            not item and row and row.get("status") == "queued"
        )
        if not waiting:
            started = item or sid in state.active_runs or sid in state.executing_run_keys or row
            return "already_started" if started else "not_found"
        settled = session_repo.update_session_status(
            sid, "cancelled", time.time(), error=_CANCELLED_WHILE_QUEUED
        )
        if not settled and row:
            # Not ours to claim: another writer settled it, or the commit failed.
            try:
                now = session_repo.read_session(sid)
            except (sqlite3.Error, OSError):
                logger.exception("[QueueWorker] Could not re-read session %s", sid)
                return "retry"
            if now and now.get("status") != "queued":
                return "already_started"
            return "retry"  # still queued and persisted as such: nothing was cancelled
        cls._deliver_outcome(sid)
        cls._remove_task(sid)  # also cancels the queue ticket, keeping the others' order
        state.wake_event.set()
        return "cancelled"

    @classmethod
    async def requeue_starting(cls, session_id: str) -> bool:
        """The host agent NACKed a start that raced its barrier: wait again, in place.

        Honored only while no worker process exists for the run and terminal
        settlement has not started: the run is then cancelled before spawn, the
        row goes back to ``pending`` at its list position, its ticket (never
        handed to a worker) keeps its original timestamp and its session stays
        queued. Once a worker was, or may have been, spawned, once settlement
        owns the outcome, or when the state is unknown, the NACK is refused and
        nothing is killed: a worker may already hold the device, and stopping a
        spawned worker belongs to the host agent protocol (B2/B3a), where the
        agent is authoritative for its own processes. False when refused.
        """
        sid = str(session_id)
        item = cls._queue_item_for(sid)
        if (
            item.get("status") != "starting"
            or item.get("worker_spawned")
            or item.get("settling")
            or not item.get("queue_ticket")
            or sid in state.active_runs
            or item not in state.queue_items
        ):
            return False
        # No await from the decision to the cancel: the run cannot move meanwhile.
        item["requeue"] = True
        run_task = cls._run_tasks_by_session.get(sid)
        if run_task is not None:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        host_admission.release(sid)
        item.pop("requeue", None)
        if item not in state.queue_items:  # stopped while we waited
            return True
        item["status"] = "pending"
        state.wake_event.set()
        return True

    @classmethod
    def stop_tasks(
        cls,
        clear_all: bool = False,
        session_id: str | None = None,
        device_id: str | None = None,
        clear_pause: bool = True,
    ) -> bool:
        """Stop the active task controlling a mobile device or all tasks.

        ``clear_pause=False`` leaves the global pause marker alone (a targeted
        stop by a caller who may not resume the other runs it covers).

        The active lease is shared by frontend, MCP, CLI, SDK, and other UI
        processes across all connected devices.
        - If ``session_id`` or ``device_id`` is specified, only the corresponding
          running or queued task is cancelled.
        - If ``clear_all`` is requested, all active device owners are terminated
          and pending queue submissions are cleared.
        - If no specific task is specified and ``clear_all`` is False, stops the
          currently active task in single-device mode for backward compatibility.
        """
        target_sid = str(session_id).strip() if session_id else None
        target_device = str(device_id).strip() if device_id else None

        if clear_all:
            return cls._stop_all_tasks()

        return cls._stop_targeted_task(target_sid, target_device, clear_pause)

    @classmethod
    def resume_task(cls) -> bool:
        return cls.clear_pause_marker()

    @classmethod
    def recover_orphaned_recordings_on_launch(cls) -> int:
        """Finalize recordings left behind by workers that died with the daemon.

        The per-run finalizer handles workers that exit while the daemon is up;
        this sweep covers everything else (daemon crash, machine reboot, tasks
        cancelled before this recovery path existed). Returns the number of
        recordings published.
        """
        recovered = 0
        try:
            pending = session_repo.get_unfinalized_video_recordings()
        except Exception as exc:
            print(f"[ServerStartup] Could not enumerate unfinalized recordings: {exc}")
            return 0
        for row in pending:
            sess_id = row.get("session_id")
            try:
                final_path = media_service.recover_orphaned_recording(
                    row.get("local_video_path"),
                    row.get("start_time") or row.get("session_start_time"),
                )
            except Exception as exc:
                print(f"[ServerStartup] Recording recovery failed for {sess_id}: {exc}")
                continue
            if not final_path:
                continue
            if session_repo.mark_recording_ready(str(sess_id), str(final_path)):
                recovered += 1
                print(f"[ServerStartup] Recovered recording for session {sess_id}: {final_path}")
        if recovered:
            print(f"[ServerStartup] Recovered {recovered} orphaned recording(s).")
        return recovered

    @staticmethod
    def archive_older_replays_on_launch():
        """Archives all existing step replay output folders on server launch."""
        if TEST_OUTPUTS_DIR.exists():
            archive_dir = TEST_DATA_DIR / "older"
            for item in TEST_OUTPUTS_DIR.iterdir():
                if item.is_dir():
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    target_name = f"{item.name}_{timestamp}"
                    target_path = archive_dir / target_name

                    counter = 1
                    while target_path.exists():
                        target_name = f"{item.name}_{timestamp}_{counter}"
                        target_path = archive_dir / target_name
                        counter += 1

                    print(
                        f"Archiving older replay record on server launch: "
                        f"{item.name} -> {target_path}"
                    )
                    try:
                        shutil.move(str(item), str(target_path))
                    except Exception as e:
                        print(
                            f"Warning: Failed to archive {item.name} during server launch: {e}",
                            file=sys.stderr,
                        )

    @staticmethod
    def verify_chunks_exist_on_launch(replay_manager):
        """Verifies that chunked directories exist for all sessions in the database."""
        print("Verifying session chunks on launch...")
        try:
            sessions = session_repo.get_all_sessions()
            session_ids = [s.get("session_id") for s in sessions if s.get("session_id")]
            print(f"Found {len(session_ids)} sessions in database to verify.")
            for session_id in session_ids:
                replay_manager._ensure_session_chunked(str(session_id))
        except Exception as e:
            print(
                f"Warning: Failed to verify chunks during server launch: {e}",
                file=sys.stderr,
            )


task_queue_service = TaskQueueService()
