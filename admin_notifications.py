"""Cross-messenger delivery of administrator notifications."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import messenger_delivery
from messaging import ReplyTarget
import telegram_api
import vk_api


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventAccessDelivery:
    primary_delivered: bool
    delivered_targets: tuple[ReplyTarget, ...]
    failed_targets: tuple[ReplyTarget, ...]


@dataclass(frozen=True)
class AdminBroadcastDelivery:
    delivered_targets: tuple[ReplyTarget, ...]
    failed_targets: tuple[ReplyTarget, ...]


def configured_admin_targets() -> tuple[ReplyTarget, ...]:
    providers = (
        ("telegram", telegram_api.BOT_TOKEN, telegram_api.ADMIN_ID),
        ("vk", vk_api.BOT_TOKEN, vk_api.ADMIN_ID),
    )
    return tuple(
        ReplyTarget(provider, admin_id)
        for provider, token, admin_id in providers
        if token and admin_id
    )


async def _send_card(
    target: ReplyTarget,
    image: bytes,
    caption: str,
    *,
    filename: str,
    keyboard: dict | None = None,
    parse_mode: str | None = None,
) -> bool:
    try:
        delivery_options = {
            "filename": filename,
            "content_type": (
                "image/jpeg" if filename.endswith(".jpg") else "image/png"
            ),
            "keyboard": keyboard,
        }
        if parse_mode is not None:
            delivery_options["parse_mode"] = parse_mode
        return await messenger_delivery.send_photo(
            target,
            image,
            caption,
            **delivery_options,
        )
    except Exception as exc:
        log.warning(
            "Admin photo delivery failed provider=%s conversation=%s: %s",
            target.provider,
            target.conversation_id,
            exc,
        )
        return False


async def _send_text(
    target: ReplyTarget,
    text: str,
    *,
    parse_mode: str | None = None,
) -> bool:
    try:
        options = {"parse_mode": parse_mode} if parse_mode is not None else {}
        return await messenger_delivery.send_text(
            target,
            text,
            **options,
        )
    except Exception as exc:
        log.warning(
            "Admin text delivery failed provider=%s conversation=%s: %s",
            target.provider,
            target.conversation_id,
            exc,
        )
        return False


async def _send_document(
    target: ReplyTarget,
    payload: bytes,
    filename: str,
    content_type: str,
    caption: str,
) -> bool:
    try:
        return await messenger_delivery.send_document(
            target,
            payload,
            filename,
            content_type,
            caption=caption,
        )
    except Exception as exc:
        log.warning(
            "Admin document delivery failed provider=%s conversation=%s: %s",
            target.provider,
            target.conversation_id,
            exc,
        )
        return False


def _targets_with_primary(primary_target: ReplyTarget) -> tuple[ReplyTarget, ...]:
    targets = [ReplyTarget.from_value(primary_target)]
    seen = {(targets[0].provider, targets[0].conversation_id)}
    for target in configured_admin_targets():
        key = (target.provider, target.conversation_id)
        if key not in seen:
            seen.add(key)
            targets.append(target)
    return tuple(targets)


async def send_event_update(
    primary_target: ReplyTarget,
    image: bytes | None,
    caption: str,
    *,
    telegram_caption: str | None = None,
) -> EventAccessDelivery:
    """Send an event result to its origin and every configured administrator."""
    primary_target = ReplyTarget.from_value(primary_target)
    targets = _targets_with_primary(primary_target)

    async def deliver(target: ReplyTarget) -> bool:
        is_telegram = target.provider == "telegram"
        target_caption = (
            telegram_caption
            if is_telegram and telegram_caption is not None
            else caption
        )
        parse_mode = "HTML" if is_telegram and telegram_caption is not None else None
        if image is None:
            return await _send_text(
                target,
                target_caption,
                parse_mode=parse_mode,
            )
        return await _send_card(
            target,
            image,
            target_caption,
            filename="event_access_telegram_vk_qr.png",
            parse_mode=parse_mode,
        )

    results = await asyncio.gather(*(deliver(target) for target in targets))
    delivered = tuple(
        target for target, success in zip(targets, results, strict=True) if success
    )
    failed = tuple(
        target for target, success in zip(targets, results, strict=True) if not success
    )

    return EventAccessDelivery(
        primary_delivered=primary_target in delivered,
        delivered_targets=delivered,
        failed_targets=failed,
    )


async def send_event_history(
    primary_target: ReplyTarget,
    payload: bytes,
    caption: str,
) -> AdminBroadcastDelivery:
    """Send the previous event journal to every administrator."""
    targets = _targets_with_primary(primary_target)
    results = await asyncio.gather(*(
        _send_document(
            target,
            payload,
            "event_history_previous.json",
            "application/json; charset=utf-8",
            caption,
        )
        for target in targets
    ))
    return AdminBroadcastDelivery(
        delivered_targets=tuple(
            target
            for target, success in zip(targets, results, strict=True)
            if success
        ),
        failed_targets=tuple(
            target
            for target, success in zip(targets, results, strict=True)
            if not success
        ),
    )


_PRINT_ADMIN_ACTIONS = (
    ("✅ РАЗРЕШИТЬ", "approve", "positive"),
    ("❌ ОТКЛОНИТЬ", "reject", "negative"),
)


def print_approval_keyboard(provider: str, job_id: str) -> dict:
    if provider == "telegram":
        return {
            "inline_keyboard": [[
                {
                    "text": label,
                    "callback_data": f"print_admin:{action}:{job_id}",
                }
                for label, action, _color in _PRINT_ADMIN_ACTIONS
            ]],
        }
    if provider == "vk":
        return {
            "inline": True,
            "buttons": [[
                {
                    "action": {
                        "type": "callback",
                        "label": label,
                        "payload": json.dumps(
                            {
                                "type": "print_admin",
                                "action": action,
                                "job_id": job_id,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                    "color": color,
                }
                for label, action, color in _PRINT_ADMIN_ACTIONS
            ]],
        }
    raise ValueError(f"unsupported admin provider: {provider}")


async def send_print_approval(
    *,
    job_id: str,
    preview: bytes,
    caption: str,
    telegram_caption: str | None = None,
) -> AdminBroadcastDelivery:
    """Best-effort deliver one actionable print card to every admin channel."""
    targets = configured_admin_targets()

    async def deliver(target: ReplyTarget) -> bool:
        is_telegram = target.provider == "telegram"
        return await _send_card(
            target,
            preview,
            telegram_caption if is_telegram and telegram_caption else caption,
            filename=f"print_{job_id}.jpg",
            keyboard=print_approval_keyboard(target.provider, job_id),
            parse_mode="HTML" if is_telegram and telegram_caption else None,
        )

    results = await asyncio.gather(*(deliver(target) for target in targets))
    return AdminBroadcastDelivery(
        delivered_targets=tuple(
            target
            for target, success in zip(targets, results, strict=True)
            if success
        ),
        failed_targets=tuple(
            target
            for target, success in zip(targets, results, strict=True)
            if not success
        ),
    )


async def send_admin_text(text: str) -> AdminBroadcastDelivery:
    """Send one final status to every configured administrator."""
    targets = configured_admin_targets()
    results = await asyncio.gather(*(_send_text(target, text) for target in targets))
    return AdminBroadcastDelivery(
        delivered_targets=tuple(
            target
            for target, success in zip(targets, results, strict=True)
            if success
        ),
        failed_targets=tuple(
            target
            for target, success in zip(targets, results, strict=True)
            if not success
        ),
    )
