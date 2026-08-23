"""Low-level asynchronous transport for the VK community API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

import delivery_retry


BOT_TOKEN = os.environ.get("VK_TOKEN", "").strip()
GROUP_USERNAME = os.environ.get("VK_GROUP_USERNAME", "").strip().lstrip("@")
ADMIN_ID = os.environ.get("VK_ADMIN_ID", "").strip()
ARCHIVE_CHAT_ID = os.environ.get("VK_CHAT_ID", "").strip() or ADMIN_ID
API_VERSION = "5.199"
API_BASE = "https://api.vk.com/method/"
_RETRYABLE_API_ERROR_CODES = frozenset({1, 6, 10, 29})

log = logging.getLogger(__name__)


def is_admin(user_id: object) -> bool:
    return bool(
        ADMIN_ID
        and user_id is not None
        and str(user_id) == ADMIN_ID
    )


class VkApiError(RuntimeError):
    """Safe-to-log VK failure that never contains credentials or request URLs."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


async def _retry(
    operation: str,
    callback: Callable[[], Awaitable[Any]],
) -> Any:
    for attempt in range(1, delivery_retry.MAX_ATTEMPTS + 1):
        try:
            return await callback()
        except VkApiError as exc:
            if not exc.retryable or attempt >= delivery_retry.MAX_ATTEMPTS:
                raise
            log.warning(
                "VK %s retry attempt=%d/%d: %s",
                operation,
                attempt,
                delivery_retry.MAX_ATTEMPTS,
                exc,
            )
            await delivery_retry.wait_before_retry(
                attempt,
                retry_after=exc.retry_after,
            )
    raise AssertionError("unreachable VK retry state")


def community_link(*, ref: str) -> str:
    if not GROUP_USERNAME:
        raise RuntimeError("VK_GROUP_USERNAME не настроен")
    query = urlencode({"ref": ref})
    return f"https://vk.me/{quote(GROUP_USERNAME, safe='')}?{query}"


async def api_call(
    session: aiohttp.ClientSession,
    method: str,
    **params: Any,
) -> Any:
    if not BOT_TOKEN:
        raise VkApiError("VK_TOKEN не настроен")
    body = {
        **params,
        "access_token": BOT_TOKEN,
        "v": API_VERSION,
    }
    try:
        async with session.post(
            API_BASE + method,
            data=body,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            status = response.status
            raw = await response.read()
            headers = getattr(response, "headers", None)
    except VkApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise VkApiError(
            f"не удалось подключиться к VK API ({method})",
            retryable=True,
        ) from None

    if status != 200:
        raise VkApiError(
            f"VK API {method} вернул HTTP {status}",
            retryable=delivery_retry.retryable_http_status(status),
            retry_after=delivery_retry.retry_after_seconds(headers, raw),
        )
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise VkApiError(
            f"VK API {method} вернул некорректный JSON",
            retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise VkApiError(
            f"VK API {method} вернул неожиданный ответ",
            retryable=True,
        )
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("error_code", "unknown")
        raise VkApiError(
            f"VK API {method} отклонил запрос, код {code}",
            retryable=code in _RETRYABLE_API_ERROR_CODES,
        )
    if "response" not in payload:
        raise VkApiError(
            f"VK API {method} не вернул response",
            retryable=True,
        )
    return payload["response"]


def extract_group_id(response: Any) -> int:
    candidates: list[Any]
    if isinstance(response, list):
        candidates = response
    elif isinstance(response, dict):
        if isinstance(response.get("groups"), list):
            candidates = response["groups"]
        elif isinstance(response.get("items"), list):
            candidates = response["items"]
        else:
            candidates = [response]
    else:
        candidates = []

    for candidate in candidates:
        if isinstance(candidate, dict):
            group_id = candidate.get("id")
            if isinstance(group_id, int) and not isinstance(group_id, bool):
                return group_id
    raise VkApiError("VK API не вернул ID сообщества")


async def get_group_id(session: aiohttp.ClientSession) -> int:
    return extract_group_id(await api_call(session, "groups.getById"))


def _profile_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def extract_user_profile(response: Any, user_id: int) -> dict[str, str | None]:
    """Normalize one ``users.get`` response for the shared user model."""
    if not isinstance(response, list):
        raise VkApiError("VK API users.get вернул неожиданный ответ")
    for candidate in response:
        if not isinstance(candidate, dict) or candidate.get("id") != user_id:
            continue
        return {
            "username": _profile_text(candidate.get("screen_name")),
            "first_name": _profile_text(candidate.get("first_name")),
            "last_name": _profile_text(candidate.get("last_name")),
        }
    raise VkApiError("VK API users.get не вернул запрошенного пользователя")


async def get_user_profile(
    session: aiohttp.ClientSession,
    user_id: int,
) -> dict[str, str | None]:
    """Fetch names missing from Bots Long Poll's ``message_new`` payload."""
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
    ):
        raise ValueError("VK user_id must be a positive integer")

    async def fetch_once() -> dict[str, str | None]:
        response = await api_call(
            session,
            "users.get",
            user_ids=str(user_id),
            fields="screen_name",
        )
        return extract_user_profile(response, user_id)

    return await _retry("users.get", fetch_once)


async def validate_long_poll(
    session: aiohttp.ClientSession,
    group_id: int,
) -> None:
    settings = await api_call(
        session,
        "groups.getLongPollSettings",
        group_id=group_id,
    )
    if not isinstance(settings, dict) or settings.get("is_enabled") != 1:
        raise VkApiError("Bots Long Poll выключен в настройках сообщества")
    events = settings.get("events")
    if not isinstance(events, dict) or events.get("message_new") not in (1, True):
        raise VkApiError("событие message_new выключено в Bots Long Poll")
    if events.get("message_event") not in (1, True):
        raise VkApiError("событие message_event выключено в Bots Long Poll")


async def get_long_poll_server(
    session: aiohttp.ClientSession,
    group_id: int,
) -> tuple[str, str, str]:
    connection = await api_call(
        session,
        "groups.getLongPollServer",
        group_id=group_id,
    )
    if not isinstance(connection, dict):
        raise VkApiError("VK API не вернул параметры Long Poll")
    server = connection.get("server")
    key = connection.get("key")
    timestamp = connection.get("ts")
    if not all(isinstance(value, str) and value for value in (server, key, timestamp)):
        raise VkApiError("VK API вернул неполные параметры Long Poll")
    if urlsplit(server).scheme != "https":
        raise VkApiError("VK API вернул небезопасный Long Poll URL")
    return server, key, timestamp


async def poll_long_poll(
    session: aiohttp.ClientSession,
    server: str,
    key: str,
    timestamp: str,
    *,
    wait_seconds: int = 20,
) -> dict[str, Any]:
    try:
        async with session.get(
            server,
            params={
                "act": "a_check",
                "key": key,
                "ts": timestamp,
                "wait": wait_seconds,
            },
            timeout=aiohttp.ClientTimeout(total=wait_seconds + 10),
        ) as response:
            status = response.status
            try:
                payload = await response.json(content_type=None)
            except Exception:
                raise VkApiError(
                    "VK Long Poll вернул некорректный JSON"
                ) from None
    except VkApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise VkApiError(
            "не удалось подключиться к VK Long Poll"
        ) from None

    if status != 200:
        raise VkApiError(f"VK Long Poll вернул HTTP {status}")
    if not isinstance(payload, dict):
        raise VkApiError("VK Long Poll вернул неожиданный ответ")
    return payload


async def send_text(
    session: aiohttp.ClientSession,
    peer_id: int,
    text: str,
    keyboard: dict | None = None,
) -> int | None:
    params: dict[str, Any] = {
        "peer_id": peer_id,
        "random_id": secrets.randbelow(2_147_483_647) + 1,
        "message": text,
    }
    if keyboard:
        params["keyboard"] = json.dumps(
            keyboard,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return await _send_message(session, params)


def sent_message_id(response: Any) -> int | None:
    """Normalize the response variants returned by ``messages.send``."""
    if isinstance(response, int) and not isinstance(response, bool):
        return response if response > 0 else None
    if isinstance(response, dict):
        for key in ("conversation_message_id", "message_id", "id"):
            value = response.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


async def _send_message(
    session: aiohttp.ClientSession,
    params: dict[str, Any],
) -> int:
    """Retry messages.send with one stable random_id for deduplication."""
    async def send_once() -> int:
        response = await api_call(session, "messages.send", **params)
        message_id = sent_message_id(response)
        if message_id is None:
            raise VkApiError(
                "VK API messages.send не вернул корректный message ID",
                retryable=True,
            )
        return message_id

    return await _retry("messages.send", send_once)


def extract_message(response: Any, conversation_message_id: int) -> dict:
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise VkApiError(
            "VK API messages.getByConversationMessageId вернул неожиданный ответ"
        )
    for message in items:
        if (
            isinstance(message, dict)
            and message.get("conversation_message_id") == conversation_message_id
        ):
            return message
    raise VkApiError("VK API не вернул редактируемое сообщение")


async def get_message_by_cmid(
    session: aiohttp.ClientSession,
    peer_id: int,
    conversation_message_id: int,
) -> dict:
    async def fetch_once() -> dict:
        response = await api_call(
            session,
            "messages.getByConversationMessageId",
            peer_id=peer_id,
            conversation_message_ids=str(conversation_message_id),
        )
        return extract_message(response, conversation_message_id)

    return await _retry("messages.getByConversationMessageId", fetch_once)


def message_attachment_refs(message: dict) -> str | None:
    """Return reusable attachment references from a VK message object."""
    references: list[str] = []
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        kind = attachment.get("type")
        media = attachment.get(kind) if isinstance(kind, str) else None
        if kind not in {"photo", "doc", "video", "audio"} or not isinstance(
            media,
            dict,
        ):
            continue
        owner_id = media.get("owner_id")
        media_id = media.get("id")
        if (
            not isinstance(owner_id, int)
            or isinstance(owner_id, bool)
            or not isinstance(media_id, int)
            or isinstance(media_id, bool)
        ):
            continue
        reference = f"{kind}{owner_id}_{media_id}"
        access_key = media.get("access_key")
        if isinstance(access_key, str) and access_key:
            reference += f"_{access_key}"
        references.append(reference)
    return ",".join(references) or None


async def edit_message(
    session: aiohttp.ClientSession,
    peer_id: int,
    conversation_message_id: int,
    text: str,
    *,
    attachment: str | None = None,
) -> None:
    params: dict[str, Any] = {
        "peer_id": peer_id,
        "cmid": conversation_message_id,
        "message": text,
        "keyboard": json.dumps(
            {"buttons": []},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if attachment:
        params["attachment"] = attachment

    async def edit_once() -> None:
        response = await api_call(session, "messages.edit", **params)
        if response not in (1, True):
            raise VkApiError(
                "VK API messages.edit не подтвердил редактирование",
                retryable=True,
            )

    await _retry("messages.edit", edit_once)


async def answer_message_event(
    session: aiohttp.ClientSession,
    *,
    event_id: str,
    user_id: int,
    peer_id: int,
    text: str,
) -> None:
    event_data = json.dumps(
        {"type": "show_snackbar", "text": str(text or "")[:90]},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    async def answer_once() -> None:
        response = await api_call(
            session,
            "messages.sendMessageEventAnswer",
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data=event_data,
        )
        if response not in (1, True):
            raise VkApiError(
                "VK API не подтвердил callback-ответ",
                retryable=True,
            )

    await _retry("messages.sendMessageEventAnswer", answer_once)


def photo_attachment(saved_photo: Any) -> str:
    candidates: list[Any]
    if isinstance(saved_photo, list):
        candidates = saved_photo
    elif isinstance(saved_photo, dict) and isinstance(saved_photo.get("items"), list):
        candidates = saved_photo["items"]
    else:
        candidates = []

    for photo in candidates:
        if not isinstance(photo, dict):
            continue
        owner_id = photo.get("owner_id")
        photo_id = photo.get("id")
        if (
            not isinstance(owner_id, int)
            or isinstance(owner_id, bool)
            or not isinstance(photo_id, int)
            or isinstance(photo_id, bool)
        ):
            continue
        attachment = f"photo{owner_id}_{photo_id}"
        access_key = photo.get("access_key")
        if isinstance(access_key, str) and access_key:
            attachment += f"_{access_key}"
        return attachment
    raise VkApiError(
        "VK API не вернул сохранённую фотографию",
        retryable=True,
    )


def _normalize_uploaded_photo(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (list, dict)) and value:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    raise VkApiError(
        "VK upload не вернул фотографию",
        retryable=True,
    )


async def _upload_message_photo_once(
    session: aiohttp.ClientSession,
    peer_id: int,
    photo: bytes,
    *,
    filename: str,
    content_type: str,
) -> str:
    upload_server = await api_call(
        session,
        "photos.getMessagesUploadServer",
        peer_id=peer_id,
    )
    upload_url = (
        upload_server.get("upload_url")
        if isinstance(upload_server, dict)
        else None
    )
    if (
        not isinstance(upload_url, str)
        or urlsplit(upload_url).scheme != "https"
    ):
        raise VkApiError(
            "VK API не вернул безопасный URL загрузки фотографии",
            retryable=True,
        )

    form = aiohttp.FormData()
    form.add_field(
        "photo",
        photo,
        filename=filename,
        content_type=content_type,
    )
    try:
        async with session.post(
            upload_url,
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            status = response.status
            raw = await response.read()
            headers = getattr(response, "headers", None)
            response_type = str(
                headers.get("Content-Type", "") if headers is not None else ""
            ).split(";", 1)[0]
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise VkApiError(
            "не удалось загрузить фотографию в VK",
            retryable=True,
        ) from None

    if status != 200:
        raise VkApiError(
            "VK photo upload вернул "
            f"HTTP {status} content_type={response_type or 'unknown'} "
            f"bytes={len(raw)}",
            retryable=delivery_retry.retryable_http_status(status),
            retry_after=delivery_retry.retry_after_seconds(headers, raw),
        )
    try:
        uploaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise VkApiError(
            "VK photo upload вернул некорректный JSON "
            f"content_type={response_type or 'unknown'} bytes={len(raw)}",
            retryable=True,
        ) from exc
    if not isinstance(uploaded, dict):
        raise VkApiError(
            "VK photo upload вернул неожиданный тип ответа "
            f"type={type(uploaded).__name__}",
            retryable=True,
        )
    if uploaded.get("error") is not None:
        raise VkApiError(
            "VK photo upload отклонил фотографию",
            retryable=True,
        )

    upload_server_id = uploaded.get("server")
    upload_hash = uploaded.get("hash")
    if (
        not isinstance(upload_server_id, (str, int))
        or isinstance(upload_server_id, bool)
        or not isinstance(upload_hash, str)
        or not upload_hash
    ):
        raise VkApiError(
            "VK photo upload вернул неполные параметры "
            f"photo_type={type(uploaded.get('photo')).__name__} "
            f"server_type={type(upload_server_id).__name__} "
            f"hash_type={type(upload_hash).__name__}",
            retryable=True,
        )
    upload_photo = _normalize_uploaded_photo(uploaded.get("photo"))
    saved = await api_call(
        session,
        "photos.saveMessagesPhoto",
        photo=upload_photo,
        server=upload_server_id,
        hash=upload_hash,
    )
    return photo_attachment(saved)


async def upload_message_photo(
    session: aiohttp.ClientSession,
    peer_id: int,
    photo: bytes,
    *,
    filename: str = "event_access.png",
    content_type: str = "image/png",
) -> str:
    return await _retry(
        "photo upload",
        lambda: _upload_message_photo_once(
            session,
            peer_id,
            photo,
            filename=filename,
            content_type=content_type,
        ),
    )


async def send_photo(
    session: aiohttp.ClientSession,
    peer_id: int,
    photo: bytes,
    text: str,
    *,
    filename: str = "event_access.png",
    content_type: str = "image/png",
    keyboard: dict | None = None,
) -> int | None:
    attachment = await upload_message_photo(
        session,
        peer_id,
        photo,
        filename=filename,
        content_type=content_type,
    )
    params: dict[str, Any] = {
        "peer_id": peer_id,
        "random_id": secrets.randbelow(2_147_483_647) + 1,
        "message": text,
        "attachment": attachment,
    }
    if keyboard:
        params["keyboard"] = json.dumps(
            keyboard,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return await _send_message(session, params)


def document_attachment(saved_document: Any) -> str:
    """Build a VK ``doc`` attachment from docs.save response variants."""
    candidates: list[Any]
    if isinstance(saved_document, list):
        candidates = saved_document
    elif isinstance(saved_document, dict):
        nested = saved_document.get("doc")
        if isinstance(nested, dict):
            candidates = [nested]
        elif isinstance(saved_document.get("items"), list):
            candidates = saved_document["items"]
        else:
            candidates = [saved_document]
    else:
        candidates = []

    for document in candidates:
        if not isinstance(document, dict):
            continue
        owner_id = document.get("owner_id")
        document_id = document.get("id")
        if (
            not isinstance(owner_id, int)
            or isinstance(owner_id, bool)
            or not isinstance(document_id, int)
            or isinstance(document_id, bool)
        ):
            continue
        attachment = f"doc{owner_id}_{document_id}"
        access_key = document.get("access_key")
        if isinstance(access_key, str) and access_key:
            attachment += f"_{access_key}"
        return attachment
    raise VkApiError(
        "VK API не вернул сохранённый документ",
        retryable=True,
    )


async def _upload_message_document_once(
    session: aiohttp.ClientSession,
    peer_id: int,
    payload: bytes,
    *,
    filename: str,
    content_type: str,
) -> str:
    upload_server = await api_call(
        session,
        "docs.getMessagesUploadServer",
        peer_id=peer_id,
        type="doc",
    )
    upload_url = (
        upload_server.get("upload_url")
        if isinstance(upload_server, dict)
        else None
    )
    if (
        not isinstance(upload_url, str)
        or urlsplit(upload_url).scheme != "https"
    ):
        raise VkApiError(
            "VK API не вернул безопасный URL загрузки документа",
            retryable=True,
        )

    form = aiohttp.FormData()
    form.add_field(
        "file",
        payload,
        filename=filename,
        content_type=content_type,
    )
    try:
        async with session.post(
            upload_url,
            data=form,
            # Completed-session videos use this document path too and can be
            # much larger than command log/config exports.
            timeout=aiohttp.ClientTimeout(total=600, connect=30),
        ) as response:
            status = response.status
            raw = await response.read()
            headers = getattr(response, "headers", None)
            response_type = str(
                headers.get("Content-Type", "") if headers is not None else ""
            ).split(";", 1)[0]
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise VkApiError(
            "не удалось загрузить документ в VK",
            retryable=True,
        ) from None

    if status != 200:
        raise VkApiError(
            "VK document upload вернул "
            f"HTTP {status} content_type={response_type or 'unknown'} "
            f"bytes={len(raw)}",
            retryable=delivery_retry.retryable_http_status(status),
            retry_after=delivery_retry.retry_after_seconds(headers, raw),
        )
    try:
        uploaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise VkApiError(
            "VK document upload вернул некорректный JSON "
            f"content_type={response_type or 'unknown'} bytes={len(raw)}",
            retryable=True,
        ) from exc
    if not isinstance(uploaded, dict):
        raise VkApiError(
            "VK document upload вернул неожиданный тип ответа "
            f"type={type(uploaded).__name__}",
            retryable=True,
        )
    if uploaded.get("error") is not None:
        raise VkApiError(
            "VK document upload отклонил документ",
            retryable=True,
        )
    upload_file = uploaded.get("file")
    if not isinstance(upload_file, str) or not upload_file:
        raise VkApiError(
            "VK document upload не вернул файл "
            f"file_type={type(upload_file).__name__}",
            retryable=True,
        )

    saved = await api_call(
        session,
        "docs.save",
        file=upload_file,
        title=filename,
    )
    return document_attachment(saved)


async def upload_message_document(
    session: aiohttp.ClientSession,
    peer_id: int,
    payload: bytes,
    *,
    filename: str,
    content_type: str = "application/octet-stream",
) -> str:
    return await _retry(
        "document upload",
        lambda: _upload_message_document_once(
            session,
            peer_id,
            payload,
            filename=filename,
            content_type=content_type,
        ),
    )


async def send_documents(
    session: aiohttp.ClientSession,
    peer_id: int,
    documents: list[tuple[bytes, str, str]],
    *,
    text: str = "",
) -> int | None:
    if not 1 <= len(documents) <= 10:
        raise ValueError("VK message must contain 1-10 documents")
    attachments = []
    for payload, filename, content_type in documents:
        attachments.append(
            await upload_message_document(
                session,
                peer_id,
                payload,
                filename=filename,
                content_type=content_type,
            )
        )
    params: dict[str, Any] = {
        "peer_id": peer_id,
        "random_id": secrets.randbelow(2_147_483_647) + 1,
        "attachment": ",".join(attachments),
    }
    if text:
        params["message"] = text
    return await _send_message(session, params)


async def send_attachments(
    session: aiohttp.ClientSession,
    peer_id: int,
    attachments: list[str],
    text: str = "",
) -> int | None:
    """Send one VK message containing an arbitrary attachment mix."""
    if not 1 <= len(attachments) <= 10:
        raise ValueError("VK message must contain 1-10 attachments")
    if any(
        not isinstance(attachment, str) or not attachment
        for attachment in attachments
    ):
        raise ValueError("VK attachment reference must be a non-empty string")
    params: dict[str, Any] = {
        "peer_id": peer_id,
        "random_id": secrets.randbelow(2_147_483_647) + 1,
        "attachment": ",".join(attachments),
    }
    if text:
        params["message"] = text
    return await _send_message(session, params)


async def send_document(
    session: aiohttp.ClientSession,
    peer_id: int,
    payload: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
    *,
    caption: str = "",
) -> int | None:
    options = {"text": caption} if caption else {}
    return await send_documents(
        session,
        peer_id,
        [(payload, filename, content_type)],
        **options,
    )
