"""Finalize booth control responses and deliver them to their origin."""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass

import admin_notifications
import database
import event_access
import messenger_delivery
import runtime_config
import yadisk_control
import yadisk_poll
from messaging import ReplyTarget


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventUpdate:
    image: bytes | None
    caption: str
    telegram_caption: str


async def _persist_print_result(response: dict) -> bool:
    try:
        if response.get("status") == "ok":
            transition = await database.mark_print_job_queued(
                command_id=response["command_id"],
            )
            expected_status = "queued"
        else:
            transition = await database.mark_print_job_failed(
                command_id=response["command_id"],
                last_error=str(response.get("message") or "ошибка будки"),
            )
            expected_status = "failed"
    except Exception as exc:
        # Keep the durable response on Disk until the database transition lands.
        log.warning("Control: cannot persist print response: %s", exc)
        return False

    outcome = transition.get("outcome")
    expected = (
        outcome == expected_status
        or (
            outcome == "already_finished"
            and transition.get("status") == expected_status
        )
    )
    if not expected:
        log.warning(
            "Control: unexpected print response transition command=%s "
            "outcome=%s status=%s",
            response.get("command_id"),
            outcome,
            transition.get("status"),
        )
    return expected


async def _activate_event(response: dict) -> tuple[str | None, str | None] | None:
    event_name = str(response.get("event_folder") or "")
    if not event_name:
        return None
    try:
        await yadisk_poll.set_event_folder(event_name)
        runtime_config.save_event(event_name)
    except Exception as exc:
        log.warning("Control: cannot activate event on VPS: %s", exc)
        return None
    if event_name == event_access.TECHNICAL_EVENT_NAME:
        return None, None
    try:
        return await yadisk_poll.publish_current_folder(), None
    except Exception as exc:
        log.warning("Control: event activated but sharing failed: %s", exc)
        return None, str(exc)


async def _deliver_artifact(
    response: dict,
    target: ReplyTarget,
) -> bool:
    artifact_path = response["artifact_path"]
    try:
        payload = await yadisk_control.download_bytes(artifact_path)
        if response["command"] == "get_config":
            vps_config = await asyncio.to_thread(runtime_config.read_bytes)
            delivered = await messenger_delivery.send_documents(
                target,
                [
                    (
                        payload,
                        "photobooth_configs.txt",
                        "text/plain; charset=utf-8",
                    ),
                    (
                        vps_config,
                        "config_vps.json",
                        "application/json",
                    ),
                ],
            )
            artifact_label = "booth and VPS configs"
        else:
            delivered = await messenger_delivery.send_document(
                target,
                payload,
                "photobooth.log",
                "text/plain",
            )
            artifact_label = "log"
        if not delivered:
            return False
    except Exception as exc:
        log.warning("Control: artifact delivery failed: %s", exc)
        return False

    # Delivery is complete even if best-effort cleanup fails. Retrying the
    # response would duplicate an already delivered messenger attachment.
    try:
        deleted = await yadisk_control.delete_resource(artifact_path)
    except Exception as exc:
        deleted = None
        log.warning(
            "Control: delivered %s; artifact cleanup failed: %s",
            artifact_label,
            exc,
        )
    if deleted is False:
        log.warning(
            "Control: delivered %s but could not delete %s",
            artifact_label,
            artifact_path,
        )
    log.info(
        "Control: %s delivered provider=%s conversation=%s",
        artifact_label,
        target.provider,
        target.conversation_id,
    )
    return True


def _event_caption(
    response: dict,
    event_public_url: str | None,
    event_publish_error: str | None,
    guest_links: dict[str, str] | None,
    qr_error: str | None,
    *,
    telegram_html: bool,
) -> str:
    event_name = str(response.get("event_folder") or "")
    render = html.escape if telegram_html else str
    event_label = render(event_name)
    if telegram_html:
        event_label = f"<b>{event_label}</b>"
    lines = [f"✅ Event активирован на будке: {event_label}"]

    if (
        event_name == event_access.TECHNICAL_EVENT_NAME
        and response.get("start_locked") is True
        and response.get("unlock_sessions_remaining") == 0
    ):
        lines.append("🔒 Запуск заблокирован. Разрешённых фотосессий: 0.")
    if event_public_url:
        lines.append(f"Публичная папка: {render(event_public_url)}")
    elif event_publish_error:
        lines.append(
            "⚠️ Мероприятие активировано, но папку не удалось "
            f"опубликовать: {render(event_publish_error)}"
        )
    if qr_error:
        lines.append(f"⚠️ QR-код не создан: {render(qr_error)}")
    elif guest_links:
        lines.extend((
            "Ссылки для гостей:",
            f"Telegram: {render(guest_links['telegram'])}",
            f"VK: {render(guest_links['vk'])}",
        ))
    return "\n\n".join(lines)


async def _event_update(
    response: dict,
    event_public_url: str | None,
    event_publish_error: str | None,
) -> EventUpdate:
    event_name = str(response.get("event_folder") or "")
    image: bytes | None = None
    guest_links: dict[str, str] | None = None
    qr_error: str | None = None

    if event_name != event_access.TECHNICAL_EVENT_NAME:
        try:
            guest_links = event_access.guest_links(event_name)
            image = await asyncio.to_thread(
                event_access.guest_qr_sheet_png,
                guest_links,
            )
        except Exception as exc:
            log.warning("Control: event QR unavailable: %s", exc)
            guest_links = None
            qr_error = str(exc)

    caption_arguments = (
        response,
        event_public_url,
        event_publish_error,
        guest_links,
        qr_error,
    )
    return EventUpdate(
        image=image,
        caption=_event_caption(*caption_arguments, telegram_html=False),
        telegram_caption=_event_caption(*caption_arguments, telegram_html=True),
    )


async def handle_notice(notice: dict) -> bool:
    """Deliver one unsolicited booth notice to every configured administrator.

    The booth cannot address a messenger itself, so the notice has no
    ``reply_target`` and is broadcast to the administrators this VPS knows.
    Returning False keeps the message on Disk for a later retry.
    """
    title = str(notice.get("title") or "").strip()
    text = str(notice.get("text") or "").strip()
    caption = f"ℹ️ {title}\n\n{text}" if title else f"ℹ️ {text}"
    delivery = await admin_notifications.send_admin_text(caption)
    if not delivery.delivered_targets:
        log.warning(
            "Control: notice %s (%s) not delivered to any administrator",
            notice.get("notice_id"),
            notice.get("kind"),
        )
        return False
    if delivery.failed_targets:
        # At least one administrator has the notice, so retrying would only
        # duplicate it for the channels that already succeeded.
        log.warning(
            "Control: notice %s delivered partially; failed=%s",
            notice.get("notice_id"),
            ", ".join(
                f"{target.provider}:{target.conversation_id}"
                for target in delivery.failed_targets
            ),
        )
    log.info(
        "Control: notice %s (%s) delivered to %d administrator channel(s)",
        notice.get("notice_id"),
        notice.get("kind"),
        len(delivery.delivered_targets),
    )
    return True


async def handle(response: dict) -> bool:
    """Apply one validated booth response and deliver its user-facing result."""
    target = ReplyTarget.from_value(response.get("reply_target"))

    if response.get("command") == "print_image":
        if not await _persist_print_result(response):
            return False
        if response.get("status") == "ok":
            # The print flow acknowledged submission before the booth response.
            return True

    event_public_url: str | None = None
    event_publish_error: str | None = None
    if response["status"] == "ok" and response["command"] == "set_event":
        activation = await _activate_event(response)
        if activation is None:
            return False
        event_public_url, event_publish_error = activation

    if response.get("artifact_path"):
        return await _deliver_artifact(response, target)

    prefix = "✅" if response["status"] == "ok" else "❌"
    if response["status"] == "ok" and response["command"] == "set_event":
        event_update = await _event_update(
            response,
            event_public_url,
            event_publish_error,
        )
        delivery = await admin_notifications.send_event_update(
            target,
            event_update.image,
            event_update.caption,
            telegram_caption=event_update.telegram_caption,
        )
        return delivery.primary_delivered
    response_message = response["message"]
    caption = f"{prefix} {response_message}"
    return await messenger_delivery.send_text(target, caption)
