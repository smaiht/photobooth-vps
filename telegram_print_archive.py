"""Best-effort Telegram archive copy for successfully dispatched print jobs."""

from __future__ import annotations

import asyncio
import html
import logging

import messenger_delivery
import print_media
import telegram_api
from messaging import ReplyTarget


log = logging.getLogger(__name__)


async def send(
    *,
    job_id: str,
    payload: bytes,
    metadata: dict,
    source_target: ReplyTarget,
    mode_label: str,
) -> None:
    """Mirror an already-published print to the Telegram archive chat."""
    if not telegram_api.ARCHIVE_CHAT_ID:
        return
    archive_target = ReplyTarget("telegram", telegram_api.ARCHIVE_CHAT_ID)
    if source_target == archive_target:
        return
    try:
        preview = await asyncio.to_thread(print_media.jpeg_preview, payload)
        caption = (
            "<b>Фото отправлено на печать</b>\n"
            "Мероприятие: "
            f"<b>{html.escape(str(metadata.get('event_folder') or '—'))}</b>\n"
            f"Выбор: <b>{html.escape(mode_label)}</b>\n"
            f"{print_media.sender_caption(metadata, telegram_html=True)}\n"
            f"Job: <code>{html.escape(job_id)}</code>"
        )
        delivered = await messenger_delivery.send_photo(
            archive_target,
            preview,
            caption,
            filename=f"print_{job_id}.jpg",
            content_type="image/jpeg",
            parse_mode="HTML",
        )
        if not delivered:
            log.warning("Print archive copy was not delivered job=%s", job_id)
    except Exception:
        # The booth command has already been published. Archiving is best effort.
        log.exception("Could not deliver print archive copy job=%s", job_id)
