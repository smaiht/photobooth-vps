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

import ai_flow
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


def user_from_message(
    message: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> print_flow.PrintUser:
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
    profile = profile if isinstance(profile, dict) else {}
    return print_flow.PrintUser(
        provider="vk",
        provider_user_id=user_id,
        conversation_id=peer_id,
        source_message_id=source_message_id,
        username=profile.get("username"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
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
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise RuntimeError("не удалось скачать изображение из VK") from None
    except (ValueError, RuntimeError):
        raise
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
                        "type": "callback",
                        "label": print_flow.PRINT_CHOICE_LABELS["fit"],
                        "payload": _payload("print_choice", "fit", job_id),
                    },
                    "color": "primary",
                },
                {
                    "action": {
                        "type": "callback",
                        "label": print_flow.PRINT_CHOICE_LABELS["fill"],
                        "payload": _payload("print_choice", "fill", job_id),
                    },
                    "color": "primary",
                },
            ],
            [{
                "action": {
                    "type": "callback",
                    "label": print_flow.PRINT_CHOICE_LABELS["cancel"],
                    "payload": _payload("print_choice", "cancel", job_id),
                },
                "color": "negative",
            }],
        ],
    }


def _ai_choice_keyboard(job_id: str, templates: tuple[dict, ...]) -> dict:
    buttons = [
        [{
            "action": {
                "type": "callback",
                "label": template["button"],
                "payload": _payload("ai_template", template["id"], job_id),
            },
            "color": "primary",
        }]
        for template in templates
    ]
    buttons.append([{
        "action": {
            "type": "callback",
            "label": "❌ ОТМЕНА",
            "payload": _payload("ai_cancel", "cancel", job_id),
        },
        "color": "negative",
    }])
    return {"inline": True, "buttons": buttons}


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

    async def send_ai_choice(
        self,
        upload: ai_flow.AiUpload,
        preview: bytes,
        job_id: str,
        templates: tuple[dict, ...],
    ) -> int | None:
        return await vk_api.send_photo(
            self.session,
            int(upload.user.conversation_id),
            preview,
            "✨ Выберите AI-эффект:",
            filename="ai_source.jpg",
            content_type="image/jpeg",
            keyboard=_ai_choice_keyboard(job_id, templates),
        )

    async def acknowledge(
        self,
        action: print_flow.PrintAction,
        text: str,
        *,
        alert: bool = False,
    ) -> None:
        event = action.context.get("vk_message_event")
        if isinstance(event, dict):
            await vk_api.answer_message_event(
                self.session,
                event_id=str(event["event_id"]),
                user_id=int(event["user_id"]),
                peer_id=int(event["peer_id"]),
                text=text,
            )
            return
        # Compatibility for cards sent before callback buttons were deployed.
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
        await self._edit_callback_card(action, text)

    async def update_admin(
        self,
        action: print_flow.PrintAction,
        status: str,
    ) -> None:
        if await self._edit_callback_card(action, status):
            return
        # Compatibility for an old text-button action without the source cmid.
        await vk_api.send_text(
            self.session,
            int(action.user.conversation_id),
            status,
        )

    async def _edit_callback_card(
        self,
        action: print_flow.PrintAction,
        status: str,
    ) -> bool:
        event = action.context.get("vk_message_event")
        if not isinstance(event, dict):
            return False
        peer_id = int(event["peer_id"])
        cmid = int(event["conversation_message_id"])
        message = await vk_api.get_message_by_cmid(self.session, peer_id, cmid)
        caption = str(message.get("text") or "").strip()
        edited_caption = f"{caption}\n\n{status}" if caption else status
        await vk_api.edit_message(
            self.session,
            peer_id,
            cmid,
            edited_caption,
            attachment=vk_api.message_attachment_refs(message),
        )
        return True


def user_from_event(
    event: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> print_flow.PrintUser:
    user_id = event.get("user_id")
    peer_id = event.get("peer_id")
    cmid = event.get("conversation_message_id")
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
    ):
        raise ValueError("VK не передал корректный callback")
    profile = profile if isinstance(profile, dict) else {}
    admin = vk_api.is_admin(user_id)
    return print_flow.PrintUser(
        provider="vk",
        provider_user_id=user_id,
        conversation_id=peer_id,
        source_message_id=cmid,
        username=profile.get("username"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
        allowlisted=admin,
        is_admin=admin,
        metadata={"vk_message_event_id": event.get("event_id")},
    )


def _action_payload(raw_payload) -> dict | None:
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        return None
    return payload if isinstance(payload, dict) else None


def _action_parts(payload: dict) -> tuple[str, str, str] | None:
    kind = payload.get("type")
    action_name = payload.get("action")
    job_id = payload.get("job_id")
    if kind == "print_choice":
        valid_action = action_name in {"fit", "fill", "cancel"}
    elif kind == "print_admin":
        valid_action = action_name in {"approve", "reject"}
    elif kind == "ai_template":
        valid_action = ai_flow.valid_template_id(action_name)
    elif kind == "ai_cancel":
        valid_action = action_name == "cancel"
    elif kind == "ai_print":
        valid_action = action_name == "print"
    else:
        valid_action = False
    if not valid_action or not isinstance(job_id, str):
        return None
    return str(kind), str(action_name), job_id


def parse_action(
    message: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> tuple[str, print_flow.PrintAction | ai_flow.AiAction] | None:
    payload = _action_payload(message.get("payload"))
    if payload is None:
        return None
    parts = _action_parts(payload)
    if parts is None:
        return None
    kind, action_name, job_id = parts
    try:
        action_id = (
            str(message.get("conversation_message_id"))
            if message.get("conversation_message_id") is not None
            else None
        )
        user = user_from_message(message, profile=profile)
        if kind.startswith("ai_"):
            action = ai_flow.AiAction(
                user=user,
                action="template" if kind == "ai_template" else action_name,
                template_id=action_name if kind == "ai_template" else None,
                job_id=job_id,
                action_id=action_id,
                context=message,
            )
        else:
            action = print_flow.PrintAction(
                user=user,
                action=action_name,
                job_id=job_id,
                action_id=action_id,
                context=message,
            )
    except ValueError:
        return None
    return str(kind), action


def parse_event_action(
    event: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> tuple[str, print_flow.PrintAction | ai_flow.AiAction] | None:
    payload = _action_payload(event.get("payload"))
    if payload is None:
        return None
    parts = _action_parts(payload)
    if parts is None:
        return None
    kind, action_name, job_id = parts
    try:
        user = user_from_event(event, profile=profile)
        if kind.startswith("ai_"):
            action = ai_flow.AiAction(
                user=user,
                action="template" if kind == "ai_template" else action_name,
                template_id=action_name if kind == "ai_template" else None,
                job_id=job_id,
                action_id=str(event.get("event_id") or "") or None,
                context={"vk_message_event": event},
            )
        else:
            action = print_flow.PrintAction(
                user=user,
                action=action_name,
                job_id=job_id,
                action_id=str(event.get("event_id") or "") or None,
                context={"vk_message_event": event},
            )
    except ValueError:
        return None
    return kind, action


async def handle_action(
    session: aiohttp.ClientSession,
    message: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> bool:
    parsed = parse_action(message, profile=profile)
    if parsed is None:
        return False
    kind, action = parsed
    ui = VkPrintUI(session)
    if kind == "print_choice":
        return await print_flow.handle_choice(action, ui)
    if kind == "print_admin":
        return await print_flow.handle_admin_action(action, ui)
    return await ai_flow.handle_action(action, ui)


async def handle_event(
    session: aiohttp.ClientSession,
    event: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> bool:
    parsed = parse_event_action(event, profile=profile)
    if parsed is None:
        return False
    kind, action = parsed
    ui = VkPrintUI(session)
    if kind == "print_choice":
        return await print_flow.handle_choice(action, ui)
    if kind == "print_admin":
        return await print_flow.handle_admin_action(action, ui)
    return await ai_flow.handle_action(action, ui)


async def handle_message(
    session: aiohttp.ClientSession,
    message: dict,
    *,
    profile: dict[str, str | None] | None = None,
) -> bool:
    attachments = message.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return False
    user = user_from_message(message, profile=profile)
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

    ui = VkPrintUI(session)
    if ai_flow.is_ai_caption(message.get("text")):
        handled = await ai_flow.handle_upload(
            ai_flow.AiUpload(
                user=user,
                suffix=image.suffix,
                declared_size=image.declared_size,
                download=download,
            ),
            ui,
        )
        if handled:
            return True
    upload = print_flow.PrintUpload(
        user=user,
        suffix=image.suffix,
        declared_size=image.declared_size,
        download=download,
        metadata=image.metadata,
    )
    return await print_flow.handle_upload(upload, ui)
