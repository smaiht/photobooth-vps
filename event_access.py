"""Current event state and cross-messenger guest-access helpers."""

import base64
import hashlib
import hmac
import io
import os
import unicodedata

import qrcode
from PIL import Image, ImageDraw, ImageFont

import telegram_api
import vk_api
import yadisk_poll

EVENT_KEY = os.environ.get("EVENT_KEY", "")
TECHNICAL_EVENT_NAME = "Кафе"


def normalize_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(name or ""))
    return yadisk_poll.validate_event_name(" ".join(normalized.split()))


def access_token(event_name: str) -> str:
    key = EVENT_KEY.strip()
    if not key:
        raise RuntimeError("EVENT_KEY не настроен")
    canonical_name = normalize_name(event_name).casefold()
    digest = hmac.new(
        key.encode("utf-8"),
        canonical_name.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    # Nine HMAC bytes encode to exactly 12 URL-safe Base64 characters.
    return base64.urlsafe_b64encode(digest[:9]).decode("ascii")


def telegram_start_link(event_name: str) -> str:
    if not telegram_api.BOT_USERNAME:
        raise RuntimeError("TG_BOT_USERNAME не настроен")
    return (
        f"https://t.me/{telegram_api.BOT_USERNAME}"
        f"?start={access_token(event_name)}"
    )


def vk_start_link(event_name: str) -> str:
    return vk_api.community_link(ref=access_token(event_name))


def guest_links(event_name: str) -> dict[str, str]:
    return {
        "telegram": telegram_start_link(event_name),
        "vk": vk_start_link(event_name),
    }


def validate_configuration() -> None:
    if not EVENT_KEY.strip():
        raise RuntimeError("EVENT_KEY не настроен")
    if not telegram_api.BOT_USERNAME:
        raise RuntimeError("TG_BOT_USERNAME не настроен")
    if not vk_api.GROUP_USERNAME:
        raise RuntimeError("VK_GROUP_USERNAME не настроен")


def _qr_image(payload: str, *, box_size: int = 12) -> Image.Image:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    source = qr.make_image(fill_color="black", back_color="white").get_image()
    try:
        return source.convert("RGB")
    finally:
        source.close()


def _label_font(size: int) -> ImageFont.ImageFont:
    for font_name in (
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(font_name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def qr_code_png(payload: str) -> bytes:
    image = _qr_image(payload)
    try:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


def guest_qr_sheet_png(links: dict[str, str]) -> bytes:
    """Render one shareable card containing full-size Telegram and VK QRs."""
    platforms = (
        ("Telegram", links.get("telegram")),
        ("VK", links.get("vk")),
    )
    if any(not isinstance(link, str) or not link for _label, link in platforms):
        raise ValueError("для QR-листа нужны ссылки Telegram и VK")

    qr_images = [_qr_image(link, box_size=11) for _label, link in platforms]
    margin = 40
    gap = 32
    label_height = 64
    width = margin * 2 + sum(image.width for image in qr_images) + gap
    height = margin * 2 + label_height + max(image.height for image in qr_images)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    font = _label_font(34)

    try:
        x = margin
        for (label, _link), image in zip(platforms, qr_images, strict=True):
            bounds = draw.textbbox((0, 0), label, font=font)
            label_width = bounds[2] - bounds[0]
            draw.text(
                (x + (image.width - label_width) // 2, margin),
                label,
                fill="black",
                font=font,
            )
            sheet.paste(image, (x, margin + label_height))
            x += image.width + gap

        output = io.BytesIO()
        sheet.save(output, format="PNG", optimize=True)
        return output.getvalue()
    finally:
        for image in qr_images:
            image.close()
        sheet.close()


def current_event() -> tuple[str, str | None, bool]:
    event_name = normalize_name(yadisk_poll.current_event_folder())
    cafe_mode = event_name == TECHNICAL_EVENT_NAME
    event_token = None if cafe_mode else access_token(event_name)
    return event_name, event_token, cafe_mode
