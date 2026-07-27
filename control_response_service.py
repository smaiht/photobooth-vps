"""Finalize booth control responses and deliver them to their origin."""

from __future__ import annotations

import asyncio
import logging

import admin_notifications
import database
import event_access
import messenger_delivery
import runtime_config
import yadisk_control
import yadisk_poll
from messaging import ReplyTarget


log = logging.getLogger(__name__)


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
    event_name = response.get("event_folder")
    if not event_name:
        return None
    try:
        await yadisk_poll.set_event_folder(event_name)
        runtime_config.save_event(event_name)
    except Exception as exc:
        log.warning("Control: cannot activate event on VPS: %s", exc)
        return None
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


async def _event_card(
    response: dict,
    event_public_url: str | None,
    event_publish_error: str | None,
) -> tuple[bytes | None, str]:
    response_message = response["message"]
    event_name = str(response.get("event_folder") or "")
    if event_public_url:
        response_message += f"\n\nПубличная папка: {event_public_url}"
    elif event_publish_error:
        response_message += (
            "\n⚠️ Мероприятие активировано, но папку не удалось "
            f"опубликовать: {event_publish_error}"
        )
    if not event_name or event_name == event_access.TECHNICAL_EVENT_NAME:
        return None, response_message

    try:
        guest_links = event_access.guest_links(event_name)
        image = await asyncio.to_thread(
            event_access.guest_qr_sheet_png,
            guest_links,
        )
    except Exception as exc:
        log.warning("Control: event QR unavailable: %s", exc)
        return None, response_message + f"\n⚠️ QR-код не создан: {exc}"
    return image, (
        response_message
        + "\n\nСсылки для гостей:"
        + f"\nTelegram: {guest_links['telegram']}"
        + f"\nVK: {guest_links['vk']}"
    )


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
    response_message = response["message"]
    event_qr_png: bytes | None = None
    if response["status"] == "ok" and response["command"] == "set_event":
        event_qr_png, response_message = await _event_card(
            response,
            event_public_url,
            event_publish_error,
        )

    caption = f"{prefix} {response_message}"
    if response["status"] == "ok" and response["command"] == "set_event":
        delivery = await admin_notifications.send_event_update(
            target,
            event_qr_png,
            caption,
        )
        return delivery.primary_delivered
    return await messenger_delivery.send_text(target, caption)
