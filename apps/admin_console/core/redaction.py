import json
import re
from typing import Any

from artemis.utils.redaction import REDACTED, redact, redact_json, redact_text

_IMAGE_DATA = re.compile(
    r"data:image/[^\s,\"']+;base64,[A-Za-z0-9+/=\r\n]*"
    r"|(?:iVBORw0KGgo|/9j/|UklGR|R0lGOD)[A-Za-z0-9+/=]+"
    r"|image://[^\s\"'<>\\]+|<ImageRef:[^>]*>",
    re.IGNORECASE,
)


def redact_image_data(value: Any) -> Any:
    """Copy shared data without inline pictures or fetchable image references."""
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(parsed, dict | list):
                    return json.dumps(redact_image_data(parsed), ensure_ascii=False)
        return _IMAGE_DATA.sub(REDACTED, value)
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_url", "input_image") or any(
            isinstance(value.get(key), str) and value[key].casefold().startswith("image/")
            for key in ("mime_type", "mimeType", "media_type")
        ):
            return REDACTED
        return {key: redact_image_data(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_image_data(item) for item in value)
    return value
