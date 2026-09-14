from io import StringIO

from artemis.utils.file import load_jsonc, strip_json_comments


def test_jsonc_comments_preserve_urls_and_comment_like_strings():
    text = r"""{
      // line comment
      "api_base": "https://example.test/v1",
      "literal": "// keep /* both */ and a quote: \\\"",
      /* block comment */
      "enabled": true
    }"""

    assert load_jsonc(StringIO(text)) == {
        "api_base": "https://example.test/v1",
        "literal": '// keep /* both */ and a quote: \\"',
        "enabled": True,
    }
    assert "line comment" not in strip_json_comments(text)
