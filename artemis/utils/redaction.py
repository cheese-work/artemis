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

"""Shared pattern redaction, handler protection and private worker goal transport.

``redact_text`` and ``redact_json`` preserve the download-helper interface.
``redact`` also masks configured credentials and the current session's goal.
Unknown free-text secrets and media cannot be inferred by pattern matching.
"""

from __future__ import annotations

import json
import atexit
from contextvars import ContextVar
import hashlib
import logging
import os
import re
import stat
import sys
import tempfile
import traceback
from typing import Any

REDACTED = "[REDACTED]"

_WORD = (
    r"password|passwd|pwd|passphrase|passcode|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|secret[_-]?key|client[_-]?secret|authorization|cookie"
)
# A key must END in a sensitive word, so ``max_tokens`` and ``author`` stay readable.
_SENSITIVE_KEY = re.compile(rf"(?:{_WORD})$", re.IGNORECASE)
# A value is quoted, or an unterminated quote (to the end of the line), or one
# unquoted run. A quoted value closes only on the SAME run of k backslashes + quote
# that opened it, so JSON nested at any depth works (k is 0, 1, 3, 7... for each
# json.dumps level; an escaped inner quote at that level carries 2k+1 backslashes).
# An escaped backslash right before the real closer reads as an inner quote: that
# errs towards redacting more, never less.
_VALUE = (
    r"(?P<bs>\\*)(?P<q>[\"'])(?:(?!(?<!\\)(?P=bs)(?P=q)).)*(?P=bs)(?P=q)"
    r"|\\*[\"'][^\n]*"
    r"|(?:(?:bearer|basic|token)\s+)?[^\s\"'\\,;&}\]]+"
)
_ASSIGNMENT = re.compile(
    rf"(?P<head>(?<![\w.-])[\w.-]*?(?:{_WORD})(?:\\*[\"'])?\s*[:=]\s*)(?P<value>{_VALUE})",
    re.IGNORECASE,
)
# A Cookie / Set-Cookie header carries several name=value pairs (some quoted): redact the
# whole value. It ends at the quote that wrapped it (a JSON value, or the quoted header
# argument of ``curl -H '...'``), else at the end of the line.
_COOKIE = re.compile(
    r"(?P<head>(?P<lead>\\*[\"'])?\b(?:set-)?cookie(?:\\*[\"'])?\s*[:=]\s*)(?P<rest>[^\r\n]*)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?P<head>\bbearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_TYPED = (
    re.compile(
        r"(?P<head>\b(?:typed|typing|type|entered|entering|enter|inputted|input|filled(?: in)?)\s+"
        r"(?:in\s+)?(?:the\s+|my\s+|your\s+)?(?:password|passcode|pin)\b\s*"
        rf"(?:is\s+|as\s+|:\s*|=\s*)?)(?P<value>{_VALUE})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<head>\b(?:password|passcode)\s+(?:is|was)\s*[:=]?\s*)(?P<value>{_VALUE})",
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


def _redact_value(match: re.Match[str]) -> str:
    """Replace the value but keep its quotes, so redacted JSON text stays parseable."""
    value = match.group("value")
    opener = re.match(r"\\*[\"']", value)
    closer = ""
    if opener:
        delimiter = opener.group()
        if len(value) >= 2 * len(delimiter) and value.endswith(delimiter):
            closer = delimiter
    if match.string.startswith(
        REDACTED, match.start("value") + len(opener.group() if opener else "")
    ):
        return match.group(0)
    return f"{match.group('head')}{opener.group() if opener else ''}{REDACTED}{closer}"


def _redact_cookie(match: re.Match[str]) -> str:
    rest = match.group("rest")
    opener = re.match(r"\\*[\"']", rest)
    quote = opener.group() if opener else match.group("lead") or ""
    body = rest[len(opener.group()) :] if opener else rest
    end = re.search(rf"(?<!\\){re.escape(quote)}", body) if quote else None
    if end and opener and re.match(r"\s*;", body[end.end() :]):
        end = None  # ``cookie="a=1"; b=2``: the quote closed one pair, more follow
    value, tail = (body[: end.start()], body[end.start() :]) if end else (body, "")
    head = match.group("head") + (opener.group() if opener else "")
    tail = _COOKIE.sub(_redact_cookie, tail)  # a second header later on the same line
    if not value.strip() or value.startswith(REDACTED):
        return f"{head}{value}{tail}"
    return f"{head}{REDACTED}{tail}"


def redact_text(text: str) -> str:
    """Replace secrets in free text with ``[REDACTED]``, keeping the surrounding words."""
    if not text:
        return text
    out = _SHAPES.sub(REDACTED, text)
    out = _COOKIE.sub(_redact_cookie, out)
    out = _ASSIGNMENT.sub(_redact_value, out)
    for pattern in _TYPED:
        out = pattern.sub(_redact_value, out)
    return _BEARER.sub(_keep_head, out)


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


_SESSION_ID = ContextVar("log_session_id", default=None)
_PRIVATE_TEXT = ContextVar("log_private_text", default=frozenset())


def bind_session(session_id: str | None, goal: str | None = None) -> None:
    _SESSION_ID.set(session_id)
    values = set()
    if goal:
        values.add(goal)
        values.update(line for line in goal.splitlines() if len(line) >= 8)
        for pattern in (_ASSIGNMENT, *_TYPED):
            values.update(match.group("value").strip("\\\"'") for match in pattern.finditer(goal))
    _PRIVATE_TEXT.set(frozenset(values))


def goal_metadata(goal: str) -> str:
    return f"goal_length={len(goal)} goal_sha256={hashlib.sha256(goal.encode()).hexdigest()[:12]}"


class Redactor(logging.Filter):
    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in redact_json(value).items()}
        if isinstance(value, list | tuple):
            return type(value)(self.redact(item) for item in value)
        if not isinstance(value, str):
            return value
        secrets = set(_PRIVATE_TEXT.get())
        secrets.update(
            item
            for key, item in tuple(os.environ.items())
            if item and key not in {"PWD", "OLDPWD"} and _SENSITIVE_KEY.search(key)
        )
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            if escaped != secret:
                value = value.replace(escaped, REDACTED)
        return _redact_string(value)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = self.redact("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
            for key, value in tuple(record.__dict__.items()):
                if isinstance(value, str | dict | list | tuple):
                    record.__dict__[key] = self.redact(value)
            record.session_id = (
                getattr(record, "session_id", None)
                or _SESSION_ID.get()
                or os.environ.get("ARTEMIS_SESSION_ID")
                or "-"
            )
            record.session_id = self.redact(str(record.session_id))
            if "session_id=" not in record.msg:
                record.msg = f"session_id={record.session_id} {record.msg}"
        except Exception:
            for key, value in tuple(record.__dict__.items()):
                if isinstance(value, str | dict | list | tuple):
                    record.__dict__[key] = REDACTED
            record.msg = "[REDACTED: unrenderable log record]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            record.session_id = "-"
        return True


redactor = Redactor()


def redact(value: Any) -> Any:
    return redactor.redact(value)


class LineRedactor:
    def __init__(self):
        self.pending = ""
        self.discarding = False

    def feed(self, text: str, final: bool = False) -> str:
        output = []
        for part in text.splitlines(keepends=True):
            complete = part.endswith(("\n", "\r"))
            if not self.discarding:
                self.pending += part
                if len(self.pending) > 65536:
                    self.pending = ""
                    self.discarding = True
            if complete:
                output.append(
                    "[REDACTED: oversized log line]\n" if self.discarding else redact(self.pending)
                )
                self.pending = ""
                self.discarding = False
        if final and (self.pending or self.discarding):
            output.append(
                "[REDACTED: oversized log line]\n" if self.discarding else redact(self.pending)
            )
            self.pending = ""
            self.discarding = False
        return "".join(output)


class RedactingStream:
    def __init__(self, stream):
        self.stream = stream
        self.lines = LineRedactor()

    def write(self, text: str) -> int:
        self.stream.write(self.lines.feed(text))
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def finish(self) -> None:
        if getattr(self.stream, "closed", False):
            return
        self.stream.write(self.lines.feed("", final=True))
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def configure_logging(*, streams: bool = False) -> None:
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
        root.setLevel(logging.INFO)
    loggers = [root, *logging.Logger.manager.loggerDict.values()]
    for logger in loggers:
        if isinstance(logger, logging.Logger):
            for handler in logger.handlers:
                if not any(isinstance(item, Redactor) for item in handler.filters):
                    handler.addFilter(redactor)
    if streams:
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name)
            if not isinstance(stream, RedactingStream):
                protected = RedactingStream(stream)
                setattr(sys, name, protected)
                atexit.register(protected.finish)


def write_goal_file(goal: str, directory=None) -> str:
    descriptor, path = tempfile.mkstemp(prefix="artemis-goal-", dir=directory)
    written = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as goal_file:
            goal_file.write(goal)
        written = True
        return path
    finally:
        if not written:
            os.unlink(path)


def read_goal_file(path: str) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as goal_file:
        metadata = os.fstat(goal_file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or (
            os.name != "nt" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077)
        ):
            raise ValueError("Worker goal file must be a private, owned regular file")
        try:
            goal = goal_file.read(4 * 1024 * 1024 + 1)
            if len(goal) > 4 * 1024 * 1024:
                raise ValueError("Worker goal exceeds the input size limit")
            return goal
        finally:
            os.unlink(path)
