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

"""scrcpy version reporting for toolchain diagnostics and recording."""

import subprocess


def read_scrcpy_version(scrcpy_executable: str) -> str:
    """Read the installed scrcpy version without checking recording support."""
    try:
        result = subprocess.run(
            [scrcpy_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"Could not read scrcpy version using {scrcpy_executable!r} --version: {exc}"
        ) from exc

    version_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        raise ValueError(
            f"Could not read scrcpy version: {scrcpy_executable!r} --version exited "
            f"with {result.returncode}: {version_output.strip() or 'no version output'}"
        )
    return version_output
