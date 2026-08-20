"""Telegram-specific photo extraction and print workflow presentation."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import aiohttp

import ai_flow
import print_flow
import print_media
import telegram_api


log = logging.getLogger(__name__)

LEGACY_TELEGRAM_PRINT_ALLOWLIST = frozenset({"6634566969", "5683598562"})

SAFE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
CHOICE_RE = re.compile(r"^print:(fit|fill|cancel):([a-f0-9]{32})$")
ADMIN_RE = re.compile(r"^print_admin:(approve|reject):([a-f0-9]{32})$")
AI_TEMPLATE_RE = re.compile(
    r"^ai:t:([a-z0-9][a-z0-9_-]{0,19}):([a-f0-9]{32})$"
)
AI_ACTION_RE = re.compile(r"^ai:(c|p):([a-f0-9]{32})$")

_rejected_media_groups: set[str] = set()


def is_allowlisted(user_id) -> bool:
    return telegram_api.is_admin(user_id) or (
        user_id is not None
        and str(user_id) in LEGACY_TELEGRAM_PRINT_ALLOWLIST
    )


def _user_id(sender: dict) -> int:
    user_id = sender.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("Telegram не передал ID пользователя")
    return user_id


def _sender_metadata(message: dict) -> dict:
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    document = message.get("document") or {}
    filename = re.sub(
        r"[\x00-\x1f\x7f]",
        "",
        str(document.get("file_name") or "telegram_photo.jpg"),
    )[:200]
    return {
        "source_filename": filename,
        "telegram_mime_type": str(document.get("mime_type") or ""),
        "telegram_document": dict(document),
        "telegram_user": dict(sender),
        "telegram_chat": dict(chat),
        "telegram_message_id": message.get("message_id"),
        "telegram_message_date": message.get("date"),
        "telegram_caption": str(message.get("caption") or "")[:1024],
        "telegram_source_kind": "photo" if message.get("photo") else "document",
    }


def user_from_message(message: dict) -> print_flow.PrintUser:
    sender = message.get("from") or {}
    user_id = _user_id(sender)
    chat_id = (message.get("chat") or {}).get("id", user_id)
    return print_flow.PrintUser(
        provider="telegram",
        provider_user_id=user_id,
        conversation_id=chat_id,
        source_message_id=message.get("message_id"),
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
        allowlisted=is_allowlisted(user_id),
        is_admin=telegram_api.is_admin(user_id),
        metadata=_sender_metadata(message),
    )


def _print_file(message: dict) -> tuple[str, str, int | None] | None:
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = max(
            (item for item in photos if isinstance(item, dict)),
            key=lambda item: (
                int(item.get("file_size") or 0),
                int(item.get("width") or 0) * int(item.get("height") or 0),
            ),
            default=None,
        )
        if photo and photo.get("file_id"):
            return str(photo["file_id"]), ".jpg", photo.get("file_size")

    document = message.get("document")
    if not isinstance(document, dict):
        return None
    mime_type = str(document.get("mime_type") or "").lower()
    suffix = print_media.suffix_from_mime(mime_type)
    if not suffix:
        filename_suffix = Path(str(document.get("file_name") or "")).suffix.lower()
        suffix = print_media.suffix_from_extension(filename_suffix)
        if not suffix and mime_type.startswith("image/"):
            suffix = filename_suffix if SAFE_SUFFIX_RE.fullmatch(
                filename_suffix
            ) else ".img"
    if not suffix or not document.get("file_id"):
        raise ValueError(
            "пришлите изображение как обычное фото или image-документ"
        )
    return str(document["file_id"]), suffix, document.get("file_size")


def _caption_with_status(caption: str, status: str) -> str:
    caption = str(caption or "").strip()
    return f"{caption}\n\n{status}" if caption else status


class TelegramPrintUI:
    def __init__(self, session: aiohttp.ClientSession, base: str) -> None:
        self.session = session
        self.base = base

    async def send_text(self, user: print_flow.PrintUser, text: str) -> bool:
        return await telegram_api.send_text(
            self.session,
            self.base,
            user.conversation_id,
            text,
        )

    async def send_choice(
        self,
        upload: print_flow.PrintUpload,
        preview: bytes,
        job_id: str,
    ) -> int | None:
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": print_flow.PRINT_CHOICE_LABELS["fit"],
                        "callback_data": f"print:fit:{job_id}",
                    },
                    {
                        "text": print_flow.PRINT_CHOICE_LABELS["fill"],
                        "callback_data": f"print:fill:{job_id}",
                    },
                ],
                [{
                    "text": print_flow.PRINT_CHOICE_LABELS["cancel"],
                    "callback_data": f"print:cancel:{job_id}",
                }],
            ],
        }
        caption = print_flow.print_choice_message(telegram_html=True)
        reply_to = (
            upload.user.source_message_id
            if isinstance(upload.user.source_message_id, int)
            else None
        )
        return await telegram_api.send_photo(
            self.session,
            self.base,
            upload.user.conversation_id,
            preview,
            caption,
            keyboard,
            reply_to,
        )

    async def send_ai_choice(
        self,
        upload: ai_flow.AiUpload,
        preview: bytes,
        job_id: str,
        templates: tuple[dict, ...],
    ) -> int | None:
        keyboard = {
            "inline_keyboard": [
                [{
                    "text": template["button"],
                    "callback_data": f"ai:t:{template['id']}:{job_id}",
                }]
                for template in templates
            ] + [[{
                "text": "❌ ОТМЕНА",
                "callback_data": f"ai:c:{job_id}",
            }]],
        }
        reply_to = (
            upload.user.source_message_id
            if isinstance(upload.user.source_message_id, int)
            else None
        )
        return await telegram_api.send_photo(
            self.session,
            self.base,
            upload.user.conversation_id,
            preview,
            "✨ Выберите AI-эффект:",
            keyboard,
            reply_to,
            filename="ai_source.jpg",
            content_type="image/jpeg",
            parse_mode=None,
        )

    async def acknowledge(
        self,
        action: print_flow.PrintAction,
        text: str,
        *,
        alert: bool = False,
    ) -> None:
        await telegram_api.answer_callback(
            self.session,
            self.base,
            action.action_id,
            text,
            show_alert=alert,
        )

    async def update_choice(
        self,
        action: print_flow.PrintAction,
        text: str,
    ) -> None:
        callback = action.context
        message = callback.get("message") or {}
        message_id = message.get("message_id")
        if isinstance(message_id, int):
            caption = str(message.get("caption") or "").strip()
            raw_entities = message.get("caption_entities")
            entities = (
                raw_entities
                if isinstance(raw_entities, list)
                and all(isinstance(item, dict) for item in raw_entities)
                else None
            )
            await telegram_api.edit_print_caption(
                self.session,
                self.base,
                action.user.conversation_id,
                message_id,
                _caption_with_status(caption, text),
                caption_entities=entities,
            )

    async def update_admin(
        self,
        action: print_flow.PrintAction,
        status: str,
    ) -> None:
        callback = action.context
        message = callback.get("message") or {}
        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            return
        caption = str(message.get("caption") or "").strip()
        raw_entities = message.get("caption_entities")
        entities = (
            raw_entities
            if isinstance(raw_entities, list)
            and all(isinstance(item, dict) for item in raw_entities)
            else None
        )
        await telegram_api.edit_print_caption(
            self.session,
            self.base,
            action.user.conversation_id,
            message_id,
            _caption_with_status(caption, status),
            caption_entities=entities,
        )


async def handle_message(
    session: aiohttp.ClientSession,
    base: str,
    message: dict,
) -> bool:
    if not message.get("photo") and not message.get("document"):
        return False
    user = user_from_message(message)
    media_group_id = message.get("media_group_id")
    if media_group_id:
        group_key = str(media_group_id)
        if group_key not in _rejected_media_groups:
            _rejected_media_groups.add(group_key)
            await telegram_api.send_text(
                session,
                base,
                user.conversation_id,
                "❌ Медиальбомы пока не печатаются. "
                "Пришлите одно изображение отдельным сообщением",
            )
        return True

    try:
        file_info = _print_file(message)
    except Exception as exc:
        await telegram_api.send_text(
            session,
            base,
            user.conversation_id,
            print_flow.rejected_photo_message(exc),
        )
        return True
    if file_info is None:
        return False
    file_id, suffix, declared_size = file_info

    async def download() -> bytes:
        return await telegram_api.download_file(
            session,
            base,
            file_id,
            max_size=print_media.MAX_PRINT_FILE_SIZE,
        )

    upload = print_flow.PrintUpload(
        user=user,
        suffix=suffix,
        declared_size=declared_size,
        download=download,
    )
    ui = TelegramPrintUI(session, base)
    if ai_flow.is_ai_caption(message.get("caption")):
        handled = await ai_flow.handle_upload(
            ai_flow.AiUpload(
                user=user,
                suffix=suffix,
                declared_size=declared_size,
                download=download,
            ),
            ui,
        )
        if handled:
            return True
    return await print_flow.handle_upload(upload, ui)


def _action_user(callback: dict) -> print_flow.PrintUser:
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    user_id = _user_id(sender)
    chat_id = (message.get("chat") or {}).get("id", user_id)
    return print_flow.PrintUser(
        provider="telegram",
        provider_user_id=user_id,
        conversation_id=chat_id,
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
        allowlisted=is_allowlisted(user_id),
        is_admin=telegram_api.is_admin(user_id),
    )


async def handle_callback(
    session: aiohttp.ClientSession,
    base: str,
    callback: dict,
) -> bool:
    data = str(callback.get("data") or "")
    matched = CHOICE_RE.fullmatch(data)
    if matched:
        action_name, job_id = matched.groups()
        action = print_flow.PrintAction(
            user=_action_user(callback),
            action=action_name,
            job_id=job_id,
            action_id=callback.get("id"),
            context=callback,
        )
        return await print_flow.handle_choice(
            action,
            TelegramPrintUI(session, base),
        )

    matched = ADMIN_RE.fullmatch(data)
    if matched:
        action_name, job_id = matched.groups()
        action = print_flow.PrintAction(
            user=_action_user(callback),
            action=action_name,
            job_id=job_id,
            action_id=callback.get("id"),
            context=callback,
        )
        return await print_flow.handle_admin_action(
            action,
            TelegramPrintUI(session, base),
        )

    matched = AI_TEMPLATE_RE.fullmatch(data)
    if matched:
        template_id, job_id = matched.groups()
        action = ai_flow.AiAction(
            user=_action_user(callback),
            action="template",
            template_id=template_id,
            job_id=job_id,
            action_id=callback.get("id"),
            context=callback,
        )
        return await ai_flow.handle_action(
            action,
            TelegramPrintUI(session, base),
        )

    matched = AI_ACTION_RE.fullmatch(data)
    if matched:
        action_code, job_id = matched.groups()
        action = ai_flow.AiAction(
            user=_action_user(callback),
            action="cancel" if action_code == "c" else "print",
            job_id=job_id,
            action_id=callback.get("id"),
            context=callback,
        )
        return await ai_flow.handle_action(
            action,
            TelegramPrintUI(session, base),
        )
    return False
