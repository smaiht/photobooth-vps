"""Telegram long-poll worker separated from application business logic."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp

import admin_command_service
import database
import event_access
import print_flow
import telegram_api
import telegram_print
from messaging import ReplyTarget


log = logging.getLogger(__name__)
TEMPORARY_UNAVAILABLE_MESSAGE = (
    "⚠️ Печать временно недоступна. Попробуйте чуть позже."
)

MessageHandler = Callable[
    [aiohttp.ClientSession, str, dict, dict],
    Awaitable[None],
]
CallbackHandler = Callable[
    [aiohttp.ClientSession, str, dict],
    Awaitable[None],
]


def parse_start_command(text: str) -> tuple[bool, str | None]:
    """Return ``(is_start, parameter)`` for Telegram's /start command."""
    parts = (text or "").strip().split(maxsplit=1)
    if not parts or parts[0].split("@", 1)[0].lower() != "/start":
        return False, None
    return True, parts[1] if len(parts) == 2 else None


async def record_start(update: dict, message: dict) -> bool:
    matched, parameter = parse_start_command(str(message.get("text") or ""))
    if not matched:
        return False
    sender = message.get("from") or {}
    user_id = sender.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        log.warning("TG /start ignored: sender id is missing")
        return True
    await database.record_bot_start(
        provider="telegram",
        provider_user_id=user_id,
        start_parameter=parameter,
        provider_update_id=update.get("update_id"),
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
    )
    log.info("TG /start stored for user=%s parameter=%r", user_id, parameter)
    return True


async def _access(
    message: dict,
) -> tuple[print_flow.PrintUser, str, bool, bool]:
    user = telegram_print.user_from_message(message)
    event_name, event_token, cafe_mode = event_access.current_event()
    allowed = await print_flow.user_has_access(
        user,
        event_token=event_token,
        cafe_mode=cafe_mode,
    )
    return user, event_name, cafe_mode, allowed


async def reply_to_start(
    session: aiohttp.ClientSession,
    base: str,
    message: dict,
) -> None:
    try:
        user, event_name, cafe_mode, allowed = await _access(message)
    except Exception as exc:
        log.warning("TG /start access check failed: %s", exc)
        chat_id = (message.get("chat") or {}).get("id")
        await telegram_api.send_text(
            session,
            base,
            chat_id,
            TEMPORARY_UNAVAILABLE_MESSAGE,
        )
        return
    if allowed:
        text = (
            "Отправьте фотографию, чтобы напечатать её."
            if cafe_mode
            else print_flow.connected_event_message(event_name)
        )
    else:
        text = print_flow.EVENT_ACCESS_REQUIRED_MESSAGE
    await telegram_api.send_text(
        session,
        base,
        user.conversation_id,
        text,
    )


async def reply_to_plain_message(
    session: aiohttp.ClientSession,
    base: str,
    message: dict,
) -> None:
    try:
        user, _event_name, _cafe_mode, allowed = await _access(message)
        await print_flow.ensure_user(user)
    except Exception as exc:
        log.warning("Could not resolve Telegram print access: %s", exc)
        chat_id = (message.get("chat") or {}).get("id")
        await telegram_api.send_text(
            session,
            base,
            chat_id,
            TEMPORARY_UNAVAILABLE_MESSAGE,
        )
        return
    text = (
        "Пришлите одну фотографию отдельным сообщением."
        if allowed
        else print_flow.EVENT_ACCESS_REQUIRED_MESSAGE
    )
    await telegram_api.send_text(
        session,
        base,
        user.conversation_id,
        text,
    )


async def route_message_update(
    session: aiohttp.ClientSession,
    base: str,
    update: dict,
    message: dict,
) -> None:
    """Route one Telegram message: start, photo, admin command, then text."""
    if await record_start(update, message):
        await reply_to_start(session, base, message)
        return
    if await telegram_print.handle_message(session, base, message):
        return

    user_id = (message.get("from") or {}).get("id")
    if telegram_api.is_admin(user_id):
        chat_id = (message.get("chat") or {}).get("id", user_id)
        await admin_command_service.handle_message(
            ReplyTarget("telegram", chat_id),
            str(message.get("text") or ""),
        )
        return
    await reply_to_plain_message(session, base, message)


async def route_callback_update(
    session: aiohttp.ClientSession,
    base: str,
    callback: dict,
) -> None:
    if await telegram_print.handle_callback(session, base, callback):
        return
    await telegram_api.answer_callback(
        session,
        base,
        callback.get("id"),
        "Эта кнопка больше недоступна.",
    )


async def dispatch_update(
    session: aiohttp.ClientSession,
    base: str,
    update: dict,
    message_handler: MessageHandler,
    callback_handler: CallbackHandler,
) -> None:
    """Translate one Telegram update into the matching adapter callback."""
    message = update.get("message")
    if isinstance(message, dict):
        await message_handler(session, base, update, message)
        return

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        await callback_handler(session, base, callback)


async def poll_updates(
    message_handler: MessageHandler | None = None,
    callback_handler: CallbackHandler | None = None,
) -> None:
    """Long-poll Telegram and confirm updates after their handlers finish."""
    if not telegram_api.BOT_TOKEN:
        log.warning("Telegram bot token is not configured")
        return

    message_handler = message_handler or route_message_update
    callback_handler = callback_handler or route_callback_update

    base = telegram_api.BOT_API_BASE
    offset = 0
    allowed_updates = ("message", "callback_query")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                data = await telegram_api.get_updates(
                    session,
                    base,
                    offset=offset,
                    allowed_updates=allowed_updates,
                )
                updates = data.get("result") if isinstance(data, dict) else None
                if not isinstance(updates, list):
                    raise ValueError("Telegram getUpdates returned invalid result")
                for update in updates:
                    if not isinstance(update, dict):
                        raise ValueError("Telegram returned an invalid update")
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int) or isinstance(update_id, bool):
                        raise ValueError("Telegram update_id is invalid")
                    await dispatch_update(
                        session,
                        base,
                        update,
                        message_handler,
                        callback_handler,
                    )
                    # Advance only after every durable handler side effect.
                    offset = update_id + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("TG poll error: %s", exc)
                await asyncio.sleep(5)
