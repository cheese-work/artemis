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

"""Secret redactor for text that leaves the server (bundles, trace downloads).

Interface: ``redact_text(str) -> str`` and ``redact_json(value) -> value`` (a
recursive walk over dicts, lists and tuples that also looks inside strings
holding JSON). Both are pure and idempotent. This module stands alone so the
logging slice's redactor (CHE-1051) can replace or reuse it behind the same
two functions.

It is a best-effort pattern redactor, not a guarantee: it removes typed
passwords, key/token/secret assignments, auth headers and well-known token
shapes. Media (screenshots, video) is never passed through it.
"""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[REDACTED]"

_WORD = (
    r"password|passwd|pwd|passphrase|passcode|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|secret[_-]?key|client[_-]?secret|authorization|cookie"
)
# A key must END in a sensitive word, so ``max_tokens`` and ``author`` stay readable.
_SENSITIVE_KEY = re.compile(rf"(?:{_WORD})$", re.IGNORECASE)
# JSON may itself be quoted inside a JSON string, hence the optional backslashes.
_QUOTE = r"\\*[\"']?"
_ASSIGNMENT = re.compile(
    rf"(?P<head>(?<![\w.-])[\w.-]*?(?:{_WORD}){_QUOTE}\s*[:=]\s*{_QUOTE})"
    r"(?P<value>(?:(?:bearer|basic|token)\s+)?[^\s\"'\\,;&}\]]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?P<head>\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_TYPED = (
    re.compile(
        r"(?P<head>\b(?:typed|typing|type|entered|entering|enter|inputted|input|filled(?: in)?)\s+"
        r"(?:in\s+)?(?:the\s+|my\s+|your\s+)?(?:password|passcode|pin)\b\s*"
        r"(?:is\s+|as\s+|:\s*|=\s*)?[\"']?)(?P<value>[^\s\"',;]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<head>\b(?:password|passcode)\s+(?:is|was)\s*[:=]?\s*[\"']?)(?P<value>[^\s\"',;]+)",
        re.IGNORECASE,
    ),
)
_SHAPES = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}",
    re.DOTALL,
)
_PASSWORD_HINT = re.compile(r"pass(?:word|code|wd)", re.IGNORECASE)
# Keys that carry the text a user typed or an input's value.
_TYPED_VALUE_KEYS = frozenset({"text", "value", "input", "content", "typed_text", "input_text"})


def _keep_head(match: re.Match[str]) -> str:
    return f"{match.group('head')}{REDACTED}"


def redact_text(text: str) -> str:
    """Replace secrets in free text with ``[REDACTED]``, keeping the surrounding words."""
    if not text:
        return text
    out = _SHAPES.sub(REDACTED, text)
    out = _ASSIGNMENT.sub(_skip_redacted(_keep_head), out)
    for pattern in _TYPED:
        out = pattern.sub(_skip_redacted(_keep_head), out)
    return _BEARER.sub(_keep_head, out)


def _skip_redacted(replace):
    def apply(match: re.Match[str]) -> str:
        if match.string.startswith(REDACTED, match.start("value")):
            return match.group(0)
        return replace(match)

    return apply


def _mentions_password(mapping: dict[Any, Any]) -> bool:
    return any(
        isinstance(value, str) and _PASSWORD_HINT.search(value)
        for key, value in mapping.items()
        if key not in _TYPED_VALUE_KEYS
    )


def redact_json(value: Any) -> Any:
    """Recursive copy of ``value`` with every string and secret-keyed value redacted."""
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        typed_into_password = _mentions_password(value)
        out: dict[Any, Any] = {}
        for key, item in value.items():
            secret_key = isinstance(key, str) and (
                _SENSITIVE_KEY.search(key)
                or (typed_into_password and key.casefold() in _TYPED_VALUE_KEYS)
            )
            out[key] = REDACTED if secret_key and item is not None else redact_json(item)
        return out
    if isinstance(value, list | tuple):
        return type(value)(redact_json(item) for item in value)
    return value


def _redact_string(value: str) -> str:
    stripped = value.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return redact_text(value)
        if isinstance(parsed, dict | list):
            return json.dumps(redact_json(parsed), ensure_ascii=False)
    return redact_text(value)
