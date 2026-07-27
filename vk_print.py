"""VK-specific image extraction and presentation for the shared print flow."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

import print_flow
import print_media
import vk_api


log = logging.getLogger(__name__)

SAFE_FILENAME_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class VkImage:
    url: str
    suffix: str
    declared_size: int | None
    metadata: dict


def user_from_message(message: dict) -> print_flow.PrintUser:
    user_id = message.get("from_id")
    peer_id = message.get("peer_id")
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or not isinstance(peer_id, int)
        or isinstance(peer_id, bool)
        or peer_id <= 0
    ):
        raise ValueError("VK не передал корректного отправителя")
    source_message_id = (
        message.get("conversation_message_id")
        if message.get("conversation_message_id") is not None
        else message.get("id")
    )
    admin = vk_api.is_admin(user_id)
    return print_flow.PrintUser(
        provider="vk",
        provider_user_id=user_id,
        conversation_id=peer_id,
        source_message_id=source_message_id,
        allowlisted=admin,
        is_admin=admin,
        metadata={
            "vk_message_id": message.get("id"),
            "vk_conversation_message_id": message.get("conversation_message_id"),
            "vk_date": message.get("date"),
            "vk_text": str(message.get("text") or "")[:1024],
        },
    )


def _safe_https_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return value


def _photo_image(photo: dict) -> VkImage | None:
    candidates = []
    sizes = photo.get("sizes")
    if isinstance(sizes, list):
        candidates.extend(item for item in sizes if isinstance(item, dict))
    original = photo.get("orig_photo")
    if isinstance(original, dict):
        candidates.append(original)
    valid = []
    for item in candidates:
        url = _safe_https_url(item.get("url") or item.get("src"))
        if url:
            valid.append((
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("width") or 0),
                int(item.get("height") or 0),
                url,
            ))
    if not valid:
        return None
    _area, width, height, url = max(valid)
    return VkImage(
        url=url,
        suffix=".jpg",
        declared_size=None,
        metadata={
            "source_filename": "vk_photo.jpg",
            "vk_source_kind": "photo",
            "vk_attachment_owner_id": photo.get("owner_id"),
            "vk_attachment_id": photo.get("id"),
            "vk_attachment_width": width,
            "vk_attachment_height": height,
        },
    )


def _document_image(document: dict) -> VkImage | None:
    title = SAFE_FILENAME_RE.sub(
        "",
        str(document.get("title") or "vk_image"),
    )[:200]
    ext = str(document.get("ext") or Path(title).suffix.lstrip(".")).lower()
    suffix = print_media.suffix_from_extension(ext)
    url = _safe_https_url(document.get("url"))
    if not suffix or not url:
        return None
    size = document.get("size")
    declared_size = (
        int(size)
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0
        else None
    )
    if not Path(title).suffix:
        title = f"{title}{suffix}"
    return VkImage(
        url=url,
        suffix=suffix,
        declared_size=declared_size,
        metadata={
            "source_filename": title or f"vk_image{suffix}",
            "vk_source_kind": "document",
            "vk_attachment_owner_id": document.get("owner_id"),
            "vk_attachment_id": document.get("id"),
            "vk_document_type": document.get("type"),
            "vk_document_ext": ext,
        },
    )


def extract_image(message: dict) -> VkImage | None:
    """Return the only printable VK attachment and reject image albums."""
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return None
    images: list[VkImage] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        kind = attachment.get("type")
        body = attachment.get(kind) if isinstance(kind, str) else None
        if not isinstance(body, dict):
            continue
        image = (
            _photo_image(body)
            if kind == "photo"
            else _document_image(body) if kind == "doc" else None
        )
        if image is not None:
            images.append(image)
    if len(images) > 1:
        raise ValueError(
            "альбомы пока не печатаются; пришлите одно изображение сообщением"
        )
    return images[0] if images else None


async def download_image(
    session: aiohttp.ClientSession,
    image: VkImage,
) -> bytes:
    """Download one signed VK CDN URL without ever logging that URL."""
    payload = bytearray()
    try:
        async with session.get(
            image.url,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"VK download вернул HTTP {response.status}")
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError:
                    content_length = 0
                if content_length > print_media.MAX_PRINT_FILE_SIZE:
                    raise ValueError(
                        "файл больше "
                        f"{print_media.MAX_PRINT_FILE_SIZE_MB} МБ"
                    )
            async for chunk in response.content.iter_chunked(1024 * 1024):
                payload.extend(chunk)
                if len(payload) > print_media.MAX_PRINT_FILE_SIZE:
                    raise ValueError(
                        "файл больше "
                        f"{print_media.MAX_PRINT_FILE_SIZE_MB} МБ"
                    )
    except (ValueError, RuntimeError):
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError("не удалось скачать изображение из VK") from exc
    if not payload:
        raise ValueError("VK прислал пустой файл")
    return bytes(payload)


def _payload(kind: str, action: str, job_id: str) -> str:
    return json.dumps(
        {"type": kind, "action": action, "job_id": job_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _choice_keyboard(job_id: str) -> dict:
    return {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": print_flow.PRINT_CHOICE_LABELS["fit"],
                        "payload": _payload("print_choice", "fit", job_id),
                    },
                    "color": "primary",
                },
                {
                    "action": {
                        "type": "text",
                        "label": print_flow.PRINT_CHOICE_LABELS["fill"],
                        "payload": _payload("print_choice", "fill", job_id),
                    },
                    "color": "primary",
                },
            ],
            [{
                "action": {
                    "type": "text",
                    "label": print_flow.PRINT_CHOICE_LABELS["cancel"],
                    "payload": _payload("print_choice", "cancel", job_id),
                },
                "color": "negative",
            }],
        ],
    }


class VkPrintUI:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def send_text(self, user: print_flow.PrintUser, text: str) -> bool:
        message_id = await vk_api.send_text(
            self.session,
            int(user.conversation_id),
            text,
        )
        return message_id is not None

    async def send_choice(
        self,
        upload: print_flow.PrintUpload,
        preview: bytes,
        job_id: str,
    ) -> int | None:
        caption = print_flow.print_choice_message(telegram_html=False)
        return await vk_api.send_photo(
            self.session,
            int(upload.user.conversation_id),
            preview,
            caption,
            filename="print_options.jpg",
            content_type="image/jpeg",
            keyboard=_choice_keyboard(job_id),
        )

    async def acknowledge(
        self,
        action: print_flow.PrintAction,
        text: str,
        *,
        alert: bool = False,
    ) -> None:
        # VK text buttons arrive as ordinary message_new events, so the reply
        # itself is their acknowledgement. Durable DB transitions handle repeats.
        await vk_api.send_text(
            self.session,
            int(action.user.conversation_id),
            text,
        )

    async def update_choice(
        self,
        action: print_flow.PrintAction,
        text: str,
    ) -> None:
        # The acknowledgement above is visible in VK. Keeping this a no-op
        # avoids duplicate messages; stale buttons remain safe and idempotent.
        return None

    async def update_admin(
        self,
        action: print_flow.PrintAction,
        status: str,
    ) -> None:
        await vk_api.send_text(
            self.session,
            int(action.user.conversation_id),
            status,
        )


def parse_action(message: dict) -> tuple[str, print_flow.PrintAction] | None:
    raw_payload = message.get("payload")
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    action_name = payload.get("action")
    job_id = payload.get("job_id")
    allowed = (
        {"fit", "fill", "cancel"}
        if kind == "print_choice"
        else {"approve", "reject"} if kind == "print_admin" else set()
    )
    if action_name not in allowed or not isinstance(job_id, str):
        return None
    try:
        action = print_flow.PrintAction(
            user=user_from_message(message),
            action=action_name,
            job_id=job_id,
            action_id=(
                str(message.get("conversation_message_id"))
                if message.get("conversation_message_id") is not None
                else None
            ),
            context=message,
        )
    except ValueError:
        return None
    return str(kind), action


async def handle_action(
    session: aiohttp.ClientSession,
    message: dict,
) -> bool:
    parsed = parse_action(message)
    if parsed is None:
        return False
    kind, action = parsed
    ui = VkPrintUI(session)
    if kind == "print_choice":
        return await print_flow.handle_choice(action, ui)
    return await print_flow.handle_admin_action(action, ui)


async def handle_message(
    session: aiohttp.ClientSession,
    message: dict,
) -> bool:
    attachments = message.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return False
    user = user_from_message(message)
    try:
        image = extract_image(message)
    except ValueError as exc:
        await vk_api.send_text(
            session,
            int(user.conversation_id),
            print_flow.rejected_photo_message(exc),
        )
        return True
    if image is None:
        return False

    async def download() -> bytes:
        return await download_image(session, image)

    upload = print_flow.PrintUpload(
        user=user,
        suffix=image.suffix,
        declared_size=image.declared_size,
        download=download,
        metadata=image.metadata,
    )
    return await print_flow.handle_upload(upload, VkPrintUI(session))
