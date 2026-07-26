"""Current event state and Telegram guest-access helpers."""

import base64
import hashlib
import hmac
import io
import os
import unicodedata

import qrcode

import telegram_api
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


def start_link(event_name: str) -> str:
    if not telegram_api.BOT_USERNAME:
        raise RuntimeError("TG_BOT_USERNAME не настроен")
    return (
        f"https://t.me/{telegram_api.BOT_USERNAME}"
        f"?start={access_token(event_name)}"
    )


def validate_configuration() -> None:
    if not EVENT_KEY.strip():
        raise RuntimeError("EVENT_KEY не настроен")
    if not telegram_api.BOT_USERNAME:
        raise RuntimeError("TG_BOT_USERNAME не настроен")


def qr_code_png(payload: str) -> bytes:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").get_image()
    try:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


def current_event() -> tuple[str, str | None, bool]:
    event_name = normalize_name(yadisk_poll.current_event_folder())
    cafe_mode = event_name == TECHNICAL_EVENT_NAME
    event_token = None if cafe_mode else access_token(event_name)
    return event_name, event_token, cafe_mode
