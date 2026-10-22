"""VK community bot routing and Bots Long Poll worker."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

import aiohttp

import admin_command_service
import admin_notifications
import database
import event_access
import print_flow
import vk_api
import vk_print
from messaging import ReplyTarget


log = logging.getLogger(__name__)

EVENT_ACCESS_REQUIRED_MESSAGE = (
    "Для подключения отсканируйте VK QR-код текущего мероприятия и "
    "отправьте сообщение из открывшегося диалога."
)
USER_PROFILE_CACHE_TTL_SECONDS = 60 * 60
USER_PROFILE_CACHE_MAX_SIZE = 4096
ADMIN_NOTICE_TEXT_LIMIT = 3000
ADMIN_NOTICE_TIMEOUT_SECONDS = 20
ATTACHMENT_LABELS = {
    "photo": "фото",
    "video": "видео",
    "audio": "аудио",
    "audio_message": "голосовое",
    "doc": "документ",
    "sticker": "стикер",
    "graffiti": "граффити",
    "wall": "репост записи",
    "wall_reply": "комментарий",
    "link": "ссылка",
    "poll": "опрос",
    "gift": "подарок",
    "story": "история",
    "market": "товар",
    "article": "статья",
    "call": "звонок",
}
_user_profile_cache: OrderedDict[
    int,
    tuple[float, dict[str, str | None]],
] = OrderedDict()


def peek_cached_user_profile(user_id: int) -> dict[str, str | None] | None:
    """Return a fresh cached profile without delaying a button callback."""
    cached = _user_profile_cache.get(user_id)
    if cached is None:
        return None
    expires_at, profile = cached
    if expires_at <= time.monotonic():
        del _user_profile_cache[user_id]
        return None
    _user_profile_cache.move_to_end(user_id)
    return dict(profile)


def incoming_private_message(update: dict) -> dict | None:
    """Return an incoming direct VK message, ignoring chats and bot output."""
    if not isinstance(update, dict) or update.get("type") != "message_new":
        return None
    event_object = update.get("object")
    if not isinstance(event_object, dict):
        return None
    message = event_object.get("message", event_object)
    if not isinstance(message, dict) or message.get("out") not in (None, 0):
        return None

    from_id = message.get("from_id")
    peer_id = message.get("peer_id")
    if (
        not isinstance(from_id, int)
        or isinstance(from_id, bool)
        or from_id <= 0
        or not isinstance(peer_id, int)
        or isinstance(peer_id, bool)
        or peer_id != from_id
    ):
        return None
    return message


def incoming_private_event(update: dict) -> dict | None:
    """Return one callback-button event from a private VK conversation."""
    if not isinstance(update, dict) or update.get("type") != "message_event":
        return None
    event = update.get("object")
    if not isinstance(event, dict):
        return None
    user_id = event.get("user_id")
    peer_id = event.get("peer_id")
    cmid = event.get("conversation_message_id")
    event_id = event.get("event_id")
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or not isinstance(peer_id, int)
        or isinstance(peer_id, bool)
        or peer_id != user_id
        or not isinstance(cmid, int)
        or isinstance(cmid, bool)
        or cmid <= 0
        or not isinstance(event_id, str)
        or not event_id
    ):
        return None
    return event


def provider_update_id(update: dict, message: dict) -> str | None:
    event_id = update.get("event_id")
    if event_id is not None and str(event_id).strip():
        return str(event_id)

    peer_id = message.get("peer_id")
    message_id = message.get("conversation_message_id") or message.get("id")
    if peer_id is None or message_id is None:
        return None
    return f"message:{peer_id}:{message_id}"


def event_update_id(update: dict, event: dict) -> str:
    update_id = update.get("event_id")
    if update_id is not None and str(update_id).strip():
        return str(update_id)
    return f"message_event:{event['event_id']}"


async def cached_user_profile(
    session: aiohttp.ClientSession,
    user_id: int,
) -> dict[str, str | None]:
    """Resolve a VK profile best-effort and avoid one API call per button."""
    cached = peek_cached_user_profile(user_id)
    if cached is not None:
        return cached

    try:
        profile = await vk_api.get_user_profile(session, user_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Profile data is useful metadata, but must never block printing or
        # Long Poll progress. Do not cache failures so a later message retries.
        log.warning("VK profile lookup failed for user=%s: %s", user_id, exc)
        return {}

    _user_profile_cache[user_id] = (
        time.monotonic() + USER_PROFILE_CACHE_TTL_SECONDS,
        dict(profile),
    )
    _user_profile_cache.move_to_end(user_id)
    while len(_user_profile_cache) > USER_PROFILE_CACHE_MAX_SIZE:
        _user_profile_cache.popitem(last=False)
    return dict(profile)


async def record_start(
    update: dict,
    message: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> bool:
    """Persist a VK ref parameter and report whether the message had one."""
    parameter = message.get("ref")
    if not isinstance(parameter, str) or not parameter.strip():
        return False
    parameter = parameter.strip()
    user_id = message.get("from_id")
    profile = profile if isinstance(profile, dict) else {}
    await database.record_bot_start(
        provider="vk",
        provider_user_id=user_id,
        start_parameter=parameter,
        provider_update_id=provider_update_id(update, message),
        username=profile.get("username"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
    )
    log.info("VK deep link stored for user=%s", user_id)
    return True


async def user_has_event_access(
    user_id,
    *,
    event_token: str | None,
    cafe_mode: bool,
) -> bool:
    admin = vk_api.is_admin(user_id)
    user = print_flow.PrintUser(
        provider="vk",
        provider_user_id=int(user_id),
        conversation_id=int(user_id),
        allowlisted=admin,
        is_admin=admin,
    )
    return await print_flow.user_has_access(
        user,
        event_token=event_token,
        cafe_mode=cafe_mode,
    )


async def reply_for_current_event(
    session: aiohttp.ClientSession,
    message: dict,
) -> None:
    user_id = message["from_id"]
    peer_id = message["peer_id"]
    try:
        event_name, event_token, cafe_mode = event_access.current_event()
        has_access = await user_has_event_access(
            user_id,
            event_token=event_token,
            cafe_mode=cafe_mode,
        )
    except Exception as exc:
        log.warning("VK event access check failed for user=%s: %s", user_id, exc)
        await vk_api.send_text(
            session,
            peer_id,
            "⚠️ Подключение временно недоступно. Попробуйте чуть позже.",
        )
        return

    if has_access:
        text = (
            "✅ VK-бот подключён. Отправьте фотографию, чтобы напечатать её."
            if cafe_mode
            else print_flow.connected_event_message(event_name)
        )
    else:
        text = EVENT_ACCESS_REQUIRED_MESSAGE
    await vk_api.send_text(session, peer_id, text)


async def current_event_guest(user_id: int) -> str | None:
    """Return the current event name if this user joined it by its QR."""
    event_name, event_token, cafe_mode = event_access.current_event()
    if cafe_mode or event_token is None:
        return None
    joined = await database.user_has_current_start_parameter(
        provider="vk",
        provider_user_id=user_id,
        start_parameter=event_token,
    )
    return event_name if joined else None


def content_labels(message: dict) -> list[str]:
    """Describe non-text message content for a plain-text admin notice."""
    counts: dict[str, int] = {}
    attachments = message.get("attachments")
    for attachment in attachments if isinstance(attachments, list) else ():
        kind = attachment.get("type") if isinstance(attachment, dict) else None
        if isinstance(kind, str) and kind:
            label = ATTACHMENT_LABELS.get(kind, kind)
            counts[label] = counts.get(label, 0) + 1
    if message.get("fwd_messages"):
        counts["пересланные сообщения"] = 1
    if message.get("geo"):
        counts["геолокация"] = 1
    return [
        label if count == 1 else f"{label} ×{count}"
        for label, count in counts.items()
    ]


def admin_message_notice(
    message: dict,
    profile: dict[str, str | None],
    *,
    group_id,
    guest_event: str | None,
) -> str:
    """Build a plain-text notice about a message written to the community."""
    user_id = message["from_id"]
    name = " ".join(
        part.strip()
        for part in (profile.get("first_name"), profile.get("last_name"))
        if isinstance(part, str) and part.strip()
    )
    sender = f"vk.com/id{user_id}"
    lines = [f"💬 VK: {name} ({sender})" if name else f"💬 VK: {sender}"]
    if guest_event:
        lines.append(f"Гость мероприятия «{guest_event}»")
    lines.append("")

    text = str(message.get("text") or "").strip()
    if len(text) > ADMIN_NOTICE_TEXT_LIMIT:
        text = f"{text[:ADMIN_NOTICE_TEXT_LIMIT - 1]}…"
    labels = content_labels(message)
    if text:
        lines.append(text)
    if labels:
        lines.append(f"📎 {', '.join(labels)}")
    if not text and not labels:
        lines.append("(пустое сообщение)")

    if isinstance(group_id, int) and not isinstance(group_id, bool) and group_id > 0:
        lines.extend(("", f"Ответить: https://vk.com/gim{group_id}?sel={user_id}"))
    return "\n".join(lines)


async def notify_admin(
    update: dict,
    message: dict,
    profile: dict[str, str | None],
    *,
    print_upload: bool = False,
) -> None:
    """Report a community message to the Telegram admin without ever raising.

    A raised error would make Long Poll replay the batch and, for a photo,
    process the print upload a second time.
    """
    user_id = message["from_id"]
    try:
        try:
            guest_event = await current_event_guest(user_id)
        except Exception as exc:
            log.warning(
                "VK guest lookup for admin notice failed user=%s: %s",
                user_id,
                exc,
            )
            guest_event = None
        if print_upload and guest_event is not None:
            # Photos from guests of the current event are ordinary print traffic.
            return
        notice = admin_message_notice(
            message,
            profile,
            group_id=update.get("group_id"),
            guest_event=guest_event,
        )
        delivered = await asyncio.wait_for(
            admin_notifications.send_telegram_admin_text(notice),
            timeout=ADMIN_NOTICE_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        log.warning("VK message admin notice timed out user=%s", user_id)
        return
    except Exception as exc:
        log.warning("VK message admin notice failed user=%s: %s", user_id, exc)
        return
    if not delivered:
        log.warning("VK message from user=%s was not reported to the admin", user_id)


async def route_message_update(
    session: aiohttp.ClientSession,
    update: dict,
    message: dict,
) -> None:
    profile = await cached_user_profile(session, message["from_id"])
    started = await record_start(update, message, profile=profile)
    if started:
        await reply_for_current_event(session, message)

    # Keep routing old text-button cards until all pre-deployment cards expire.
    if await vk_print.handle_action(session, message, profile=profile):
        return
    admin = vk_api.is_admin(message["from_id"])
    if await vk_print.handle_message(session, message, profile=profile):
        if not started and not admin:
            await notify_admin(update, message, profile, print_upload=True)
        return

    if not started and await vk_print.handle_ai_text(
        session,
        message,
        profile=profile,
    ):
        return
    text = str(message.get("text") or "")
    if admin and (text.strip() or not started):
        await admin_command_service.handle_message(
            ReplyTarget("vk", message["peer_id"]),
            text,
        )
        return
    if started:
        return
    await database.ensure_bot_user(
        provider="vk",
        provider_user_id=message["from_id"],
        username=profile.get("username"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
    )
    # The community is also a regular inbox: people who write anything other
    # than a photo get no scripted reply; the admin answers them in person.
    await notify_admin(update, message, profile)


async def route_message_event(
    session: aiohttp.ClientSession,
    event: dict,
) -> None:
    # Names were stored when the photo/message arrived. A callback must not
    # wait for an extra users.get request; a cached profile is only a bonus.
    profile = peek_cached_user_profile(event["user_id"]) or {}
    if not await vk_print.handle_event(session, event, profile=profile):
        log.warning(
            "VK ignored unknown message_event user=%s cmid=%s",
            event["user_id"],
            event["conversation_message_id"],
        )
        try:
            await vk_api.answer_message_event(
                session,
                event_id=event["event_id"],
                user_id=event["user_id"],
                peer_id=event["peer_id"],
                text="Кнопка устарела или больше неактивна",
            )
        except Exception:
            log.exception(
                "Could not acknowledge unknown VK message_event user=%s",
                event["user_id"],
            )


async def process_update_batch(
    session: aiohttp.ClientSession,
    updates: list,
    completed_update_ids: set[str],
) -> None:
    """Process a redeliverable VK batch without repeating completed updates."""
    for update in updates:
        message = incoming_private_message(update)
        if message is not None:
            update_id = provider_update_id(update, message)
            if update_id is not None and update_id in completed_update_ids:
                continue
            await route_message_update(session, update, message)
            if update_id is not None:
                completed_update_ids.add(update_id)
            continue

        event = incoming_private_event(update)
        if event is None:
            continue
        update_id = event_update_id(update, event)
        if update_id in completed_update_ids:
            continue
        await route_message_event(session, event)
        completed_update_ids.add(update_id)


async def poll_messages() -> None:
    """Long-poll VK alongside Telegram and route direct community messages."""
    if not vk_api.BOT_TOKEN or not vk_api.GROUP_USERNAME:
        log.warning("VK bot/token or community username is not configured")
        return

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                group_id = await vk_api.get_group_id(session)
                await vk_api.validate_long_poll(session, group_id)
                server, key, timestamp = await vk_api.get_long_poll_server(
                    session,
                    group_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("VK Long Poll initialization failed: %s", exc)
                await asyncio.sleep(5)
                continue

            log.info("VK Long Poll is ready for community=%s", group_id)
            consecutive_poll_errors = 0
            completed_update_ids: set[str] = set()
            while True:
                try:
                    payload = await vk_api.poll_long_poll(
                        session,
                        server,
                        key,
                        timestamp,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_poll_errors += 1
                    log.warning("VK Long Poll request failed: %s", exc)
                    if consecutive_poll_errors >= 3:
                        log.warning("VK Long Poll connection will be refreshed")
                        break
                    await asyncio.sleep(5)
                    continue

                consecutive_poll_errors = 0
                failure = payload.get("failed")
                new_timestamp = payload.get("ts")
                if failure == 1:
                    if not isinstance(new_timestamp, (str, int)):
                        log.warning("VK Long Poll lost history without a new timestamp")
                        break
                    timestamp = str(new_timestamp)
                    completed_update_ids.clear()
                    log.warning("VK Long Poll history was outdated; timestamp refreshed")
                    continue
                if failure in (2, 3):
                    log.warning("VK Long Poll credentials expired; refreshing connection")
                    break
                if failure is not None:
                    log.warning("VK Long Poll returned failure=%r", failure)
                    break

                updates = payload.get("updates")
                if (
                    not isinstance(updates, list)
                    or not isinstance(new_timestamp, (str, int))
                ):
                    log.warning("VK Long Poll returned a malformed update batch")
                    break

                try:
                    await process_update_batch(
                        session,
                        updates,
                        completed_update_ids,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Do not advance ts: VK will redeliver this batch after a
                    # transient database/API failure. bot_start_events makes
                    # a repeated deep-link update idempotent.
                    log.warning("VK update processing failed: %s", exc)
                    await asyncio.sleep(5)
                    continue

                timestamp = str(new_timestamp)
                completed_update_ids.clear()
