"""VK community bot routing and Bots Long Poll worker."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

import aiohttp

import admin_command_service
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
_user_profile_cache: OrderedDict[
    int,
    tuple[float, dict[str, str | None]],
] = OrderedDict()


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


def provider_update_id(update: dict, message: dict) -> str | None:
    event_id = update.get("event_id")
    if event_id is not None and str(event_id).strip():
        return str(event_id)

    peer_id = message.get("peer_id")
    message_id = message.get("conversation_message_id") or message.get("id")
    if peer_id is None or message_id is None:
        return None
    return f"message:{peer_id}:{message_id}"


async def cached_user_profile(
    session: aiohttp.ClientSession,
    user_id: int,
) -> dict[str, str | None]:
    """Resolve a VK profile best-effort and avoid one API call per button."""
    now = time.monotonic()
    cached = _user_profile_cache.get(user_id)
    if cached is not None:
        expires_at, profile = cached
        if expires_at > now:
            _user_profile_cache.move_to_end(user_id)
            return dict(profile)
        del _user_profile_cache[user_id]

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
        now + USER_PROFILE_CACHE_TTL_SECONDS,
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


async def route_message_update(
    session: aiohttp.ClientSession,
    update: dict,
    message: dict,
) -> None:
    profile = await cached_user_profile(session, message["from_id"])
    started = await record_start(update, message, profile=profile)
    if started:
        await reply_for_current_event(session, message)

    # VK keyboard text buttons are regular message_new updates. Route their
    # signed job payload before treating visible button labels as commands.
    if await vk_print.handle_action(session, message, profile=profile):
        return
    if await vk_print.handle_message(session, message, profile=profile):
        return

    text = str(message.get("text") or "")
    if vk_api.is_admin(message["from_id"]) and (text.strip() or not started):
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
    await reply_for_current_event(session, message)


async def process_update_batch(
    session: aiohttp.ClientSession,
    updates: list,
    completed_update_ids: set[str],
) -> None:
    """Process a redeliverable VK batch without repeating completed updates."""
    for update in updates:
        message = incoming_private_message(update)
        if message is None:
            continue
        update_id = provider_update_id(update, message)
        if update_id is not None and update_id in completed_update_ids:
            continue
        await route_message_update(session, update, message)
        if update_id is not None:
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
