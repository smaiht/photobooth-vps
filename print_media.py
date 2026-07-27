"""Small provider-neutral image helpers used by bot adapters."""

from __future__ import annotations

import html
import io

from PIL import Image, ImageOps


MAX_PRINT_FILE_SIZE_MB = 20
MAX_PRINT_FILE_SIZE = MAX_PRINT_FILE_SIZE_MB * 1024 * 1024
PRINT_FORMAT_LABEL = "10×15"
IMAGE_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
}
IMAGE_FILE_SUFFIXES = {
    ".jpg": ".jpg",
    ".jpeg": ".jpg",
    ".png": ".png",
    ".webp": ".webp",
    ".bmp": ".bmp",
    ".tif": ".tiff",
    ".tiff": ".tiff",
    ".gif": ".gif",
    ".heic": ".heic",
    ".heif": ".heif",
    ".avif": ".avif",
    ".ico": ".ico",
}


def suffix_from_mime(mime_type: str | None) -> str | None:
    return IMAGE_MIME_SUFFIXES.get(str(mime_type or "").strip().lower())


def suffix_from_extension(extension: str | None) -> str | None:
    value = str(extension or "").strip().lower()
    if value and not value.startswith("."):
        value = f".{value}"
    return IMAGE_FILE_SUFFIXES.get(value)


def sender_caption(
    metadata: dict,
    *,
    telegram_html: bool = False,
) -> str:
    """Format normalized sender fields consistently in print notifications."""
    sender_name = str(metadata.get("sender_name") or "—")
    username = str(metadata.get("username") or "").strip().lstrip("@")
    provider = str(metadata.get("provider") or "messenger").upper()
    sender_id = str(metadata.get("sender_id") or "—")
    if telegram_html:
        username_line = f" (@{html.escape(username)})" if username else ""
        return (
            f"Пользователь: {html.escape(sender_name)}{username_line}\n"
            f"{html.escape(provider)} ID: <code>{html.escape(sender_id)}</code>"
        )
    username_line = f" (@{username})" if username else ""
    return (
        f"Пользователь: {sender_name}{username_line}\n"
        f"{provider} ID: {sender_id}"
    )


def jpeg_preview(payload: bytes) -> bytes:
    """Convert a supported image into a messenger-friendly JPEG preview."""
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
