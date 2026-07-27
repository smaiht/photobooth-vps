"""Low-level asynchronous transport for the VK community API."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import aiohttp


BOT_TOKEN = os.environ.get("VK_TOKEN", "").strip()
GROUP_USERNAME = os.environ.get("VK_GROUP_USERNAME", "").strip().lstrip("@")
ADMIN_ID = os.environ.get("VK_ADMIN_ID", "").strip()
API_VERSION = "5.199"
API_BASE = "https://api.vk.com/method/"


def is_admin(user_id: object) -> bool:
    return bool(
        ADMIN_ID
        and user_id is not None
        and str(user_id) == ADMIN_ID
    )


class VkApiError(RuntimeError):
    """Safe-to-log VK failure that never contains credentials or request URLs."""


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
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:
                raise VkApiError(
                    f"VK API {method} вернул некорректный JSON"
                ) from exc
    except VkApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise VkApiError(f"не удалось подключиться к VK API ({method})") from exc

    if status != 200:
        raise VkApiError(f"VK API {method} вернул HTTP {status}")
    if not isinstance(payload, dict):
        raise VkApiError(f"VK API {method} вернул неожиданный ответ")
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("error_code", "unknown")
        raise VkApiError(f"VK API {method} отклонил запрос, код {code}")
    if "response" not in payload:
        raise VkApiError(f"VK API {method} не вернул response")
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
            except Exception as exc:
                raise VkApiError("VK Long Poll вернул некорректный JSON") from exc
    except VkApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise VkApiError("не удалось подключиться к VK Long Poll") from exc

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
    response = await api_call(
        session,
        "messages.send",
        **params,
    )
    return sent_message_id(response)


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
    raise VkApiError("VK API не вернул сохранённую фотографию")


async def upload_message_photo(
    session: aiohttp.ClientSession,
    peer_id: int,
    photo: bytes,
    *,
    filename: str = "event_access.png",
    content_type: str = "image/png",
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
    if not isinstance(upload_url, str) or urlsplit(upload_url).scheme != "https":
        raise VkApiError("VK API не вернул безопасный URL загрузки фотографии")

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
            try:
                uploaded = await response.json(content_type=None)
            except Exception as exc:
                raise VkApiError("VK upload вернул некорректный JSON") from exc
    except VkApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise VkApiError("не удалось загрузить фотографию в VK") from exc

    if status != 200:
        raise VkApiError(f"VK upload вернул HTTP {status}")
    if not isinstance(uploaded, dict):
        raise VkApiError("VK upload вернул неожиданный ответ")
    upload_photo = uploaded.get("photo")
    upload_server_id = uploaded.get("server")
    upload_hash = uploaded.get("hash")
    if (
        not isinstance(upload_photo, str)
        or not upload_photo
        or not isinstance(upload_server_id, (str, int))
        or isinstance(upload_server_id, bool)
        or not isinstance(upload_hash, str)
        or not upload_hash
    ):
        raise VkApiError("VK upload вернул неполные параметры фотографии")

    saved = await api_call(
        session,
        "photos.saveMessagesPhoto",
        photo=upload_photo,
        server=upload_server_id,
        hash=upload_hash,
    )
    return photo_attachment(saved)


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
    response = await api_call(
        session,
        "messages.send",
        **params,
    )
    return sent_message_id(response)


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
    raise VkApiError("VK API не вернул сохранённый документ")


async def upload_message_document(
    session: aiohttp.ClientSession,
    peer_id: int,
    payload: bytes,
    *,
    filename: str,
    content_type: str = "application/octet-stream",
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
    if not isinstance(upload_url, str) or urlsplit(upload_url).scheme != "https":
        raise VkApiError("VK API не вернул безопасный URL загрузки документа")

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
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            status = response.status
            try:
                uploaded = await response.json(content_type=None)
            except Exception as exc:
                raise VkApiError("VK document upload вернул некорректный JSON") from exc
    except VkApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise VkApiError("не удалось загрузить документ в VK") from exc

    if status != 200:
        raise VkApiError(f"VK document upload вернул HTTP {status}")
    if not isinstance(uploaded, dict):
        raise VkApiError("VK document upload вернул неожиданный ответ")
    upload_file = uploaded.get("file")
    if not isinstance(upload_file, str) or not upload_file:
        raise VkApiError("VK document upload не вернул файл")

    saved = await api_call(
        session,
        "docs.save",
        file=upload_file,
        title=filename,
    )
    return document_attachment(saved)


async def send_documents(
    session: aiohttp.ClientSession,
    peer_id: int,
    documents: list[tuple[bytes, str, str]],
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
    response = await api_call(
        session,
        "messages.send",
        peer_id=peer_id,
        random_id=secrets.randbelow(2_147_483_647) + 1,
        attachment=",".join(attachments),
    )
    return sent_message_id(response)


async def send_document(
    session: aiohttp.ClientSession,
    peer_id: int,
    payload: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
) -> int | None:
    return await send_documents(
        session,
        peer_id,
        [(payload, filename, content_type)],
    )
