"""Small async client for the OpenAI-compatible image-edit API."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import os

import aiohttp


IMAGE_API_BASE = "https://ne.so.gl/chatgpt/v1"
IMAGE_MODEL = "gpt-image-2-5-sunburst"
IMAGE_INPUT_MAX_BYTES = 10 * 1024 * 1024
IMAGE_INPUT_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})

_TIMEOUT = aiohttp.ClientTimeout(total=600, connect=15)


class ImageApiError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("IMAGE_API_KEY", "").strip()
    if not key:
        raise ImageApiError("IMAGE_API_KEY не настроен")
    return key


def _error_message(payload: object) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str) and error.strip():
            return error.strip()[:300]
    return "неизвестная ошибка"


async def edit_image(
    payload: bytes,
    *,
    filename: str,
    prompt: str,
    size: str,
) -> bytes:
    """Send one source image with a prompt and return the generated PNG."""
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("empty image-edit source")
    form = aiohttp.FormData()
    form.add_field("model", IMAGE_MODEL)
    form.add_field("prompt", prompt)
    form.add_field("size", size)
    form.add_field(
        "image",
        payload,
        filename=filename,
        content_type=mimetypes.guess_type(filename)[0] or "image/jpeg",
    )
    headers = {"Authorization": f"Bearer {_api_key()}"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                f"{IMAGE_API_BASE}/images/edits",
                data=form,
                timeout=_TIMEOUT,
            ) as response:
                status = response.status
                raw = await response.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise ImageApiError(f"image edit: не удалось подключиться ({exc!r})") from exc

    try:
        result = json.loads(raw)
    except ValueError:
        result = None
    if status != 200:
        raise ImageApiError(f"image edit: HTTP {status}, {_error_message(result)}")
    try:
        encoded = result["data"][0]["b64_json"]
        return base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, binascii.Error) as exc:
        raise ImageApiError("image edit: ответ не содержит картинку") from exc
