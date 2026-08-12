"""Best-effort Telegram/VK archive copies of accepted remote print jobs."""

from __future__ import annotations

import asyncio
import html
import logging

import messenger_delivery
import print_media
import runtime_config
import telegram_api
import vk_api
from messaging import ReplyTarget


log = logging.getLogger(__name__)


def _archive_target(provider: str) -> ReplyTarget | None:
    if provider == "telegram" and telegram_api.ARCHIVE_CHAT_ID:
        return ReplyTarget("telegram", telegram_api.ARCHIVE_CHAT_ID)
    if provider == "vk" and vk_api.ARCHIVE_CHAT_ID:
        return ReplyTarget("vk", vk_api.ARCHIVE_CHAT_ID)
    return None


def _caption(
    provider: str,
    *,
    job_id: str,
    metadata: dict,
    mode_label: str,
) -> tuple[str, str | None]:
    event_name = str(metadata.get("event_folder") or "—")
    if provider == "telegram":
        return (
            "<b>Фото отправлено на печать</b>\n"
            f"Мероприятие: <b>{html.escape(event_name)}</b>\n"
            f"Выбор: <b>{html.escape(mode_label)}</b>\n"
            f"{print_media.sender_caption(metadata, telegram_html=True)}\n"
            f"Job: <code>{html.escape(job_id)}</code>",
            "HTML",
        )
    return (
        "Фото отправлено на печать\n"
        f"Мероприятие: {event_name}\n"
        f"Выбор: {mode_label}\n"
        f"{print_media.sender_caption(metadata)}\n"
        f"Job: {job_id}",
        None,
    )


async def send(
    *,
    job_id: str,
    payload: bytes,
    metadata: dict,
    source_target: ReplyTarget,
    mode_label: str,
) -> None:
    """Mirror one already-published print to every enabled archive circuit."""
    providers = runtime_config.archive_delivery_providers()
    if not providers:
        return
    try:
        preview = await asyncio.to_thread(print_media.jpeg_preview, payload)
    except Exception:
        log.exception("Could not prepare print archive preview job=%s", job_id)
        return

    for provider in providers:
        target = _archive_target(provider)
        if target is None:
            log.warning(
                "Print archive %s destination is not configured job=%s",
                provider,
                job_id,
            )
            continue
        if source_target == target:
            continue
        caption, parse_mode = _caption(
            provider,
            job_id=job_id,
            metadata=metadata,
            mode_label=mode_label,
        )
        try:
            delivered = await messenger_delivery.send_photo(
                target,
                preview,
                caption,
                filename=f"print_{job_id}.jpg",
                content_type="image/jpeg",
                parse_mode=parse_mode,
            )
            if not delivered:
                log.warning(
                    "Print archive copy was not delivered provider=%s job=%s",
                    provider,
                    job_id,
                )
        except Exception:
            # The booth command has already been published. Archiving remains
            # best effort and one provider must not suppress the other.
            log.exception(
                "Could not deliver print archive copy provider=%s job=%s",
                provider,
                job_id,
            )
