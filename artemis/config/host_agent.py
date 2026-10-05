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

"""Feature flag for release B (host agent). Off unless explicitly enabled.

The single definition, shared by the admin console and the adb transport. Accepted
spellings, case-insensitive: ``enabled`` (the documented one), ``1``, ``true``, ``yes``,
``on``; anything else, or unset, is off.

Read from the environment on every call, never cached, so a deployment flips it
by restarting with the flag set and tests flip it with ``monkeypatch``.
"""

from __future__ import annotations

import os

ENV_HOST_AGENT = "ARTEMIS_HOST_AGENT"
_TRUTHY = frozenset({"enabled", "1", "true", "yes", "on"})


def host_agent_enabled() -> bool:
    return os.environ.get(ENV_HOST_AGENT, "").strip().lower() in _TRUTHY
