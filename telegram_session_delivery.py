"""Deliver completed photobooth media sessions to the Telegram archive chat."""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from pathlib import Path

import aiohttp

import telegram_api


log = logging.getLogger(__name__)

SessionFile = tuple[Path, str]


async def _post(endpoint: str, form: aiohttp.FormData) -> bool:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=180, connect=30),
        ) as session:
            async with session.post(
                f"{telegram_api.BOT_API_BASE}/{endpoint}",
                data=form,
            ) as response:
                if response.status != 200:
                    log.warning(
                        "Telegram %s %s: %s",
                        endpoint,
                        response.status,
                        await response.text(),
                    )
                    return False
                return True
    except Exception as exc:
        log.warning("Telegram %s failed: %s", endpoint, exc)
        return False


async def _send_chunk(files: list[SessionFile]) -> bool:
    with ExitStack() as stack:
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
            endpoint = "sendVideo" if kind == "video" else "sendPhoto"
            return await _post(endpoint, form)

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
        return await _post("sendMediaGroup", form)


async def send_session(files: list[SessionFile]) -> bool:
    """Send files in Telegram's groups of at most ten attachments."""
    if not telegram_api.BOT_TOKEN or not telegram_api.ARCHIVE_CHAT_ID:
        log.warning("Telegram token/chat is missing")
        return False
    for start in range(0, len(files), 10):
        if not await _send_chunk(files[start:start + 10]):
            return False
    return True
