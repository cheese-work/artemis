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

"""Redactor unit tests: secrets in free text and in nested JSON (CHE-1093 A5)."""

import json

import pytest

from apps.admin_console.core.redaction import REDACTED, redact_json, redact_text

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
SECRETS = [
    ("password=hunter2", "hunter2"),
    ("password: hunter2", "hunter2"),
    ('{"password": "hunter2"}', "hunter2"),
    ('{\\"password\\": \\"hunter2\\"}', "hunter2"),  # JSON inside a JSON string
    ("db_password='hunter2'", "hunter2"),
    ("secret=s3cr3tvalue", "s3cr3tvalue"),
    ("access_token=abcdef123456", "abcdef123456"),
    ("api_key = abcdef123456", "abcdef123456"),
    ("sk-proj-abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    ("sk-ant-api03-abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    ("curl -H 'Authorization: Bearer abc.def-ghi_123'", "abc.def-ghi_123"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("Bearer abcdefghijklmnop", "abcdefghijklmnop"),
    ("aws key AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnopqrstuvwxyz0123456789"),
    ("github_pat_11ABCDEFG0abcdefghijklmnop_qrstuvwxyz", "11ABCDEFG0abcdefghijklmnop"),
    (f"jwt {JWT} end", JWT),
    ("AIzaSyA-abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN\n-----END PRIVATE KEY-----", "MIIEvQIBADAN"),
    ('typed password "hunter2" into the field', "hunter2"),
    ("entered the password hunter2 and pressed login", "hunter2"),
]


@pytest.mark.parametrize(("text", "secret"), SECRETS)
def test_redact_text_removes_secret(text, secret):
    out = redact_text(text)
    assert secret not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=4096 input_tokens: 120",
        "The quick brown fox opened Settings and tapped Wi-Fi",
        "author=bob; keyboard=us; tokenizer=gpt",
        "step 3: tap 'Sign in' button at (120, 340)",
        "",
    ],
)
def test_redact_text_leaves_ordinary_text_alone(text):
    assert redact_text(text) == text


def test_redact_text_keeps_the_key_and_is_idempotent():
    once = redact_text("login with password=hunter2 now")
    assert once == f"login with password={REDACTED} now"
    assert redact_text(once) == once


def test_redact_json_walks_nested_dicts_lists_and_tuples():
    payload = {
        "action": {
            "type": "type",
            "args": [{"note": "password=hunter2"}, ("Bearer abcdefghijklmnop",)],
        },
        "headers": {"Authorization": "Bearer abcdefghijklmnop", "X-Other": "fine"},
        "api_key": {"deep": ["anything"]},
        "count": 3,
        "none": None,
        "flag": True,
    }

    out = redact_json(payload)
    dumped = json.dumps(out)

    assert "hunter2" not in dumped and "abcdefghijklmnop" not in dumped and "anything" not in dumped
    assert out["headers"]["X-Other"] == "fine"
    assert out["count"] == 3 and out["none"] is None and out["flag"] is True
    assert payload["headers"]["Authorization"] == "Bearer abcdefghijklmnop"  # input untouched


def test_redact_json_reaches_json_embedded_in_strings():
    inner = json.dumps(
        {"messages": [{"content": "my password is hunter2, token=abc12345"}], "secret": "x"}
    )
    out = redact_json({"payload": inner})

    assert "hunter2" not in out["payload"] and "abc12345" not in out["payload"]
    assert json.loads(out["payload"])["messages"][0]["content"].startswith("my password is")


def test_redact_json_masks_text_typed_into_a_password_field():
    step = {"action": {"type": "input_text", "text": "hunter2", "element": "Password field"}}
    other = {"action": {"type": "input_text", "text": "hello", "element": "Search box"}}

    assert redact_json(step)["action"]["text"] == REDACTED
    assert redact_json(other) == other


def test_redact_json_masks_text_typed_into_an_input_type_password():
    step = {"x": [{"input_type": "textPassword", "value": "hunter2"}]}
    assert redact_json(step)["x"][0]["value"] == REDACTED


QUOTED = [
    'password="alpha beta"',
    "password='alpha beta'",
    'password="alpha \\"beta\\" gamma"',
    'password: "alpha beta"',
    "password = 'alpha beta gamma'",
    '{"password": "alpha beta"}',
    '{\\"password\\": \\"alpha beta\\"}',  # JSON quoted inside a JSON string
    'db_secret="alpha beta" next=1',
    'typed password "alpha beta" into the field',
    "api_key='alpha beta'",
]


@pytest.mark.parametrize("text", QUOTED)
def test_quoted_values_with_spaces_are_redacted_whole(text):
    out = redact_text(text)
    assert "alpha" not in out and "beta" not in out and "gamma" not in out
    assert REDACTED in out
    assert redact_text(out) == out  # idempotent


def test_quoted_redaction_keeps_json_parseable_and_neighbours():
    out = redact_text('{"password": "alpha beta", "user": "dana", "n": 1}')
    assert json.loads(out) == {"password": REDACTED, "user": "dana", "n": 1}
    assert redact_text('password="a b" next=1') == f'password="{REDACTED}" next=1'


def test_quoted_values_nested_through_redact_json():
    payload = {
        "log": ['login password="alpha beta" ok'],
        "raw": json.dumps({"x": 'password="a \\"b\\" c"'}),
    }
    dumped = json.dumps(redact_json(payload))
    assert "alpha" not in dumped and "beta" not in dumped and '\\"b\\"' not in dumped
