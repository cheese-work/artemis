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

"""Pictures sent with a goal: validate, store beside the run, serve back, hand to the worker.

No generic attachment store: a picture is a file under the run's own folder
(``traces/<session>/goal_images/<n>.<ext>``), so it is deleted, retained and
bundled with the run it belongs to.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re

from PIL import Image, UnidentifiedImageError

from artemis.data_engine.run_catalog import validate_session_id

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.services.run_artifacts import library_paths

# No earlier image upload exists to inherit limits from, so these are conservative.
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 24_000_000
# Base64 of the total cap (4/3) plus the goal text and JSON framing.
MAX_REQUEST_BYTES = 17 * 1024 * 1024

FOLDER = "goal_images"
_FORMATS = {
    "image/png": ("PNG", ".png"),
    "image/jpeg": ("JPEG", ".jpg"),
    "image/webp": ("WEBP", ".webp"),
}
_MEDIA_BY_EXTENSION = {ext: media for media, (_fmt, ext) in _FORMATS.items()}
_STORED_NAME = re.compile(r"(\d{1,2})(\.png|\.jpg|\.webp)")


class ImageRejected(AdminAPIError):
    pass


def _refuse(status: int, code: str, detail: str, fix: str) -> ImageRejected:
    return ImageRejected(status, detail, code, fix)


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    content: bytes
    media_type: str
    extension: str


def _field(upload, key: str) -> str:
    value = upload.get(key) if isinstance(upload, dict) else getattr(upload, key, None)
    return value if isinstance(value, str) else ""


def _decode(upload) -> tuple[bytes, str]:
    media_type = _field(upload, "media_type").strip().lower()
    if media_type not in _FORMATS:
        raise _refuse(
            415,
            "unsupported_image_type",
            "Only PNG, JPG, JPEG and WEBP images can be attached.",
            "Choose a PNG, JPG, JPEG or WEBP file.",
        )
    data = _field(upload, "data")
    if len(data) * 3 // 4 > MAX_IMAGE_BYTES + 3:  # cheap bound before decoding
        raise _too_large()
    try:
        return base64.b64decode(data, validate=True), media_type
    except (binascii.Error, ValueError):
        raise _refuse(
            400, "invalid_image", "The image could not be read.", "Attach the file again."
        ) from None


def _too_large() -> ImageRejected:
    return _refuse(
        413,
        "image_too_large",
        f"An image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB or too many pixels.",
        "Resize or compress the image and attach it again.",
    )


def _check_content(content: bytes, media_type: str) -> None:
    expected = _FORMATS[media_type][0]
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != expected:
                raise ValueError("content does not match the declared type")
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise _too_large()
            image.load()  # a truncated or corrupt body fails here, not at the model
    except ImageRejected:
        raise
    except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
        raise _refuse(
            400,
            "invalid_image",
            "The file is not a valid image of the type it claims to be.",
            "Attach a real PNG, JPG, JPEG or WEBP file.",
        ) from None


def validate(uploads) -> list[ValidatedImage]:
    """Every upload checked for type, size, count and real content, or one refusal."""
    if len(uploads) > MAX_IMAGES:
        raise _refuse(
            413,
            "too_many_images",
            f"At most {MAX_IMAGES} images can be attached to one message.",
            "Remove some images and send again.",
        )
    images, total = [], 0
    for upload in uploads:
        content, media_type = _decode(upload)
        if len(content) > MAX_IMAGE_BYTES:
            raise _too_large()
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise _refuse(
                413,
                "images_too_large",
                f"Attached images may total at most {MAX_TOTAL_BYTES // (1024 * 1024)} MB.",
                "Remove or shrink an image and send again.",
            )
        _check_content(content, media_type)
        images.append(ValidatedImage(content, media_type, _FORMATS[media_type][1]))
    return images


def is_safe_session_id(session_id: str) -> bool:
    try:
        validate_session_id(session_id, base_dir=library_paths()[1])
    except ValueError:
        return False
    return True


def _folder(session_id: str) -> Path:
    return library_paths()[1] / session_id / FOLDER


def store(session_id: str, images: list[ValidatedImage]) -> list[dict]:
    """Write the pictures beside the run; the public description of each (no paths)."""
    folder = _folder(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(images):
        (folder / f"{index}{image.extension}").write_bytes(image.content)
    return describe(session_id)


def _stored(session_id: str) -> list[tuple[int, str, Path]]:
    folder = _folder(session_id)
    if not folder.is_dir() or folder.is_symlink():
        return []
    found = []
    for entry in folder.iterdir():
        match = _STORED_NAME.fullmatch(entry.name)
        if match and entry.is_file() and not entry.is_symlink():
            found.append((int(match[1]), _MEDIA_BY_EXTENSION[match[2]], entry))
    return sorted(found)


def describe(session_id: str) -> list[dict]:
    """The run's pictures as clients see them: index, media type, and where to fetch it."""
    return [
        {
            "index": index,
            "media_type": media_type,
            "url": f"/api/sessions/{session_id}/{FOLDER.replace('_', '-')}/{index}",
        }
        for index, media_type, _path in _stored(session_id)
    ]


def stored_paths(session_id: str) -> list[str]:
    return [str(path) for _index, _media, path in _stored(session_id)]


def find(session_id: str, index: str) -> tuple[Path, str] | None:
    """The stored file and media type for a picture index, or None."""
    if not index.isdecimal() or len(index) > 2:
        return None
    for found_index, media_type, path in _stored(session_id):
        if found_index == int(index):
            return path, media_type
    return None


def worker_environment(session_id: str) -> dict[str, str]:
    """``ARTEMIS_GOAL_IMAGES`` for the worker, or nothing when the run has no pictures."""
    paths = stored_paths(session_id)
    return {"ARTEMIS_GOAL_IMAGES": json.dumps(paths)} if paths else {}


class _BodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Refuses an oversized ``POST /api/run`` before its body is read into memory."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/api/run":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
            await self._refuse(send)
            return
        received = 0
        started = False

        async def counted():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_REQUEST_BYTES:  # chunked: no length was declared
                raise _BodyTooLarge
            return message

        async def tracked(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except _BodyTooLarge:
            if started:
                raise
            await self._refuse(send)

    @staticmethod
    async def _refuse(send) -> None:
        body = json.dumps(
            {
                "detail": "The message and its images are too large to send.",
                "code": "image_request_too_large",
                "fix": "Remove or shrink an image and send again.",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
