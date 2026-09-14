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

import json
import re
from typing import IO


def strip_json_comments(text: str) -> str:
    """Strip JSONC comments while preserving quoted strings and escapes."""
    pattern = r'//.*?$|/\*.*?\*/|"(?:\\.|[^\\"])*"'

    def replace_comment(match: re.Match[str]) -> str:
        return "" if match.group(0).startswith("/") else match.group(0)

    return re.sub(pattern, replace_comment, text, flags=re.DOTALL | re.MULTILINE)


def load_jsonc(file: IO) -> dict:
    return json.loads(strip_json_comments(file.read()))
