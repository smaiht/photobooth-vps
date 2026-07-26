"""Photobooth VPS: Yandex.Disk media/control bridge to Telegram."""

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

import database
import migrate
import yadisk_control
import yadisk_poll
import yadisk_updates
import print_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("VPS_CONFIG", "config_vps.json"))


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


CONFIG = _load_config()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
TG_ADMIN = os.environ.get("TG_ADMIN_ID", "")
GITHUB_RELEASE_URL = os.environ.get("GITHUB_RELEASE_URL", "")

# MVP allowlist for arbitrary Telegram image printing. Admin commands still use
# TG_ADMIN_ID; these IDs only grant access to the print-by-image flow.
PRINT_ALLOWED_USER_IDS = frozenset({6634566969, 5683598562})
MAX_TELEGRAM_PRINT_FILE_SIZE = 20 * 1024 * 1024
PRINT_MIME_SUFFIXES = {
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
PRINT_FILE_SUFFIXES = {
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
SAFE_PRINT_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_rejected_media_groups: set[str] = set()
_print_callbacks_in_progress: set[str] = set()
PRINT_CALLBACK_RE = re.compile(r"^print:(fit|fill|cancel):([a-f0-9]{32})$")

TG_COMMANDS = {
    "/run": "run",
    "/status": "status",
    "/logs": "send_logs",
    "/get_config": "get_config",
    "/clear_logs": "clear_logs",
    "/restart": "restart",
    "/link": "link",
    "/update": "update",
}
CAMERA_SETTING_COMMAND_RE = re.compile(
    r"^/([a-z][a-z0-9_]{0,63})(?:@[a-z0-9_]+)?$",
    re.IGNORECASE,
)
RESERVED_TELEGRAM_COMMANDS = set(TG_COMMANDS) | {"/event", "/start"}
UpdateProgressCallback = Callable[[str], Awaitable[None]]
TG_COMMAND_KEYBOARD = {
    "inline_keyboard": [
        [{"text": text, "callback_data": text}]
        for text in TG_COMMANDS
    ],
}


async def _tg_send_text(
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
            log.warning(f"TG sendMessage failed: {await response.text()}")
            return False
        return True


async def _tg_send_photo(
    session: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    photo: bytes,
    caption: str,
    reply_markup: dict,
    reply_to_message_id: int | None,
) -> bool:
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("caption", caption)
    form.add_field("parse_mode", "HTML")
    form.add_field(
        "photo",
        photo,
        filename="print_options.jpg",
        content_type="image/jpeg",
    )
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
            log.warning("TG print preview send failed: %s", await response.text())
            return False
        return True


async def _tg_edit_print_caption(
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
        f"{base}/editMessageCaption", json=payload,
    ) as response:
        if response.status != 200:
            log.warning("TG print preview edit failed: %s", await response.text())
            return False
        return True


def is_admin(user_id) -> bool:
    return bool(TG_ADMIN and user_id is not None and str(user_id) == TG_ADMIN)


async def _tg_show_keyboard(
    telegram: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
) -> None:
    await _tg_send_text(
        telegram,
        base,
        chat_id,
        "Не понял команду. Выбери действие кнопкой:",
        reply_markup=TG_COMMAND_KEYBOARD,
    )


async def _tg_answer_callback(
    telegram: aiohttp.ClientSession,
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
    async with telegram.post(
        f"{base}/answerCallbackQuery",
        json=payload,
    ) as response:
        if response.status != 200:
            log.warning(f"TG answerCallbackQuery failed: {await response.text()}")


async def _do_update(
    progress_callback: UpdateProgressCallback | None = None,
) -> str:
    if not GITHUB_RELEASE_URL:
        raise RuntimeError("GITHUB_RELEASE_URL не задан")

    started_at = time.monotonic()
    log.info("Update: requested, downloading full release from GITHUB_RELEASE_URL")
    async with aiohttp.ClientSession() as download_session:
        async with download_session.get(
            GITHUB_RELEASE_URL, timeout=aiohttp.ClientTimeout(total=300),
        ) as response:
            content_length = response.content_length
            expected_text = (
                f"{content_length / 1048576:.1f} MiB"
                if content_length is not None else "unknown size"
            )
            log.info(
                "Update: GitHub responded HTTP %s, content-length=%s",
                response.status, expected_text,
            )
            if response.status != 200:
                raise RuntimeError(f"GitHub вернул HTTP {response.status}")
            resolved_release_url = str(response.url)

            download_started = time.monotonic()
            last_report_at = download_started
            last_report_bytes = 0
            next_report_bytes = 10 * 1024 * 1024
            downloaded = io.BytesIO()
            async for chunk in response.content.iter_chunked(1024 * 1024):
                downloaded.write(chunk)
                total = downloaded.tell()
                now = time.monotonic()
                if total >= next_report_bytes or now - last_report_at >= 5:
                    interval = max(now - last_report_at, 0.001)
                    elapsed = max(now - download_started, 0.001)
                    current_speed = (total - last_report_bytes) / interval / 1048576
                    average_speed = total / elapsed / 1048576
                    progress = (
                        f", {total * 100 / content_length:.1f}%"
                        if content_length else ""
                    )
                    log.info(
                        "Update: GitHub download %.1f MiB%s, "
                        "speed=%.1f MiB/s, average=%.1f MiB/s",
                        total / 1048576, progress,
                        current_speed, average_speed,
                    )
                    last_report_at = now
                    last_report_bytes = total
                    next_report_bytes = total + 10 * 1024 * 1024

            zip_data = downloaded.getvalue()
            download_elapsed = max(time.monotonic() - download_started, 0.001)
            if content_length is not None and len(zip_data) != content_length:
                raise RuntimeError(
                    f"GitHub download size mismatch: {len(zip_data)}/{content_length}")
            log.info(
                "Update: GitHub download complete, %.1f MiB in %.1fs "
                "(average %.1f MiB/s)",
                len(zip_data) / 1048576, download_elapsed,
                len(zip_data) / download_elapsed / 1048576,
            )

    validation_started = time.monotonic()
    log.info("Update: validating downloaded ZIP CRC")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as downloaded_zip:
            bad_member = downloaded_zip.testzip()
            if bad_member:
                raise ValueError(f"ZIP CRC failed: {bad_member}")
            downloaded_names = downloaded_zip.namelist()
    except zipfile.BadZipFile as exc:
        raise RuntimeError("GitHub вернул невалидный ZIP") from exc
    log.info(
        "Update: source ZIP valid, %d entries checked in %.1fs",
        len(downloaded_names), time.monotonic() - validation_started,
    )

    with zipfile.ZipFile(io.BytesIO(zip_data)) as release_zip:
        names = [name.replace("\\", "/") for name in release_zip.namelist()]
        if "app.py" not in names:
            raise RuntimeError("ZIP не содержит app.py в корне")
    log.info(
        "Update: release ZIP structure accepted, entries=%d, size=%.1f MiB",
        len(names), len(zip_data) / 1048576,
    )

    updates_folder = CONFIG.get("yadisk_updates_folder", "photobooth_system/updates")
    log.info(
        "Update: publishing to Yandex.Disk folder /%s",
        str(updates_folder).strip("/"),
    )
    status = await yadisk_updates.publish_update(
        zip_data,
        updates_folder,
        source_url=resolved_release_url,
        progress_callback=progress_callback,
    )
    artifact = status["artifacts"]["full"]
    log.info(
        "Update: finished successfully in %.1fs, sha256=%s, size=%.1f MiB",
        time.monotonic() - started_at, artifact["sha256"][:16],
        len(zip_data) / 1048576,
    )
    return (
        "✅ Полное обновление загружено на Диск\n"
        f"ZIP: {len(zip_data) / 1048576:.1f} MB\n"
        f"SHA: {artifact['sha256'][:16]}\n"
        "Для установки отправь /restart"
    )


def _event_name_from_command(text: str) -> str | None:
    command, separator, argument = (text or "").strip().partition(" ")
    if command.split("@", 1)[0] != "/event":
        return None
    if not separator or not argument.strip():
        raise ValueError("Использование: /event Название события")
    return yadisk_poll.validate_event_name(argument.strip())


def _camera_setting_from_command(text: str) -> tuple[str, str] | None:
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    matched = CAMERA_SETTING_COMMAND_RE.fullmatch(parts[0])
    if not matched:
        return None

    field = matched.group(1).lower()
    if f"/{field}" in RESERVED_TELEGRAM_COMMANDS:
        return None
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError(f"Использование: /{field} значение")
    return field, parts[1].strip()


def _start_parameter_from_command(text: str) -> tuple[bool, str | None]:
    command, separator, argument = (text or "").strip().partition(" ")
    if command.split("@", 1)[0].lower() != "/start":
        return False, None
    parameter = argument.strip() if separator else ""
    return True, parameter or None


async def _record_telegram_start(update: dict, message: dict) -> bool:
    matched, parameter = _start_parameter_from_command(message.get("text", ""))
    if not matched:
        return False
    sender = message.get("from") or {}
    user_id = sender.get("id")
    if user_id is None:
        log.warning("TG /start ignored: sender id is missing")
        return True
    await database.record_bot_start(
        provider="telegram",
        provider_user_id=user_id,
        start_parameter=parameter,
        provider_update_id=update.get("update_id"),
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
        profile={},
    )
    log.info("TG /start stored for user=%s parameter=%r", user_id, parameter)
    return True


async def _send_disk_command(
    command: str,
    chat_id: str | int,
    data: dict | str | None = None,
) -> str:
    body = await yadisk_control.send_command(command, data, reply_chat_id=chat_id)
    return body["command_id"]


def _telegram_print_file(message: dict) -> tuple[str, str, int | None] | None:
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
    suffix = PRINT_MIME_SUFFIXES.get(mime_type)
    if not suffix:
        filename_suffix = Path(str(document.get("file_name") or "")).suffix.lower()
        suffix = PRINT_FILE_SUFFIXES.get(filename_suffix)
        if not suffix and mime_type.startswith("image/"):
            suffix = filename_suffix if SAFE_PRINT_SUFFIX_RE.fullmatch(
                filename_suffix) else ".img"
    if not suffix or not document.get("file_id"):
        raise ValueError("пришли изображение как обычное фото или image-документ")
    return str(document["file_id"]), suffix, document.get("file_size")


async def _tg_download_file(
    telegram: aiohttp.ClientSession,
    base: str,
    file_id: str,
) -> bytes:
    async with telegram.post(
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

    download_url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}"
    payload = bytearray()
    async with telegram.get(
        download_url,
        timeout=aiohttp.ClientTimeout(total=90),
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Telegram download HTTP {response.status}")
        async for chunk in response.content.iter_chunked(1024 * 1024):
            payload.extend(chunk)
            if len(payload) > MAX_TELEGRAM_PRINT_FILE_SIZE:
                raise ValueError("файл больше 20 МБ")
    if not payload:
        raise ValueError("Telegram прислал пустой файл")
    return bytes(payload)


def _telegram_sender_data(message: dict) -> dict:
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


async def _submit_print_job(
    job_id: str,
    user_id: int,
    suffix: str,
    payload: bytes,
    metadata: dict,
    chat_id: str | int,
) -> str:
    event_folder = str(
        metadata.get("event_folder") or yadisk_poll.current_event_folder())
    stored_files = await yadisk_poll.store_print_job(
        job_id,
        user_id,
        suffix,
        payload,
        metadata,
        event_folder=event_folder,
    )
    metadata.update(stored_files)
    return await _send_disk_command("print_image", chat_id, metadata)


async def _tg_handle_print_message(
    telegram: aiohttp.ClientSession,
    base: str,
    message: dict,
) -> bool:
    if not message.get("photo") and not message.get("document"):
        return False

    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat_id = (message.get("chat") or {}).get("id", user_id)
    if user_id not in PRINT_ALLOWED_USER_IDS:
        await _tg_send_text(
            telegram, base, chat_id,
            "❌ Печать изображений для этого аккаунта не разрешена",
        )
        return True

    media_group_id = message.get("media_group_id")
    if media_group_id:
        group_key = str(media_group_id)
        if group_key not in _rejected_media_groups:
            _rejected_media_groups.add(group_key)
            await _tg_send_text(
                telegram, base, chat_id,
                "❌ Медиальбомы пока не печатаются. Пришли одно изображение отдельным сообщением",
            )
        return True

    job_id = uuid.uuid4().hex
    try:
        file_info = _telegram_print_file(message)
        if file_info is None:
            return False
        file_id, suffix, declared_size = file_info
        if declared_size is not None and int(declared_size) > MAX_TELEGRAM_PRINT_FILE_SIZE:
            raise ValueError("файл больше 20 МБ")

        await _tg_send_text(
            telegram,
            base,
            chat_id,
            "⏳ Ваше фото обрабатывается, подождите немного…",
        )
        payload = await _tg_download_file(telegram, base, file_id)
        preview = await asyncio.to_thread(print_jobs.build_choice_preview, payload)
        metadata = _telegram_sender_data(message)
        metadata.update({
            "job_id": job_id,
            "source_size": len(payload),
            "source_width": preview.source_size[0],
            "source_height": preview.source_size[1],
            "print_orientation": preview.orientation,
            "print_target_size": list(preview.target_size),
            "event_folder": yadisk_poll.current_event_folder(),
        })

        if preview.exact_ratio:
            metadata.update({
                "print_mode": "fit",
                "print_choice": "automatic_exact_ratio",
            })
            await _submit_print_job(
                job_id, user_id, suffix, payload, metadata, chat_id)
            await _tg_send_text(
                telegram,
                base,
                chat_id,
                "⏳ Фото подходит под формат 10×15 и передано на печать",
            )
            log.info(
                "TG print job sent without choice job=%s user=%s source=%sx%s",
                job_id, user_id, preview.source_size[0], preview.source_size[1],
            )
            return True

        await asyncio.to_thread(
            print_jobs.save_pending,
            job_id,
            suffix,
            payload,
            metadata,
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "1️⃣ Как есть",
                        "callback_data": f"print:fit:{job_id}",
                    },
                    {
                        "text": "2️⃣ Увеличить",
                        "callback_data": f"print:fill:{job_id}",
                    },
                ],
                [{
                    "text": "❌ Отмена",
                    "callback_data": f"print:cancel:{job_id}",
                }],
            ],
        }
        caption = (
            "<b>Фото не совпадает с форматом 10×15.</b>\n\n"
            "1 — <b>как есть</b> — будут белые поля.\n"
            "2 — <b>увеличить под размер</b> — обрежутся затемнённые края."
        )
        sent = await _tg_send_photo(
            telegram,
            base,
            chat_id,
            preview.payload or b"",
            caption,
            keyboard,
            message.get("message_id"),
        )
        if not sent:
            await asyncio.to_thread(print_jobs.delete_pending, job_id)
            raise RuntimeError("не удалось показать варианты печати")
        log.info(
            "TG print choice requested job=%s user=%s source=%sx%s "
            "orientation=%s overflow=%s",
            job_id, user_id,
            preview.source_size[0], preview.source_size[1],
            preview.orientation, preview.overflow_axis,
        )
    except Exception as exc:
        log.warning("TG print job rejected user=%s job=%s: %s", user_id, job_id, exc)
        await _tg_send_text(telegram, base, chat_id, f"❌ Фото не принято: {exc}")
    return True


async def _tg_handle_print_callback(
    telegram: aiohttp.ClientSession,
    base: str,
    callback: dict,
) -> bool:
    matched = PRINT_CALLBACK_RE.fullmatch(str(callback.get("data") or ""))
    if not matched:
        return False
    mode, job_id = matched.groups()
    callback_id = callback.get("id")
    user_id = (callback.get("from") or {}).get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id", user_id)

    if user_id not in PRINT_ALLOWED_USER_IDS:
        await _tg_answer_callback(
            telegram, base, callback_id,
            "Эта печать недоступна", show_alert=True)
        return True
    if job_id in _print_callbacks_in_progress:
        await _tg_answer_callback(
            telegram, base, callback_id, "Задание уже обрабатывается")
        return True
    _print_callbacks_in_progress.add(job_id)
    try:
        try:
            payload, metadata = await asyncio.to_thread(
                print_jobs.load_pending, job_id)
        except Exception:
            await _tg_answer_callback(
                telegram,
                base,
                callback_id,
                "Кнопка уже неактивна. Для новой печати пришли фото ещё раз.",
                show_alert=True,
            )
            return True

        if int(metadata.get("sender_id") or 0) != user_id:
            await _tg_answer_callback(
                telegram, base, callback_id,
                "Выбрать может только отправитель фото", show_alert=True)
            return True
        status = str(metadata.get("pending_status") or "")
        if status in ("submitting", "submitted"):
            await _tg_answer_callback(
                telegram, base, callback_id,
                "Фото уже передаётся или передано на печать")
            return True

        if mode == "cancel":
            try:
                await asyncio.to_thread(print_jobs.delete_pending, job_id)
            except Exception:
                log.exception("Could not cancel pending print job=%s", job_id)
                await _tg_answer_callback(
                    telegram,
                    base,
                    callback_id,
                    "Не удалось отменить печать. Попробуй ещё раз.",
                    show_alert=True,
                )
                return True

            await _tg_answer_callback(
                telegram, base, callback_id, "Печать отменена")
            if isinstance(message.get("message_id"), int):
                await _tg_edit_print_caption(
                    telegram,
                    base,
                    chat_id,
                    message["message_id"],
                    "🚫 Печать отменена.",
                )
            log.info("TG print choice cancelled job=%s user=%s", job_id, user_id)
            return True

        selected_text = (
            "как есть, с белыми полями"
            if mode == "fit"
            else "увеличить под размер, края обрежутся"
        )
        try:
            await _tg_answer_callback(
                telegram, base, callback_id, "Принято, отправляю на печать")
            metadata = await asyncio.to_thread(
                print_jobs.update_pending,
                job_id,
                pending_status="submitting",
                print_mode=mode,
                print_choice="telegram_button",
                print_selected_at=time.time(),
            )
            suffix = str(metadata["source_suffix"])
            command_id = await _submit_print_job(
                job_id, user_id, suffix, payload, metadata, chat_id)
            await asyncio.to_thread(
                print_jobs.update_pending,
                job_id,
                pending_status="submitted",
                command_id=command_id,
            )
        except Exception as exc:
            log.exception(
                "TG print choice submission failed job=%s mode=%s", job_id, mode)
            try:
                await asyncio.to_thread(
                    print_jobs.update_pending,
                    job_id,
                    pending_status="awaiting_choice",
                )
            except Exception:
                log.exception("Could not restore pending print choice job=%s", job_id)
            await _tg_send_text(
                telegram,
                base,
                chat_id,
                f"❌ Не удалось передать фото на печать: {exc}. Нажми вариант ещё раз.",
            )
            return True

        if isinstance(message.get("message_id"), int):
            await _tg_edit_print_caption(
                telegram,
                base,
                chat_id,
                message["message_id"],
                f"✅ Выбрано: {selected_text}. Фото передано на печать.",
            )
        try:
            await asyncio.to_thread(print_jobs.delete_pending, job_id)
        except Exception:
            log.exception("Could not remove submitted pending print job=%s", job_id)
        log.info(
            "TG print choice submitted job=%s user=%s mode=%s command=%s",
            job_id, user_id, mode, command_id,
        )
        return True
    finally:
        _print_callbacks_in_progress.discard(job_id)


async def _tg_handle_admin_command(
    telegram: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    text: str,
) -> None:
    text = (text or "").strip()
    try:
        event_name = _event_name_from_command(text)
    except ValueError as exc:
        await _tg_send_text(telegram, base, chat_id, f"❌ {exc}")
        return

    if event_name is not None:
        try:
            await _send_disk_command("set_event", chat_id, {"name": event_name})
            await _tg_send_text(
                telegram, base, chat_id,
                f"⏳ Переключаю event на будке и VPS: {event_name}")
        except Exception as exc:
            await _tg_send_text(telegram, base, chat_id, f"❌ Event не отправлен: {exc}")
        return

    try:
        camera_setting = _camera_setting_from_command(text)
    except ValueError as exc:
        await _tg_send_text(telegram, base, chat_id, f"❌ {exc}")
        return

    if camera_setting is not None:
        field, value = camera_setting
        try:
            await _send_disk_command(
                "set_camera_config",
                chat_id,
                {"field": field, "value": value},
            )
            await _tg_send_text(
                telegram,
                base,
                chat_id,
                f"⏳ Камера: {field} → {value}; ожидаю подтверждение будки",
            )
        except Exception as exc:
            await _tg_send_text(
                telegram,
                base,
                chat_id,
                f"❌ Настройка камеры не отправлена: {exc}",
            )
        return

    command_token = text.split(maxsplit=1)[0] if text else ""
    normalized = (
        command_token.split("@", 1)[0].lower()
        if command_token.startswith("/") else command_token
    )
    command = TG_COMMANDS.get(normalized)
    if not command:
        await _tg_show_keyboard(telegram, base, chat_id)
        return

    if command == "update":
        await _tg_send_text(telegram, base, chat_id, "⏳ Скачиваю полный релиз...")

        async def report_update_progress(message: str) -> None:
            await _tg_send_text(telegram, base, chat_id, message)

        try:
            result = await _do_update(report_update_progress)
        except Exception as exc:
            log.exception("Update %s failed", command)
            result = f"❌ Ошибка: {exc}"
        await _tg_send_text(telegram, base, chat_id, result)
        return

    if command == "link":
        try:
            public_url = await yadisk_poll.publish_current_folder()
            await _tg_send_text(
                telegram, base, chat_id,
                f"Event: {yadisk_poll.current_event_folder()}\n{public_url}")
        except Exception as exc:
            await _tg_send_text(telegram, base, chat_id, f"❌ Ссылка не создана: {exc}")
        return

    try:
        await _send_disk_command(command, chat_id)
        message = (
            "⏳ Запрашиваю конфиги фотобудки..."
            if command == "get_config"
            else f"⏳ {command}: команда отправлена"
        )
        await _tg_send_text(telegram, base, chat_id, message)
    except Exception as exc:
        await _tg_send_text(telegram, base, chat_id, f"❌ Команда не отправлена: {exc}")


async def tg_poll_commands() -> None:
    if not TG_TOKEN or not TG_ADMIN:
        log.warning("Telegram bot/admin is not configured")
        return
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    offset = 0
    allowed_updates = json.dumps(["message", "callback_query"])
    async with aiohttp.ClientSession() as telegram:
        while True:
            try:
                async with telegram.get(
                    f"{base}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 10,
                        "allowed_updates": allowed_updates,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    data = await response.json()
                for update in data.get("result", []):
                    next_offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        await _record_telegram_start(update, message)
                        user_id = message.get("from", {}).get("id")
                        print_handled = await _tg_handle_print_message(
                            telegram, base, message)
                        if not print_handled and is_admin(user_id):
                            chat_id = message.get("chat", {}).get("id", user_id)
                            await _tg_handle_admin_command(
                                telegram, base, chat_id, message.get("text", ""))
                    else:
                        callback = update.get("callback_query")
                        if callback:
                            user_id = callback.get("from", {}).get("id")
                            print_callback_handled = await _tg_handle_print_callback(
                                telegram, base, callback)
                            if not print_callback_handled and is_admin(user_id):
                                await _tg_answer_callback(
                                    telegram, base, callback.get("id"))
                                chat_id = callback.get("message", {}).get(
                                    "chat", {}).get("id", user_id)
                                await _tg_handle_admin_command(
                                    telegram, base, chat_id, callback.get("data", ""))
                    # Confirm an update only after every durable side effect succeeded.
                    # A DB failure therefore retries /start instead of silently losing it.
                    offset = next_offset
            except Exception as exc:
                log.warning(f"TG poll error: {exc}")
                await asyncio.sleep(5)


def _save_vps_event(name: str) -> None:
    data = _load_config()
    data["yadisk_folder"] = name
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_PATH)
    CONFIG["yadisk_folder"] = name


async def _tg_send_document(
    chat_id: str | int,
    payload: bytes,
    filename: str,
    content_type: str,
) -> bool:
    if not TG_TOKEN:
        return False
    form = aiohttp.FormData()
    # aiohttp multipart fields accept text/bytes, unlike Telegram JSON where an
    # integer chat_id is valid. Passing the raw int raises during serialization.
    form.add_field("chat_id", str(chat_id))
    form.add_field(
        "document", payload, filename=filename, content_type=content_type)
    async with aiohttp.ClientSession() as telegram:
        async with telegram.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                log.warning(
                    "TG document send failed filename=%s: %s",
                    filename, await response.text(),
                )
                return False
            return True


async def _tg_send_documents(
    chat_id: str | int,
    documents: list[tuple[bytes, str, str]],
) -> bool:
    """Send two or more separate documents in one Telegram media group."""
    if not TG_TOKEN or len(documents) < 2:
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
    async with aiohttp.ClientSession() as telegram:
        async with telegram.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMediaGroup",
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


async def _tg_send_log(chat_id: str | int, payload: bytes) -> bool:
    return await _tg_send_document(
        chat_id, payload, "photobooth.log", "text/plain")


async def _handle_control_response(response: dict) -> bool:
    chat_id = response.get("reply_chat_id") or TG_ADMIN
    if not chat_id or not TG_TOKEN:
        return False

    if response["status"] == "ok" and response["command"] == "set_event":
        event_name = response.get("event_folder")
        if not event_name:
            return False
        try:
            await yadisk_poll.set_event_folder(event_name)
            _save_vps_event(event_name)
        except Exception as exc:
            log.warning(f"Control: cannot activate event on VPS: {exc}")
            return False

    artifact_path = response.get("artifact_path")
    if artifact_path:
        try:
            payload = await yadisk_control.download_bytes(artifact_path)
            if response["command"] == "get_config":
                vps_config = await asyncio.to_thread(CONFIG_PATH.read_bytes)
                delivered = await _tg_send_documents(
                    chat_id,
                    [
                        (
                            payload,
                            "photobooth_configs.txt",
                            "text/plain; charset=utf-8",
                        ),
                        (
                            vps_config,
                            "config_vps.json",
                            "application/json",
                        ),
                    ],
                )
                artifact_label = "booth and VPS configs"
            else:
                delivered = await _tg_send_log(chat_id, payload)
                artifact_label = "log"
            if not delivered:
                return False
        except Exception as exc:
            log.warning("Control: artifact delivery failed: %s", exc)
            return False

        # The document/group itself is the complete response. Cleanup must
        # never cause the same Telegram upload to be sent again.
        try:
            deleted = await yadisk_control.delete_resource(artifact_path)
        except Exception as exc:
            deleted = None
            log.warning(
                "Control: delivered %s; artifact cleanup failed: %s",
                artifact_label, exc,
            )
        if deleted is False:
            log.warning(
                "Control: delivered %s but could not delete %s",
                artifact_label, artifact_path,
            )
        log.info(
            "Control: %s delivered to Telegram chat=%s",
            artifact_label, chat_id,
        )
        return True

    prefix = "✅" if response["status"] == "ok" else "❌"
    async with aiohttp.ClientSession() as telegram:
        delivered = await _tg_send_text(
            telegram,
            f"https://api.telegram.org/bot{TG_TOKEN}",
            chat_id,
            f"{prefix} {response['message']}",
        )
    if not delivered:
        return False
    return True


async def main() -> None:
    log.info("Database: applying pending migrations")
    await asyncio.to_thread(migrate.apply_migrations)
    log.info("Database: schema is ready")

    yadisk_folder = CONFIG.get("yadisk_folder", "")
    control_folder = CONFIG.get("yadisk_control_folder", "photobooth_system/control")
    inbox_ready = await yadisk_poll.yadisk_init(
        yadisk_folder, control_folder, TG_TOKEN, TG_CHAT)
    await yadisk_control.control_init(control_folder)
    if not inbox_ready:
        log.error("Yandex.Disk inbox poller is not configured")
    else:
        asyncio.create_task(yadisk_poll.yadisk_poll_loop(_handle_control_response))

    asyncio.create_task(tg_poll_commands())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
