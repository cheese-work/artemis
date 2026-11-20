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

"""Admin Console settings and backward-compatible shared path exports."""

import os

from artemis.config import (
    DB_PATH,
    IMAGES_DIR,
    PAUSE_FILE,
    REPLAY_BASE_DIR,
    TEST_DATA_DIR,
    TEST_OUTPUTS_DIR,
    TRACES_PATH,
    WORKSPACE_ROOT,
    init_ls_address,
)


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


COMMENT_BODY_MAX_CHARS = _positive_int_setting("ARTEMIS_COMMENT_BODY_MAX_CHARS", 4000)
NOTE_WRITE_LIMIT = _positive_int_setting("ARTEMIS_NOTE_WRITE_LIMIT", 30)
NOTE_WRITE_WINDOW_SECONDS = 60

__all__ = [
    "COMMENT_BODY_MAX_CHARS",
    "DB_PATH",
    "IMAGES_DIR",
    "NOTE_WRITE_LIMIT",
    "NOTE_WRITE_WINDOW_SECONDS",
    "PAUSE_FILE",
    "REPLAY_BASE_DIR",
    "TEST_DATA_DIR",
    "TEST_OUTPUTS_DIR",
    "TRACES_PATH",
    "WORKSPACE_ROOT",
    "init_ls_address",
]
