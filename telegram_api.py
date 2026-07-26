"""Low-level asynchronous transport for the Telegram Bot API."""

import json
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("TG_BOT_USERNAME", "").strip().lstrip("@")
BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_DOWNLOAD_FILE_SIZE = 20 * 1024 * 1024


async def send_text(
    session: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with session.post(f"{base}/sendMessage", json=payload) as response:
        if response.status != 200:
            log.warning("TG sendMessage failed: %s", await response.text())
            return False
        return True


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
            json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":")),
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
    async with session.post(f"{base}/sendPhoto", data=form) as response:
        if response.status != 200:
            log.warning("TG photo send failed: %s", await response.text())
            return None
        try:
            body = await response.json()
        except Exception as exc:
            log.warning("TG photo response returned invalid JSON: %s", exc)
            return None
        result = body.get("result") if isinstance(body, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if (
            not isinstance(body, dict)
            or not body.get("ok")
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
        ):
            log.warning("TG photo response has no valid message_id")
            return None
        return message_id


async def edit_print_caption(
    session: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    message_id: int,
    caption: str,
) -> bool:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "reply_markup": {"inline_keyboard": []},
    }
    async with session.post(
        f"{base}/editMessageCaption",
        json=payload,
    ) as response:
        if response.status != 200:
            log.warning("TG print preview edit failed: %s", await response.text())
            return False
        return True


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
    async with session.post(
        f"{base}/answerCallbackQuery",
        json=payload,
    ) as response:
        if response.status != 200:
            log.warning("TG answerCallbackQuery failed: %s", await response.text())


async def get_updates(
    session: aiohttp.ClientSession,
    base: str,
    *,
    offset: int,
    allowed_updates: list[str] | tuple[str, ...],
    poll_timeout: int = 10,
) -> dict:
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


async def download_file(
    session: aiohttp.ClientSession,
    base: str,
    file_id: str,
) -> bytes:
    async with session.post(
        f"{base}/getFile",
        json={"file_id": file_id},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        body = await response.json()
        if response.status != 200 or not body.get("ok"):
            raise RuntimeError("Telegram не отдал файл")
    file_path = (body.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError("Telegram не вернул путь к файлу")

    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    payload = bytearray()
    async with session.get(
        download_url,
        timeout=aiohttp.ClientTimeout(total=90),
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Telegram download HTTP {response.status}")
        async for chunk in response.content.iter_chunked(1024 * 1024):
            payload.extend(chunk)
            if len(payload) > MAX_DOWNLOAD_FILE_SIZE:
                raise ValueError(
                    f"файл больше {MAX_DOWNLOAD_FILE_SIZE // 1048576} МБ"
                )
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
    form = aiohttp.FormData()
    # Multipart fields accept text/bytes, unlike Telegram JSON where an integer
    # chat_id is valid. Passing the raw int raises during serialization.
    form.add_field("chat_id", str(chat_id))
    form.add_field(
        "document",
        payload,
        filename=filename,
        content_type=content_type,
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BOT_API_BASE}/sendDocument",
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                log.warning(
                    "TG document send failed filename=%s: %s",
                    filename,
                    await response.text(),
                )
                return False
            return True


async def send_documents(
    chat_id: str | int,
    documents: list[tuple[bytes, str, str]],
) -> bool:
    """Send two or more separate documents in one Telegram media group."""
    if not BOT_TOKEN or len(documents) < 2:
        return False
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
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BOT_API_BASE}/sendMediaGroup",
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                log.warning(
                    "TG document group send failed files=%s: %s",
                    [filename for _, filename, _ in documents],
                    await response.text(),
                )
                return False
            return True
