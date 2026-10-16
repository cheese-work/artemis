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

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.outputter.outputter import outputter
from artemis.clients.screen_client_factory import create_screen_client
from artemis.config import OutputConfig, record_events
from artemis.constants import RECURSION_LIMIT
from artemis.context import DeviceContext
from artemis.sdk.builders.task_request_builder import TaskRequestBuilder
import asyncio
import inspect
import os
import re

from datetime import UTC, datetime
from platform import system
import shutil
import sqlite3
import sys
import threading
from types import NoneType
from typing import Any, TypeVar, overload
import uuid
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from langchain_google_genai import ChatGoogleGenerativeAI

from artemis.clients.screen_client_factory import (
    describe_backend,
    helper_version,
    hierarchy_backend_sentence,
)
from artemis.config import (
    CheckerConfig,
    run_tuning_for_profile,
    settings,
)
from artemis.context import (
    ArtemisContext,
    DevicePlatform,
    ExecutionSetup,
)
from artemis.controllers.controller_factory import get_controller
from artemis.controllers.platform_specific_commands_controller import (
    get_first_device,
)
from artemis.runtime import DeviceExecutionLock, trace_store
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.endpoint_transport import EndpointTransport
from artemis.runtime.cancel_requests import watch_for_cancel_request
from artemis.runtime.lifecycle import InterruptReason
from artemis.llm.reliability import LLMGatewayModelRejectedError, LLMSelectedModelError
from artemis.data_engine import run_snapshot
from artemis.data_engine.engine import DataEngine
from artemis.drivers.types import DeviceDisconnectedError
from artemis.data_engine.trace import DataEngineCallbackHandler
from artemis.graph.graph import get_graph
from artemis.graph.state import State
from artemis.sdk.types.agent import AgentConfig
from artemis.sdk.types.exceptions import (
    AgentError,
    AgentNotInitializedError,
    AgentProfileNotFoundError,
    AgentTaskRequestError,
    DeviceNotFoundError,
    ExecutableNotFoundError,
)
from artemis.sdk.types.task import (
    AgentProfile,
    Task,
    TaskRequest,
)
from artemis.utils.redaction import bind_session, goal_metadata
from artemis.utils.media import (
    create_gif_from_trace_folder,
    create_steps_json_from_trace_folder,
    remove_images_from_trace_folder,
    remove_steps_json_from_trace_folder,
)
from artemis.utils.startup_progress import publish_startup_progress
from artemis.utils.video import SCRCPY_TOO_OLD_REASON, classify_recording_failure
from third_party.mobile_use.sdk.agent import AgentBase, TOutput
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

load_dotenv()


__all__ = ["Agent", "TOutput", "attach_test_summary", "resolve_trace_suffix", "run_tuning_summary"]


def run_tuning_summary(config: AgentConfig, profile: str | None) -> dict[str, str] | None:
    """Per-run Pro tuning (verification level, explorer mode) for an SDK config.

    The SDK config carries the Checker switches as flat flags; fold them back
    into a :class:`CheckerConfig` so the ladder classification stays single-
    sourced in ``artemis.config``. Returns ``None`` for Flash runs.
    """
    return run_tuning_for_profile(
        profile,
        checker=CheckerConfig(
            enabled=not config.disable_checker,
            midway_checks=not config.disable_midway_checks,
            final_check=not config.disable_final_check,
            assert_failure_policy=config.assert_failure_policy,
        ),
        explorer=config.explorer,
        explorer_versions=dict(config.explorer_versions or {}),
    )


class Agent(AgentBase):
    """Artemis SDK agent."""

    def __init__(
        self,
        *,
        config: AgentConfig | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        concurrency_mode: str | None = None,
        max_concurrency: int | None = None,
        session_id: str | None = None,
    ):
        raw_sid = (
            session_id or os.getenv("ARTEMIS_SESSION_ID") or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
        )
        self._session_id: str | None = str(raw_sid).strip() if raw_sid else None
        target_dev = device_serial or device_id
        if config is None:
            from artemis.sdk.builders import Builders

            builder = Builders.AgentConfig
            if target_dev:
                builder.for_device(DevicePlatform.ANDROID, target_dev)
            if concurrency_mode:
                builder.with_concurrency_mode(concurrency_mode)
            if max_concurrency is not None:
                builder.with_max_concurrency(max_concurrency)
            self._config = builder.build()
        else:
            self._config = config
            updates = {}
            if target_dev:
                updates["device_id"] = target_dev
                updates["device_platform"] = DevicePlatform.ANDROID
            if concurrency_mode:
                updates["concurrency_mode"] = str(concurrency_mode).strip().lower()
            if max_concurrency is not None:
                updates["max_concurrency"] = max_concurrency
            if updates:
                self._config = self._config.model_copy(update=updates)

        self._tasks = []
        self._tmp_traces_dir = Path(settings.TRACES_PATH)
        self._initialized = False
        self._task_lock = asyncio.Lock()

    async def init(
        self,
        api_key: str | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        retry_count: int = 5,
        retry_wait_seconds: int = 5,
    ):
        target_dev = device_serial or device_id
        if target_dev:
            self._config = self._config.model_copy(
                update={"device_id": target_dev, "device_platform": DevicePlatform.ANDROID}
            )

        return await self._init_internal(
            api_key=api_key,
            retry_count=retry_count,
            retry_wait_seconds=retry_wait_seconds,
        )

    async def _init_internal(
        self,
        api_key: str | None = None,
        retry_count: int = 5,
        retry_wait_seconds: int = 5,
    ):

        if os.environ.get("ARTEMIS_CLOUD_MODE") != "1" and not EndpointTransport.adb_binary():
            raise ExecutableNotFoundError("adb")

        if self._initialized:
            logger.warning("Agent is already initialized. Skipping...")
            return True

        # Get first available device ID
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            device_id = os.environ.get("ADB_DEVICE_SERIAL", "cloud_device")
            platform = DevicePlatform.ANDROID
        elif not self._config.device_id or not self._config.device_platform:
            device_id, platform, _ = get_first_device(logger=logger)
        else:
            device_id, platform = (
                self._config.device_id,
                self._config.device_platform,
            )

        if not device_id or not platform:
            error_msg = "No device found. Exiting."
            logger.error(error_msg)
            raise DeviceNotFoundError(error_msg)

        # Initialize clients
        publish_startup_progress(
            "device_check", "Checking the Android device", session_id=self._session_id
        )
        if os.environ.get("ARTEMIS_CLOUD_MODE") != "1":
            self._init_clients(
                device_id=device_id,
                platform=platform,
            )
        else:
            # Cloud mode is pinned to UIAutomator2 through the gateway; the
            # hierarchy backend switch only applies to locally attached devices.
            from cloud_service.virtualization import RemoteAdbClient, RemoteUIAutomatorClient

            self._adb_client = RemoteAdbClient()
            self._ui_adb_client = RemoteUIAutomatorClient(
                device_id=device_id,
                adb_client=self._adb_client,
            )

        self._device_context = await self._get_device_context(
            device_id=device_id, platform=platform
        )
        logger.info(self._device_context.to_str())
        publish_startup_progress(
            "device_ready", "Android device connected", session_id=self._session_id
        )

        # Asynchronously pre-warm LLM connection pools in the background
        asyncio.create_task(self._prewarm_llm_connections(api_key))

        logger.info("✅ Artemis agent initialized.")
        self._initialized = True

        return True

    async def _prewarm_llm_connections(self, api_key: str | None = None):
        """Pre-warms the HTTP2/gRPC connection pools for both Native GenAI and LangChain clients in the background."""
        if os.environ.get("ARTEMIS_FAKE_LLM") == "1":
            logger.info("ARTEMIS_FAKE_LLM=1 — skipping real LLM connection pre-warming.")
            publish_startup_progress(
                "model_ready", "Model connection is ready (fake LLM)", session_id=self._session_id
            )
            return
        publish_startup_progress(
            "model_warmup", "Warming the model connection", session_id=self._session_id
        )
        logger.info("Starting background pre-warming of Gemini API connection pools...")
        try:
            key = api_key
            if not key and settings.GOOGLE_API_KEY:
                key = settings.GOOGLE_API_KEY.get_secret_value()

            if not key:
                logger.warning("Skipping LLM pre-warming: No API key available.")
                publish_startup_progress(
                    "model_ready",
                    "Model connection will initialize on first use",
                    session_id=self._session_id,
                )
                return

            # 1. Pre-warm Native SDK client
            client = genai.Client(api_key=key)

            # 2. Pre-warm LangChain client
            chat = ChatGoogleGenerativeAI(model="gemini-3.8-flash", google_api_key=key)

            # Fire both calls concurrently in the background
            await asyncio.gather(
                client.aio.models.count_tokens(model="gemini-3.8-flash", contents="ping"),
                chat.ainvoke("ping"),
                return_exceptions=True,
            )
            logger.success("Gemini API connection pools successfully pre-warmed.")
            publish_startup_progress(
                "model_ready", "Model connection is ready", session_id=self._session_id
            )
        except Exception as e:
            logger.warning(f"Failed to pre-warm LLM connections: {e}")
            publish_startup_progress(
                "model_ready",
                "Model connection will initialize on first use",
                session_id=self._session_id,
            )

    async def install_apk(self, apk_path: str | Path) -> None:
        """Install an APK on the connected device.

        For cloud mobiles, the APK must be x86_64 compatible.

        Args:
            apk_path: Path to the local APK file to install

        Raises:
            AgentNotInitializedError: If the agent is not initialized
            AgentError: If attempting to install on non-Android device or ADB
            operations fail
            FileNotFoundError: If the APK file doesn't exist
            CloudMobileServiceUninitializedError: If cloud service is
            unavailable
        """
        await self._install_apk_internal(apk_path)

    async def _install_apk_internal(self, apk_path: str | Path) -> None:
        if isinstance(apk_path, str):
            apk_path = Path(apk_path)

        if not apk_path.exists():
            raise FileNotFoundError(f"APK file not found: {apk_path}")

        if not self._initialized:
            raise AgentNotInitializedError()

        device_id = self._device_context.device_id
        logger.info(f"Installing APK on Android device '{device_id}'")
        if not self._adb_client:
            raise AgentError("ADB client not initialized")

        device = self._adb_client.device(serial=device_id)
        await asyncio.to_thread(device.install, apk_path)
        logger.info(f"APK installed successfully on Android device '{device_id}'")

    async def install_app(self, app_path: str | Path) -> str | None:
        """Install an app on the connected device.

        For Android: Installs an APK file using ADB.

        Args:
            app_path: Path to the app to install (Android APK file).

        Returns:
            None.

        Raises:
            AgentNotInitializedError: If the agent is not initialized
            AgentError: If installation fails or platform is unsupported
            FileNotFoundError: If the app file/folder doesn't exist
        """
        return await self._install_app_internal(app_path)

    async def _install_app_internal(self, app_path: str | Path) -> str | None:
        if isinstance(app_path, str):
            app_path = Path(app_path)

        if not app_path.exists():
            raise FileNotFoundError(f"App not found: {app_path}")

        if not self._initialized:
            raise AgentNotInitializedError()

        await self._install_apk_internal(app_path)
        return None

    def new_task(self, goal: str):
        """Create a new task request builder.

        Args:
            goal: Natural language description of what to accomplish

        Returns:
            TaskRequestBuilder that can be configured with:
            - .with_output_format() for structured output
            - .with_output_description() for output description
            - .with_locked_app_package() to restrict execution to a specific app
            - .using_profile() to specify an LLM profile
            - .with_max_steps() to set maximum execution steps
            - .with_trace_recording() to enable trace recording
            - .with_name() to set a custom task name
        """
        return TaskRequestBuilder[None].from_common(
            goal=goal,
            common=self._config.task_request_defaults,
        )

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output: type[TOutput],
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> TOutput | None: ...

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output: str,
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | dict | None: ...

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output=None,
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | None: ...

    @overload
    async def run_task(
        self,
        *,
        request: TaskRequest[None],
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | dict | None: ...

    @overload
    async def run_task(
        self,
        *,
        request: TaskRequest[TOutput],
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> TOutput | None: ...

    async def run_task(
        self,
        *,
        goal: str | None = None,
        output: type[TOutput] | str | None = None,
        profile: str | AgentProfile | None = None,
        locked_app_package: str | None = None,
        name: str | None = None,
        app_path: str | Path | None = None,
        request: TaskRequest[TOutput] | None = None,
    ) -> str | dict | TOutput | None:

        # Normal local execution path
        if request is not None:
            if locked_app_package is not None:
                if request.locked_app_package:
                    logger.warning(
                        "Locked app package specified both in the request and"
                        " as a parameter. Using the parameter value."
                    )
                request.locked_app_package = locked_app_package
            # Handle app_path parameter override
            if app_path is not None:
                if request.app_path:
                    logger.warning(
                        "App path specified both in the request and as a"
                        " parameter. Using the parameter value."
                    )
                request.app_path = Path(app_path) if isinstance(app_path, str) else app_path
            return await self._run_task(request=request)
        if goal is None:
            raise AgentTaskRequestError("Goal is required")
        task_request = self.new_task(goal=goal)
        if output is not None:
            if isinstance(output, str):
                task_request.with_output_description(description=output)
            elif output is not NoneType:
                task_request.with_output_format(output_format=output)
        if profile is not None:
            task_request.using_profile(profile=profile)
        if name is not None:
            task_request.with_name(name=name)
        if locked_app_package is not None:
            task_request.with_locked_app_package(package_name=locked_app_package)
        if app_path is not None:
            task_request.with_app_path(app_path=app_path)
        return await self._run_task(task_request.build())

    @staticmethod
    def _report_recording_unavailable(error: str, session_id: str) -> None:
        reason = classify_recording_failure(error)
        detail = (
            f"{SCRCPY_TOO_OLD_REASON}."
            if SCRCPY_TOO_OLD_REASON in error
            else "scrcpy is incompatible with this phone's Android version."
            if reason == "recorder_incompatible"
            else (error.splitlines()[0].strip()[:200] if error else "")
            or "the recorder could not start."
        )
        publish_startup_progress(
            "recording_unavailable", f"No recording: {detail}", session_id=session_id, reason=reason
        )

    async def _watch_external_cancel(self, task_name: str) -> None:
        """Cancel the running task when another process drops a cancel marker.

        The admin console (and other ingresses) cannot deliver a signal to a
        worker portably, so they write a marker keyed by session id / pid.
        Reacting here routes the request through the same cancellation path
        as Ctrl+C: the recording is stopped and remuxed, the trace folder is
        compiled and renamed, and the device lease is released.
        """

        def _on_cancel() -> None:
            logger.warning(
                f"[{task_name}] External cancel request received; stopping the task gracefully."
            )
            self.stop_current_task()

        try:
            await watch_for_cancel_request(_on_cancel, session_id=self._session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[{task_name}] Cancel watcher stopped: {exc}")

    async def clean(self, force: bool = False):
        if not self._initialized and not force:
            return

        if self._ui_adb_client is not None:
            await asyncio.to_thread(self._ui_adb_client.disconnect)
        self._initialized = False
        logger.info("✅ Artemis agent stopped.")

    async def _ensure_device_unlocked(self) -> None:
        """Reject secure keyguard instead of allowing an agent to guess credentials."""
        if self._adb_client is None:
            raise AgentError("ADB client is not initialized.")

        device = self._adb_client.device(serial=self._device_context.device_id)
        try:
            trust_state = str(await asyncio.to_thread(device.shell, "dumpsys trust"))
        except Exception as exc:
            logger.warning(f"Could not inspect Android keyguard state: {exc}")
            return

        # The first deviceLocked value belongs to the current Android user;
        # later entries may describe a separately locked work profile.
        match = re.search(r"\bdeviceLocked=(?:true|1|false|0)\b", trust_state, re.IGNORECASE)
        if match is None:
            return
        value = match.group(0).split("=", 1)[1].lower()
        if value in {"true", "1"}:
            raise AgentError(
                "Android secure keyguard is locked. Unlock the device manually before "
                "running Artemis; automation will not guess a PIN, password, or pattern."
            )

    async def _prepare_device_environment(self, context: ArtemisContext):
        """Prepare device environment flags (like forcing Web Accessibility) before the task runs."""
        if not self._config.force_web_accessibility:
            logger.info(
                "Forcing web accessibility is disabled in AgentConfig. Skipping"
                " device environment prep..."
            )
            return

        if not self._adb_client or not self._device_context:
            logger.warning(
                "ADB client or device context not available. Skipping device environment prep..."
            )
            return

        device_id = self._device_context.device_id
        try:
            device = self._adb_client.device(serial=device_id)
            logger.info(f"[{device_id}] ⚙️ Configuring Chrome/WebView Web Accessibility flags...")

            # 1. Write the force accessibility command flag to Chrome command line
            await asyncio.to_thread(
                device.shell,
                "echo 'chrome --force-renderer-accessibility' >"
                " /data/local/tmp/chrome-command-line",
            )
            await asyncio.to_thread(device.shell, "chmod 555 /data/local/tmp/chrome-command-line")

            # 2. Write the same command line flag to System WebView config file
            await asyncio.to_thread(
                device.shell,
                "echo 'chrome --force-renderer-accessibility' >"
                " /data/local/tmp/webview-command-line",
            )
            await asyncio.to_thread(device.shell, "chmod 555 /data/local/tmp/webview-command-line")

            # 3. Force stop Chrome so the flag takes effect next time it launches
            await asyncio.to_thread(device.shell, "am force-stop com.android.chrome")
            logger.success(
                f"[{device_id}] ✅ Chrome Web Accessibility command line flags"
                " configured successfully."
            )
        except Exception as e:
            logger.warning(
                "Failed to configure Chrome Web Accessibility flags (needs"
                f" root/userdebug device): {e}"
            )

    def _prepare_tracing(self, task: Task, context: ArtemisContext):
        """Prepare tracing and data engine setup."""
        task_name = self._prepare_trace_paths(task)

        context.execution_setup = ExecutionSetup(
            traces_path=self._tmp_traces_dir,
            trace_name=task_name,
            enable_remote_tracing=task.request.enable_remote_tracing
            if task.request.record_trace
            else False,
            video_recording_tools_enabled=self._config.video_recording_tools_enabled,
            disable_checker=self._config.disable_checker,
            disable_midway_checks=self._config.disable_midway_checks,
            disable_final_check=self._config.disable_final_check,
            checker_max_iterations=self._config.checker_max_iterations,
            final_check_max_attempts=self._config.final_check_max_attempts,
            checkpoint_max_repairs=self._config.checkpoint_max_repairs,
            max_concurrent_checkpoints=self._config.max_concurrent_checkpoints,
            checkpoint_timeout=self._config.checkpoint_timeout,
            settlement_timeout=self._config.settlement_timeout,
            assert_failure_policy=self._config.assert_failure_policy,
            disable_device_probes=self._config.disable_device_probes,
            disable_planner_validation=self._config.disable_planner_validation,
            enable_committee=self._config.enable_committee,
            committee_debate_rounds=self._config.committee_debate_rounds,
            disable_outputter=self._config.disable_outputter,
            outputter=self._config.outputter,
            explorer=self._config.explorer,
            explorer_versions=self._config.explorer_versions,
        )

        context.data_engine = DataEngine(ctx=context)
        device_data = context.device.model_dump() if context.device else {}
        device_data.update(
            run_snapshot.execution_fields(
                context.adb_client, device_data.get("device_id"), context.llm_config
            )
        )
        if task.request.profile:
            device_data["profile"] = task.request.profile
        run_tuning = run_tuning_summary(self._config, task.request.profile)
        if run_tuning:
            device_data["run_tuning"] = run_tuning

        target_sid = (
            self._session_id
            or os.getenv("ARTEMIS_SESSION_ID")
            or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
            or getattr(task, "id", None)
            or getattr(getattr(task, "request", None), "task_name", None)
        )
        sess_uuid = None
        if target_sid:
            try:
                sess_uuid = uuid.UUID(str(target_sid))
            except Exception:
                sess_uuid = str(target_sid)

        context.data_engine.start_session(
            goal=task.request.goal,
            device_info=device_data,
            session_id=sess_uuid,
        )

    @staticmethod
    def _end_session_for_report(data_engine, report: dict, *, error: str | None = None) -> None:
        """End the session for a runner report that did not complete the task."""
        if report.get("status") == "interrupted":
            data_engine.end_session(
                "interrupted",
                interrupt_reason=report.get("interrupt_reason") or InterruptReason.DEVICE_OFFLINE,
                **({"error": error} if error is not None else {}),
            )
        else:
            data_engine.end_session("failed", **({"error": error} if error is not None else {}))

    async def _finalize_tracing_safely(self, task: Task, context: ArtemisContext):
        """Finalize optional trace artifacts without changing task semantics."""
        try:
            await self._finalize_tracing(task=task, context=context)
        except Exception as exc:
            logger.error(
                f"[{task.get_name()}] Trace artifact finalization failed after task status "
                f"was resolved as '{task.status}': {exc}",
                exc_info=True,
            )

    async def _generate_prompt_tip_safely(self, task: Task, context: ArtemisContext):
        if (
            context.data_engine
            and task.status == "completed"
            and os.getenv("ARTEMIS_TASK_WORKER") == "1"
            and not os.getenv("ARTEMIS_CHAT_SESSION_ID")
        ):
            from apps.admin_console.services.automatic_prompt_tips import ensure_tip

            results = await asyncio.gather(
                ensure_tip(str(context.data_engine.session_id)), return_exceptions=True
            )
            if isinstance(results[0], BaseException):
                logger.warning("Automatic prompt tip unavailable for a completed task.")

    def _prepare_output_files(self, task: Task):
        if task.request.llm_output_path:
            _validate_and_prepare_file(file_path=task.request.llm_output_path)

    def _get_graph_state(self, task: Task):
        return State.initial(task.request.goal)

    async def _connect_screen_client(self, context: ArtemisContext, session_id: str) -> None:
        """Connect the screen client with visible progress for slow first-time steps."""
        client = self._ui_adb_client
        previous_listener = getattr(self, "_hierarchy_backend_listener", None)
        if previous_listener is not None:
            previous_client, listener = previous_listener
            previous_client.remove_backend_listener(listener)
            self._hierarchy_backend_listener = None
        publish_startup_progress(
            "uiautomator", "Connecting to the UI hierarchy service", session_id=session_id
        )

        def on_provision(event: str, details: dict) -> None:
            version = details.get("version_name") or details.get("to_version")
            if event == "installing":
                publish_startup_progress(
                    "helper_install",
                    "Installing the Artemis accessibility helper on this device for the "
                    f"first time (v{version}, about 3 seconds)",
                    session_id=session_id,
                    **details,
                )
            elif event == "upgrading":
                publish_startup_progress(
                    "helper_upgrade",
                    "Upgrading the Artemis accessibility helper "
                    f"(v{details.get('from_version')} -> v{details.get('to_version')})",
                    session_id=session_id,
                    **details,
                )

        # Clients without provisioning (UIAutomator2, cloud) take no callback.
        try:
            accepts_events = "on_event" in inspect.signature(client.connect).parameters
        except (TypeError, ValueError):
            accepts_events = False
        if accepts_events:
            await asyncio.to_thread(client.connect, on_event=on_provision)
        else:
            await asyncio.to_thread(client.connect)

        backend = describe_backend(client) or "uiautomator"
        publish_startup_progress(
            "uiautomator_ready",
            f"UI hierarchy service is ready ({backend})",
            session_id=session_id,
        )
        self._announce_hierarchy_backend(context, session_id, None, backend, None)
        add_listener = getattr(client, "add_backend_listener", None)
        if callable(add_listener):

            def listener(previous, new, reason):
                self._announce_hierarchy_backend(context, session_id, previous, new, reason)

            add_listener(listener)
            self._hierarchy_backend_listener = (client, listener)

    def _announce_hierarchy_backend(
        self,
        context: ArtemisContext,
        session_id: str,
        previous: str | None,
        backend: str,
        reason: str | None,
    ) -> None:
        """One line everywhere a person or an agent looks for it.

        Startup-progress event (UI timeline), a named log trace, the session's
        device_info, and status.json for ``mobile_manage_task``. Runs from
        worker threads too, so every sink is best effort.
        """
        label = {"helper": "Artemis accessibility helper", "uiautomator": "UIAutomator2"}
        version = helper_version(self._ui_adb_client)
        if previous is None:
            message = f"UI hierarchy source: {label.get(backend, backend)}"
            if backend == "helper" and version:
                message += f" v{version}"
            stage = "hierarchy_backend"
        else:
            message = (
                f"UI hierarchy source switched from {label.get(previous, previous)} to "
                f"{label.get(backend, backend)}"
            )
            if reason:
                message += f" because {reason}"
            stage = "hierarchy_backend_changed"
        logger.info(message)
        try:
            publish_startup_progress(
                stage,
                message,
                session_id=session_id,
                backend=backend,
                previous_backend=previous,
                reason=reason,
                helper_version=version,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.debug(f"Could not publish hierarchy backend progress: {exc}")
        engine = getattr(context, "data_engine", None)
        if engine is not None and getattr(engine, "current_session_id", None):
            try:
                engine.record_trace(
                    type="log",
                    name="hierarchy_backend",
                    payload={
                        "message": message,
                        "backend": backend,
                        "previous_backend": previous,
                        "reason": reason,
                        "helper_version": version,
                        "level": "WARNING" if previous is not None else "INFO",
                    },
                )
                engine.update_session_device_info(
                    hierarchy_backend=backend,
                    hierarchy_backend_note=hierarchy_backend_sentence(
                        self._ui_adb_client, relative_time=engine.get_relative_time
                    ),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                logger.debug(f"Could not record hierarchy backend in the data engine: {exc}")
        try:
            trace_store.update_trace_fields(
                session_id,
                hierarchy_backend=backend,
                hierarchy_backend_note=hierarchy_backend_sentence(self._ui_adb_client),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.debug(f"Could not record hierarchy backend in status.json: {exc}")

    def _init_clients(
        self,
        device_id: str,
        platform: DevicePlatform,
    ):
        transport = EndpointTransport.shared(
            AdbEndpoint.create(self._config.servers.adb_host, self._config.servers.adb_port)
        )
        self._adb_client = transport.client()
        self._ui_adb_client = create_screen_client(device_id, transport=transport)

    async def _get_device_context(
        self,
        device_id: str,
        platform: DevicePlatform,
    ) -> DeviceContext:

        host_platform = system()
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            width, height = (1080, 2400)
            if self._adb_client:
                try:
                    width, height = self._adb_client.device(device_id).window_size()
                    logger.info(
                        f"Retrieved remote device screen dimensions dynamically: {width}x{height}"
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to query remote window size: {e}. Defaulting to 1080x2400."
                    )
            return DeviceContext(
                host_platform="LINUX",
                mobile_platform=platform,
                device_id=device_id,
                device_width=width,
                device_height=height,
            )

        # Query dimensions without starting UIAutomator or acquiring an awake
        # strategy. Those belong inside the global execution queue lease.
        if self._adb_client:
            try:
                device_width, device_height = self._adb_client.device(device_id).window_size()
                logger.info(f"Retrieved Android screen dimensions: {device_width}x{device_height}")
            except Exception as e:
                logger.warning(f"Failed to get Android window size: {e}, using defaults")
                device_width, device_height = 1080, 2340
        else:
            logger.warning("ADB client not available, using default dimensions")
            device_width, device_height = 1080, 2340

        from artemis.platform import platform as pal_platform

        return DeviceContext(
            host_platform=pal_platform.os_type.name,
            mobile_platform=platform,
            device_id=device_id,
            device_width=device_width,
            device_height=device_height,
        )


def _validate_and_prepare_file(file_path: Path):
    path_obj = Path(file_path)
    if path_obj.exists() and path_obj.is_dir():
        raise AgentTaskRequestError(f"Error: Path '{file_path}' is a directory, not a file.")
    try:
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.touch(exist_ok=True)
    except OSError as e:
        raise AgentTaskRequestError(f"Error creating file '{file_path}': {e}")


def attach_test_summary(output, run_outcome: dict | None):
    """Surfaces the machine-readable test summary in the task's return value.

    Only applies when the run actually had check items (the summary carries at
    least one counted item) — otherwise the historical return shape is kept
    untouched. String outputs are wrapped into a dict so that callers such as
    ``mobile_run_task`` receive the summary without parsing report prose.
    """
    if not run_outcome:
        return output
    tests = run_outcome.get("tests") or {}
    total = (
        int(tests.get("passed", 0))
        + int(tests.get("failed", 0))
        + int(tests.get("inconclusive", 0))
        + int(tests.get("unchecked", 0))
    )
    if total <= 0:
        return output
    summary = {"task_status": run_outcome.get("task_status"), **tests}
    if isinstance(output, dict):
        merged = dict(output)
        merged.setdefault("test_summary", summary)
        return merged
    if output is None:
        return {"test_summary": summary}
    if isinstance(output, str):
        return {"result": output, "test_summary": summary}
    # Typed/structured outputs keep their shape; the summary stays available in
    # run_outcome.json and the session state.
    return output


def resolve_trace_suffix(task_status: str, run_outcome: dict | None) -> str:
    """Trace naming: assertion failures must be distinguishable from _PASS."""
    if task_status != "completed":
        return "_FAIL"
    tests = (run_outcome or {}).get("tests") or {}
    if int(tests.get("failed", 0)) > 0:
        return "_TESTFAIL"
    return "_PASS"
