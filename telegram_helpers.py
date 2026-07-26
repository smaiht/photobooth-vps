"""Telegram-specific message metadata and bot-user helpers."""

import io
import re

from PIL import Image, ImageOps

import database


def sender_data(message: dict) -> dict:
    """Build print metadata from a Telegram message."""
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    first_name = str(sender.get("first_name") or "").strip()
    last_name = str(sender.get("last_name") or "").strip()
    display_name = " ".join(part for part in (first_name, last_name) if part)
    username = str(sender.get("username") or "").strip()
    document = message.get("document") or {}
    source_filename = str(document.get("file_name") or "telegram_photo.jpg")
    source_filename = re.sub(r"[\x00-\x1f\x7f]", "", source_filename)[:200]
    return {
        "sender_id": sender.get("id"),
        "sender_name": display_name[:100],
        "username": username[:64],
        "source_filename": source_filename,
        "telegram_mime_type": str(document.get("mime_type") or ""),
        "telegram_document": dict(document),
        "telegram_user": dict(sender),
        "telegram_chat": dict(chat),
        "telegram_message_id": message.get("message_id"),
        "telegram_message_date": message.get("date"),
        "telegram_caption": str(message.get("caption") or "")[:1024],
        "telegram_source_kind": "photo" if message.get("photo") else "document",
    }


async def ensure_bot_user(sender: dict) -> int:
    """Upsert one Telegram sender and return the internal bot-user ID."""
    user_id = sender.get("id")
    if user_id is None:
        raise ValueError("Telegram не передал ID пользователя")
    return await database.ensure_bot_user(
        provider="telegram",
        provider_user_id=user_id,
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
    )


def photo_jpeg_preview(payload: bytes) -> bytes:
    """Convert any supported input image into a Telegram-friendly JPEG."""
    with Image.open(io.BytesIO(payload)) as source:
        source.seek(0)
        oriented = ImageOps.exif_transpose(source)
        try:
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                image = Image.new("RGB", rgba.size, (255, 255, 255))
                image.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
            else:
                image = oriented.convert("RGB")
        finally:
            if oriented is not source:
                oriented.close()
    try:
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, "JPEG", quality=88, optimize=True)
        return output.getvalue()
    finally:
        image.close()
