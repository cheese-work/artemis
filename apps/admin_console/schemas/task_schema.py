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


class RunRequest(BaseModel):
    goal: str | None = None
    goals: list[str] | None = None
    profile: str | None = "flash"
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
    ingress: str | None = "frontend"
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    # Pictures for the one goal (image chat); see services/run_images.py for the limits.
    images: list[RunImageUpload] | None = None

    @model_validator(mode="after")
    def check_device_ref(self):
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
