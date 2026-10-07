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

"""Pictures the user attached to a goal, read from the worker's environment.

The admin console validates and stores the pictures, then starts the worker with
``ARTEMIS_GOAL_IMAGES`` set to a JSON list of absolute file paths. Each agent
that reads the goal also sends these blocks; a picture that cannot be read
raises, so the model never answers a goal whose pictures were silently dropped.
"""

import base64
import json
import os
from pathlib import Path

from langchain_core.messages import HumanMessage

ENV_VAR = "ARTEMIS_GOAL_IMAGES"

_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
_INTRO = "--- Images the user attached to the goal ---"


class GoalImageError(RuntimeError):
    """An attached picture is named but cannot be delivered to the model."""


def _paths() -> list[Path]:
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except ValueError as exc:
        raise GoalImageError(f"{ENV_VAR} is not valid JSON.") from exc
    if not isinstance(values, list) or not all(
        isinstance(v, str) and os.path.isabs(v) for v in values
    ):
        raise GoalImageError(f"{ENV_VAR} must be a JSON list of absolute file paths.")
    return [Path(v) for v in values]


def goal_image_blocks() -> list[dict]:
    """Text intro plus one ``image_url`` block per attached picture; empty when none."""
    paths = _paths()
    if not paths:
        return []
    blocks: list[dict] = [{"type": "text", "text": _INTRO}]
    for path in paths:
        media_type = _MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            raise GoalImageError(f"Attached image {path.name} has an unsupported type.")
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise GoalImageError(f"Attached image {path.name} cannot be read.") from exc
        blocks.append(
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}}
        )
    return blocks


def goal_image_messages() -> list[HumanMessage]:
    """The pictures as one human message for a static prompt prefix; empty when none."""
    blocks = goal_image_blocks()
    return [HumanMessage(content=blocks)] if blocks else []
