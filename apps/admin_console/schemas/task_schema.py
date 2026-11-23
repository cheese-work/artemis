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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeviceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    host_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    serial: str = Field(pattern=r"^[A-Za-z0-9._:\-]{1,64}$")


class RunImageUpload(BaseModel):
    """One picture sent with a goal; the server checks the claim against the bytes."""

    name: str | None = None
    media_type: str
    data: str  # base64


class ModelSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=256)


class SmartQARouteStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["tap", "swipe", "back", "type"]
    text: str | None = Field(default=None, min_length=1, max_length=512)
    id: str | None = Field(default=None, min_length=1, max_length=256)
    direction: Literal["up", "down", "left", "right"] | None = None
    value: str | None = Field(default=None, max_length=512)
    secret: bool = False

    @model_validator(mode="after")
    def check_action(self):
        if self.secret:
            raise ValueError("Secret-backed steps are not supported by the route worker")
        if self.action in {"tap", "type"} and not (self.text or self.id):
            raise ValueError("Tap and type steps need a selector")
        if self.action == "type" and (
            not self.value
            or not self.value.isascii()
            or not self.value.isprintable()
            or "%s" in self.value
            or "${secret:" in self.value
        ):
            raise ValueError("Type steps need plain printable test input")
        if self.action == "swipe" and not self.direction:
            raise ValueError("Swipe steps need a direction")
        return self


class SmartQAOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["light", "dark"]
    lease_id: str = Field(min_length=1, max_length=128)
    apk_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["route", "persona"]
    fresh_install: Literal[True]
    clear_data: Literal[True]
    screens: list[str] = Field(min_length=1, max_length=100)
    route: list[SmartQARouteStep] = Field(default_factory=list, max_length=100)
    persona: str | None = None
    max_model_calls: int = Field(ge=0, le=80)

    @model_validator(mode="after")
    def check_mode(self):
        if self.mode == "route" and (not self.route or self.max_model_calls != 0):
            raise ValueError("Scripted SmartQA needs route steps and a zero model budget")
        return self


class RunOptions(BaseModel):
    smartqa: SmartQAOptions | None = None


class RunRequest(BaseModel):
    goal: str | None = None
    goals: list[str] | None = None
    profile: str | None = "flash"
    model: ModelSelection | None = None
    expected_output: str | None = None
    enable_outputter: bool | None = None
    # Pro-profile tuning (ignored by the Flash profile): a coarse Checker preset
    # ('off' | 'final' | 'checkpoints' | 'strict') and the Explorer perception
    # version used by the Operator ('flash' | 'pro' | 'ultra').
    verification_level: str | None = None
    explorer_mode: str | None = None
    locked_app_package: str | None = None
    app_path: str | None = None
    device_serial: str | None = None
    device_ref: DeviceRef | None = None
    # The bridge session of the browser-held phone this run is bound to. It implies the phone:
    # `device_serial` defaults to the bridge's serial, and a closed bridge refuses the run.
    bridge_session_id: str | None = None
    ingress: str | None = "frontend"
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    # Pictures for the one goal (image chat); see services/run_images.py for the limits.
    images: list[RunImageUpload] | None = None
    options: RunOptions | None = None

    @model_validator(mode="after")
    def check_device_ref(self):
        if self.device_ref and self.bridge_session_id:
            raise ValueError("bridge_session_id and device_ref are mutually exclusive")
        if self.device_ref and self.device_serial not in {None, self.device_ref.serial}:
            raise ValueError("device_serial must match device_ref.serial")
        return self


class ReplayRequest(BaseModel):
    device_id: str
    user_submits: dict
    tool_name: str = "ask_explorer"
    replay_id: str | None = None


class StopRequest(BaseModel):
    session_id: str | None = None
    device_id: str | None = None
    all: bool = False
