"""Deliver completed photobooth media sessions to the Telegram archive chat."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from contextlib import ExitStack
from pathlib import Path

import aiohttp
from PIL import Image, ImageOps

import delivery_retry
import telegram_api


log = logging.getLogger(__name__)

SessionFile = tuple[Path, str]

# Telegram rejects a photo attachment larger than this with a permanent 400,
# so an oversized camera JPEG must be re-encoded before it is uploaded.
PHOTO_SIZE_LIMIT = 10 * 1024 * 1024
# Telegram re-encodes every accepted photo and serves at most a 2560px copy,
# so full-resolution quality steps are tried first and downscaling last.
PHOTO_QUALITY_LADDER = (95, 92, 90, 85)
PHOTO_MAX_EDGE_LADDER = (None, 3200, 2560)


def _encode_jpeg(image: Image.Image, quality: int, max_edge: int | None) -> bytes:
    candidate = image
    if max_edge is not None and max(image.size) > max_edge:
        candidate = image.copy()
        candidate.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    try:
        buffer = io.BytesIO()
        candidate.save(
            buffer,
            "JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
        return buffer.getvalue()
    finally:
        if candidate is not image:
            candidate.close()


def _compress_photo(source_path: Path, target_path: Path) -> tuple[int, int, int]:
    """Re-encode one oversized photo so Telegram accepts it as a photo.

    Returns the encoded size, the JPEG quality and the longest edge actually
    used.  Raises ValueError when even the smallest variant stays too large.
    """
    with Image.open(source_path) as source:
        oriented = ImageOps.exif_transpose(source) or source
        try:
            image = oriented.convert("RGB")
        finally:
            if oriented is not source:
                oriented.close()
    try:
        for max_edge in PHOTO_MAX_EDGE_LADDER:
            for quality in PHOTO_QUALITY_LADDER:
                payload = _encode_jpeg(image, quality, max_edge)
                if len(payload) <= PHOTO_SIZE_LIMIT:
                    target_path.write_bytes(payload)
                    return (
                        len(payload),
                        quality,
                        max_edge or max(image.size),
                    )
    finally:
        image.close()
    raise ValueError("photo stays above the Telegram limit after re-encoding")


async def _prepare_files(files: list[SessionFile]) -> list[SessionFile] | None:
    """Return files Telegram can accept, re-encoding oversized photos."""
    prepared: list[SessionFile] = []
    for path, kind in files:
        size = path.stat().st_size
        if kind == "video" or size <= PHOTO_SIZE_LIMIT:
            prepared.append((path, kind))
            continue
        target = path.with_name(f"{path.stem}_tg.jpg")
        try:
            encoded, quality, max_edge = await asyncio.to_thread(
                _compress_photo, path, target,
            )
        except Exception as exc:
            log.warning(
                "Telegram photo %s is %.1f MiB and cannot be compressed "
                "under the %.0f MiB photo limit: %s",
                path.name,
                size / 1048576,
                PHOTO_SIZE_LIMIT / 1048576,
                exc,
            )
            return None
        log.info(
            "Telegram photo %s recompressed %.1f -> %.1f MiB "
            "(quality=%d, max_edge=%d) to fit the %.0f MiB photo limit",
            path.name,
            size / 1048576,
            encoded / 1048576,
            quality,
            max_edge,
            PHOTO_SIZE_LIMIT / 1048576,
        )
        prepared.append((target, kind))
    return prepared


def _form(
    files: list[SessionFile],
    stack: ExitStack,
) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("chat_id", telegram_api.ARCHIVE_CHAT_ID)
    if len(files) == 1:
        path, kind = files[0]
        field = "video" if kind == "video" else "photo"
        form.add_field(
            field,
            stack.enter_context(path.open("rb")),
            filename=path.name,
        )
        return form

    media = []
    for index, (path, kind) in enumerate(files):
        field = f"file{index}"
        media.append({
            "type": "video" if kind == "video" else "photo",
            "media": f"attach://{field}",
        })
        form.add_field(
            field,
            stack.enter_context(path.open("rb")),
            filename=path.name,
        )
    form.add_field("media", json.dumps(media))
    return form


async def _post(endpoint: str, files: list[SessionFile]) -> bool:
    timeout = aiohttp.ClientTimeout(total=180, connect=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, delivery_retry.MAX_ATTEMPTS + 1):
            try:
                with ExitStack() as stack:
                    form = _form(files, stack)
                    async with session.post(
                        f"{telegram_api.BOT_API_BASE}/{endpoint}",
                        data=form,
                    ) as response:
                        status = response.status
                        body = await response.read()
                        headers = getattr(response, "headers", None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= delivery_retry.MAX_ATTEMPTS:
                    log.warning(
                        "Telegram %s transport failed after %d attempts: %s",
                        endpoint,
                        attempt,
                        type(exc).__name__,
                    )
                    return False
                log.warning(
                    "Telegram %s transport retry attempt=%d/%d error=%s",
                    endpoint,
                    attempt,
                    delivery_retry.MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                await delivery_retry.wait_before_retry(attempt)
                continue

            if status == 200:
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError):
                    log.warning("Telegram %s returned invalid success JSON", endpoint)
                    return False
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return True
                log.warning("Telegram %s returned an unsuccessful response", endpoint)
                return False

            if (
                delivery_retry.retryable_http_status(status)
                and attempt < delivery_retry.MAX_ATTEMPTS
            ):
                log.warning(
                    "Telegram %s HTTP %d; retry attempt=%d/%d",
                    endpoint,
                    status,
                    attempt,
                    delivery_retry.MAX_ATTEMPTS,
                )
                await delivery_retry.wait_before_retry(
                    attempt,
                    retry_after=delivery_retry.retry_after_seconds(headers, body),
                )
                continue
            log.warning(
                "Telegram %s failed HTTP %d after %d attempt(s): %s",
                endpoint,
                status,
                attempt,
                delivery_retry.error_description(body) or "no description",
            )
            return False
    return False


async def _send_chunk(files: list[SessionFile]) -> bool:
    if len(files) == 1:
        endpoint = "sendVideo" if files[0][1] == "video" else "sendPhoto"
    else:
        endpoint = "sendMediaGroup"
    return await _post(endpoint, files)


async def send_session(files: list[SessionFile]) -> bool:
    """Send files in Telegram's groups of at most ten attachments."""
    if not telegram_api.BOT_TOKEN or not telegram_api.ARCHIVE_CHAT_ID:
        log.warning("Telegram token/chat is missing")
        return False
    prepared = await _prepare_files(files)
    if prepared is None:
        return False
    for start in range(0, len(prepared), 10):
        if not await _send_chunk(prepared[start:start + 10]):
            return False
    return True
