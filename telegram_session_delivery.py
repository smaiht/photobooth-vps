"""Deliver completed photobooth media sessions to the Telegram archive chat."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import ExitStack
from pathlib import Path

import aiohttp

import delivery_retry
import telegram_api


log = logging.getLogger(__name__)

SessionFile = tuple[Path, str]


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
                "Telegram %s failed HTTP %d after %d attempt(s)",
                endpoint,
                status,
                attempt,
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
    for start in range(0, len(files), 10):
        if not await _send_chunk(files[start:start + 10]):
            return False
    return True
