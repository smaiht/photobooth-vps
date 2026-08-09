"""Low-level asynchronous transport for the Telegram Bot API."""

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

import aiohttp

import delivery_retry

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
BOT_USERNAME = os.environ.get("TG_BOT_USERNAME", "").strip().lstrip("@")
ADMIN_ID = os.environ.get("TG_ADMIN_ID", "").strip()
ARCHIVE_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
DEFAULT_SEND_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


def is_admin(user_id: object) -> bool:
    return bool(
        ADMIN_ID
        and user_id is not None
        and str(user_id) == ADMIN_ID
    )


async def _post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    operation: str,
    request_factory: Callable[[], dict[str, Any]],
    *,
    timeout: aiohttp.ClientTimeout | None = None,
) -> bytes | None:
    """POST one logical Telegram operation with at-least-once retries."""
    for attempt in range(1, delivery_retry.MAX_ATTEMPTS + 1):
        request = request_factory()
        request["timeout"] = timeout or DEFAULT_SEND_TIMEOUT
        try:
            async with session.post(url, **request) as response:
                status = response.status
                body = await response.read()
                headers = getattr(response, "headers", None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= delivery_retry.MAX_ATTEMPTS:
                log.warning(
                    "TG %s transport failed after %d attempts: %s",
                    operation,
                    attempt,
                    type(exc).__name__,
                )
                return None
            log.warning(
                "TG %s transport retry attempt=%d/%d error=%s",
                operation,
                attempt,
                delivery_retry.MAX_ATTEMPTS,
                type(exc).__name__,
            )
            await delivery_retry.wait_before_retry(attempt)
            continue

        if status == 200:
            return body
        if (
            operation == "editMessageCaption"
            and status == 400
            and _message_not_modified(body)
        ):
            return b'{"ok":true,"result":true}'
        retryable = delivery_retry.retryable_http_status(status)
        if retryable and attempt < delivery_retry.MAX_ATTEMPTS:
            log.warning(
                "TG %s HTTP %d; retry attempt=%d/%d",
                operation,
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
            "TG %s failed HTTP %d after %d attempt(s): %s",
            operation,
            status,
            attempt,
            delivery_retry.error_description(body) or "no description",
        )
        return None
    return None


def _decode_success(body: bytes | None, operation: str) -> dict | None:
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        log.warning("TG %s returned invalid success JSON", operation)
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        log.warning("TG %s returned an unsuccessful response", operation)
        return None
    return payload


def _message_not_modified(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    description = payload.get("description") if isinstance(payload, dict) else None
    return (
        isinstance(description, str)
        and "message is not modified" in description.lower()
    )


async def send_text(
    session: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    text: str,
    reply_markup: dict | None = None,
    *,
    parse_mode: str | None = None,
) -> bool:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    body = await _post_with_retry(
        session,
        f"{base}/sendMessage",
        "sendMessage",
        lambda: {"json": payload},
    )
    return _decode_success(body, "sendMessage") is not None


async def send_photo(
    session: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    photo: bytes,
    caption: str,
    reply_markup: dict | None,
    reply_to_message_id: int | None,
    *,
    filename: str = "print_options.jpg",
    content_type: str = "image/jpeg",
    parse_mode: str | None = "HTML",
) -> int | None:
    def request() -> dict[str, Any]:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        if parse_mode:
            form.add_field("parse_mode", parse_mode)
        form.add_field(
            "photo",
            photo,
            filename=filename,
            content_type=content_type,
        )
        if reply_markup:
            form.add_field(
                "reply_markup",
                json.dumps(
                    reply_markup,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        if isinstance(reply_to_message_id, int):
            form.add_field(
                "reply_parameters",
                json.dumps(
                    {"message_id": reply_to_message_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return {"data": form}

    body = _decode_success(
        await _post_with_retry(
            session,
            f"{base}/sendPhoto",
            "sendPhoto",
            request,
        ),
        "sendPhoto",
    )
    if body is not None:
        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
        ):
            log.warning("TG photo response has no valid message_id")
            return None
        return message_id
    return None


async def edit_print_caption(
    session: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    message_id: int,
    caption: str,
    *,
    caption_entities: list[dict] | None = None,
) -> bool:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "reply_markup": {"inline_keyboard": []},
    }
    if caption_entities:
        payload["caption_entities"] = caption_entities
    body = await _post_with_retry(
        session,
        f"{base}/editMessageCaption",
        "editMessageCaption",
        lambda: {"json": payload},
    )
    return _decode_success(body, "editMessageCaption") is not None


async def answer_callback(
    session: aiohttp.ClientSession,
    base: str,
    callback_id: str | None,
    text: str = "",
    show_alert: bool = False,
) -> None:
    if not callback_id:
        return
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = bool(show_alert)
    body = await _post_with_retry(
        session,
        f"{base}/answerCallbackQuery",
        "answerCallbackQuery",
        lambda: {"json": payload},
    )
    _decode_success(body, "answerCallbackQuery")


async def get_updates(
    session: aiohttp.ClientSession,
    base: str,
    *,
    offset: int,
    allowed_updates: list[str] | tuple[str, ...],
    poll_timeout: int = 10,
) -> dict:
    try:
        async with session.get(
            f"{base}/getUpdates",
            params={
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": json.dumps(allowed_updates),
            },
            timeout=aiohttp.ClientTimeout(total=poll_timeout + 5),
        ) as response:
            return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"Telegram getUpdates transport error ({type(exc).__name__})"
        ) from None


async def download_file(
    session: aiohttp.ClientSession,
    base: str,
    file_id: str,
    *,
    max_size: int,
) -> bytes:
    try:
        async with session.post(
            f"{base}/getFile",
            json={"file_id": file_id},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            body = await response.json()
            if response.status != 200 or not body.get("ok"):
                raise RuntimeError("Telegram не отдал файл")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"Telegram getFile transport error ({type(exc).__name__})"
        ) from None
    file_path = (body.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError("Telegram не вернул путь к файлу")

    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    payload = bytearray()
    try:
        async with session.get(
            download_url,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Telegram download HTTP {response.status}")
            async for chunk in response.content.iter_chunked(1024 * 1024):
                payload.extend(chunk)
                if len(payload) > max_size:
                    raise ValueError(
                        f"файл больше {max_size // 1048576} МБ"
                    )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"Telegram file download transport error ({type(exc).__name__})"
        ) from None
    if not payload:
        raise ValueError("Telegram прислал пустой файл")
    return bytes(payload)


async def send_document(
    chat_id: str | int,
    payload: bytes,
    filename: str,
    content_type: str,
) -> bool:
    if not BOT_TOKEN:
        return False

    def request() -> dict[str, Any]:
        form = aiohttp.FormData()
        # Multipart fields accept text/bytes, unlike Telegram JSON where an
        # integer chat_id is valid.
        form.add_field("chat_id", str(chat_id))
        form.add_field(
            "document",
            payload,
            filename=filename,
            content_type=content_type,
        )
        return {"data": form}

    async with aiohttp.ClientSession() as session:
        body = await _post_with_retry(
            session,
            f"{BOT_API_BASE}/sendDocument",
            "sendDocument",
            request,
            timeout=aiohttp.ClientTimeout(total=60),
        )
    return _decode_success(body, "sendDocument") is not None


async def send_documents(
    chat_id: str | int,
    documents: list[tuple[bytes, str, str]],
) -> bool:
    """Send two or more separate documents in one Telegram media group."""
    if not BOT_TOKEN or len(documents) < 2:
        return False

    def request() -> dict[str, Any]:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        media = []
        for index, (payload, filename, content_type) in enumerate(documents):
            field_name = f"document_{index}"
            form.add_field(
                field_name,
                payload,
                filename=filename,
                content_type=content_type,
            )
            media.append({
                "type": "document",
                "media": f"attach://{field_name}",
            })
        form.add_field("media", json.dumps(media, ensure_ascii=False))
        return {"data": form}

    async with aiohttp.ClientSession() as session:
        body = await _post_with_retry(
            session,
            f"{BOT_API_BASE}/sendMediaGroup",
            "sendMediaGroup",
            request,
            timeout=aiohttp.ClientTimeout(total=60),
        )
    return _decode_success(body, "sendMediaGroup") is not None
