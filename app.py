"""Photobooth VPS: Yandex.Disk media/control bridge to Telegram."""

import asyncio
import html
import io
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import aiohttp
from PIL import Image, ImageOps

import admin_commands
import database
import event_access
import migrate
import print_jobs
import telegram_api
import vps_update
import yadisk_control
import yadisk_poll
from telegram_api import (
    answer_callback as _tg_answer_callback,
    download_file as _tg_download_file,
    edit_print_caption as _tg_edit_print_caption,
    send_document as _tg_send_document,
    send_documents as _tg_send_documents,
    send_photo as _tg_send_photo,
    send_text as _tg_send_text,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("VPS_CONFIG", "config_vps.json"))


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


CONFIG = _load_config()
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
TG_ADMIN = os.environ.get("TG_ADMIN_ID", "").strip()
PRINT_EVENT_ACCESS_REQUIRED_MESSAGE = (
    "Для печати сначала отсканируйте QR-код текущего мероприятия."
)

PRINT_ALLOWED_USER_IDS = (
    TG_ADMIN,
    "6634566969",
    "5683598562",
)
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
PRINT_ADMIN_CALLBACK_RE = re.compile(
    r"^print_admin:(approve|reject):([a-f0-9]{32})$"
)

def is_admin(user_id) -> bool:
    return bool(TG_ADMIN and user_id is not None and str(user_id) == TG_ADMIN)


def _is_print_allowlisted(user_id) -> bool:
    return user_id is not None and str(user_id) in PRINT_ALLOWED_USER_IDS


async def _telegram_user_has_print_access(
    user_id,
    *,
    event_token: str | None,
    cafe_mode: bool,
) -> bool:
    """Check access before accepting work from a Telegram user.

    Choice callbacks repeat this check transactionally because access or the
    current event can change while the user is looking at the preview.
    """
    if cafe_mode or _is_print_allowlisted(user_id):
        return True
    if event_token is None:
        raise RuntimeError("у текущего event отсутствует токен доступа")
    return await database.user_has_current_start_parameter(
        provider="telegram",
        provider_user_id=user_id,
        start_parameter=event_token,
    )


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
        reply_markup=admin_commands.COMMAND_KEYBOARD,
    )


def _parse_start_command(text: str) -> tuple[bool, str | None]:
    """Return (is_start, parameter); None is valid for an empty /start."""
    parts = (text or "").strip().split(maxsplit=1)
    if not parts or parts[0].split("@", 1)[0].lower() != "/start":
        return False, None
    parameter = parts[1] if len(parts) == 2 else None
    return True, parameter


async def _record_telegram_start(update: dict, message: dict) -> bool:
    """Persist a /start update and report whether this message was consumed.

    Every non-empty parameter is stored, even if it is not the current event
    token. An empty /start is also recorded in history, but database upsert
    rules keep the user's previous current_start_parameter unchanged.
    """
    matched, parameter = _parse_start_command(message.get("text", ""))
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


async def _tg_reply_to_start(
    telegram: aiohttp.ClientSession,
    base: str,
    message: dict,
) -> None:
    """Reply using access to the current event after /start was persisted."""
    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat_id = (message.get("chat") or {}).get("id", sender.get("id"))
    try:
        event_name, event_token, cafe_mode = event_access.current_event()
        has_access = await _telegram_user_has_print_access(
            user_id,
            event_token=event_token,
            cafe_mode=cafe_mode,
        )
    except Exception as exc:
        log.warning("TG /start access check failed: %s", exc)
        await _tg_send_text(
            telegram,
            base,
            chat_id,
            "⚠️ Печать временно недоступна. Попробуйте чуть позже.",
        )
        return

    if has_access:
        text = (
            "Отправьте фотографию, чтобы напечатать её."
            if cafe_mode
            else f'✅ Вы подключены к мероприятию «{event_name}». '
                 "Теперь можно отправить фотографию."
        )
        await _tg_send_text(telegram, base, chat_id, text)
        return
    await _tg_send_text(
        telegram,
        base,
        chat_id,
        PRINT_EVENT_ACCESS_REQUIRED_MESSAGE,
    )


async def _send_disk_command(
    command: str,
    chat_id: str | int,
    data: dict | str | None = None,
    *,
    command_id: str | None = None,
) -> str:
    body = await yadisk_control.send_command(
        command,
        data,
        reply_chat_id=chat_id,
        command_id=command_id,
    )
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


async def _ensure_telegram_bot_user(sender: dict) -> int:
    user_id = sender.get("id")
    if user_id is None:
        raise ValueError("Telegram не передал ID пользователя")
    return await database.ensure_bot_user(
        provider="telegram",
        provider_user_id=user_id,
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
        profile={},
    )


def _telegram_photo_jpeg_preview(payload: bytes) -> bytes:
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


def _print_mode_text(mode: str) -> str:
    if mode == "fit":
        return "как есть, с белыми полями"
    if mode == "fill":
        return "увеличить под размер, края обрежутся"
    raise ValueError("неизвестный вариант печати")


def _admin_print_caption(job_id: str, metadata: dict, mode: str) -> str:
    sender_name = html.escape(str(metadata.get("sender_name") or "—"))
    username = str(metadata.get("username") or "").strip().lstrip("@")
    username_line = f" (@{html.escape(username)})" if username else ""
    sender_id = html.escape(str(metadata.get("sender_id") or "—"))
    source_filename = html.escape(
        str(metadata.get("source_filename") or "telegram_photo.jpg")
    )
    return (
        "<b>Новая печать в «Кафе»</b>\n"
        f"Job: <code>{html.escape(job_id)}</code>\n"
        f"Выбор: <b>{html.escape(_print_mode_text(mode))}</b>\n"
        f"Пользователь: {sender_name}{username_line}\n"
        f"Telegram ID: <code>{sender_id}</code>\n"
        f"Файл: {source_filename}"
    )


async def _send_admin_print_request(
    telegram: aiohttp.ClientSession,
    base: str,
    *,
    job_id: str,
    payload: bytes,
    metadata: dict,
    mode: str,
) -> int:
    if not TG_ADMIN:
        raise RuntimeError("TG_ADMIN_ID не настроен")
    photo = await asyncio.to_thread(_telegram_photo_jpeg_preview, payload)
    keyboard = {
        "inline_keyboard": [[
            {
                "text": "✅ РАЗРЕШИТЬ",
                "callback_data": f"print_admin:approve:{job_id}",
            },
            {
                "text": "❌ ОТКЛОНИТЬ",
                "callback_data": f"print_admin:reject:{job_id}",
            },
        ]],
    }
    message_id = await _tg_send_photo(
        telegram,
        base,
        TG_ADMIN,
        photo,
        _admin_print_caption(job_id, metadata, mode),
        keyboard,
        None,
    )
    if message_id is None:
        raise RuntimeError("не удалось отправить запрос администратору")
    try:
        await asyncio.to_thread(
            print_jobs.update_pending,
            job_id,
            admin_chat_id=str(TG_ADMIN),
            admin_message_id=message_id,
            pending_status="awaiting_authorization",
        )
    except Exception:
        # The actionable Telegram message was already delivered; local IDs are
        # only useful for diagnostics and must not invalidate that request.
        log.exception("Could not save admin print message id job=%s", job_id)
    return message_id


async def _tg_reply_to_plain_user_message(
    telegram: aiohttp.ClientSession,
    base: str,
    message: dict,
) -> None:
    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat_id = (message.get("chat") or {}).get("id", user_id)
    try:
        _event_name, event_token, cafe_mode = event_access.current_event()
        has_access = await _telegram_user_has_print_access(
            user_id,
            event_token=event_token,
            cafe_mode=cafe_mode,
        )
    except Exception as exc:
        log.warning("Could not resolve access for Telegram user=%s: %s", user_id, exc)
        await _tg_send_text(
            telegram, base, chat_id,
            "⚠️ Печать временно недоступна. Попробуйте чуть позже.",
        )
        return
    text = (
        "Пришлите одну фотографию отдельным сообщением."
        if has_access
        else PRINT_EVENT_ACCESS_REQUIRED_MESSAGE
    )
    await _tg_send_text(telegram, base, chat_id, text)


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
    command_id = uuid.uuid4().hex
    dispatch = await database.mark_print_job_dispatching(
        job_id=job_id,
        command_id=command_id,
    )
    dispatch_outcome = dispatch.get("outcome")
    if dispatch_outcome == "already_dispatching" or (
        dispatch_outcome != "dispatching"
        and dispatch.get("status") == "dispatching"
    ):
        raise RuntimeError(
            "задание уже отправляется или было отправлено"
        )
    if dispatch_outcome != "dispatching":
        raise RuntimeError(
            "задание не удалось зарезервировать для отправки: "
            f"{dispatch_outcome or 'неизвестный статус'}"
        )

    try:
        published_command_id = await _send_disk_command(
            "print_image",
            chat_id,
            metadata,
            command_id=command_id,
        )
    except Exception as exc:
        try:
            await database.mark_print_job_failed(
                command_id=command_id,
                last_error=str(exc),
            )
        except Exception:
            log.exception("Could not close unpublished print command=%s", command_id)
        raise RuntimeError("не удалось отправить команду на будку") from exc
    if published_command_id != command_id:
        await database.mark_print_job_failed(
            command_id=command_id,
            last_error="Диск вернул неожиданный command_id",
        )
        raise RuntimeError("Диск вернул неверный ID команды")
    return command_id


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
    allowlisted = _is_print_allowlisted(user_id)

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
    database_user_id: int | None = None
    database_job_created = False
    try:
        file_info = _telegram_print_file(message)
        if file_info is None:
            return False
        file_id, suffix, declared_size = file_info
        if (
            declared_size is not None
            and int(declared_size) > telegram_api.MAX_DOWNLOAD_FILE_SIZE
        ):
            raise ValueError("файл больше 20 МБ")

        event_name, event_token, cafe_mode = event_access.current_event()
        database_user_id = await _ensure_telegram_bot_user(sender)
        has_event_access = await _telegram_user_has_print_access(
            user_id,
            event_token=event_token,
            cafe_mode=cafe_mode,
        )
        if not has_event_access:
            await _tg_send_text(
                telegram,
                base,
                chat_id,
                f"❌ {PRINT_EVENT_ACCESS_REQUIRED_MESSAGE}",
            )
            return True
        created = await database.create_print_job(
            job_id=job_id,
            user_id=database_user_id,
            event_name=event_name,
            conversation_id=chat_id,
            source_message_id=message.get("message_id"),
        )
        if created["outcome"] == "already_open":
            await _tg_send_text(
                telegram,
                base,
                chat_id,
                "❌ Сначала завершите или отмените предыдущее задание печати.",
            )
            return True
        if created["outcome"] != "created":
            raise RuntimeError("не удалось создать задание печати")
        database_job_created = True

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
            "event_folder": event_name,
        })

        if preview.exact_ratio:
            metadata.update({
                "print_mode": "fit",
                "print_choice": "automatic_exact_ratio",
                "print_selected_at": time.time(),
            })
            if cafe_mode and not allowlisted:
                await asyncio.to_thread(
                    print_jobs.save_pending,
                    job_id,
                    suffix,
                    payload,
                    metadata,
                )
            current_event_name, current_event_token, current_cafe_mode = (
                event_access.current_event()
            )
            claim = await database.claim_print_job_choice(
                job_id=job_id,
                user_id=database_user_id,
                current_event_name=current_event_name,
                print_mode="fit",
                current_event_token=current_event_token,
                cafe_mode=current_cafe_mode,
                allowlisted=allowlisted,
                automatic=True,
            )
            if claim["outcome"] == "awaiting_authorization":
                await asyncio.to_thread(
                    print_jobs.update_pending,
                    job_id,
                    pending_status="awaiting_authorization",
                )
                try:
                    await _tg_send_text(
                        telegram,
                        base,
                        chat_id,
                        "✅ Фото подходит под формат 10×15. "
                        "Оплатите печать администратору; "
                        "фото ожидает его подтверждения.",
                    )
                except Exception:
                    # The durable job remains ready for the cashier even if
                    # Telegram cannot deliver this informational message.
                    log.exception(
                        "Could not notify user about exact cafe job=%s", job_id)
                await _send_admin_print_request(
                    telegram,
                    base,
                    job_id=job_id,
                    payload=payload,
                    metadata=metadata,
                    mode="fit",
                )
                return True
            if claim["outcome"] != "authorized":
                await database.cancel_print_job(
                    job_id=job_id,
                    user_id=database_user_id,
                    close_reason=claim["outcome"],
                )
                if claim["outcome"] == "cooldown":
                    retry_seconds = int(claim.get("retry_after_seconds") or 0)
                    raise ValueError(
                        "следующую фотографию можно напечатать через "
                        f"{max(1, (retry_seconds + 59) // 60)} мин."
                    )
                if claim["outcome"] == "access_denied":
                    raise ValueError(PRINT_EVENT_ACCESS_REQUIRED_MESSAGE)
                raise ValueError("event изменился; отправьте фотографию ещё раз")
            command_id = await _submit_print_job(
                job_id, user_id, suffix, payload, metadata, chat_id)
            try:
                await _tg_send_text(
                    telegram,
                    base,
                    chat_id,
                    "⏳ Фото подходит под формат 10×15 и передано на печать",
                )
            except Exception:
                # The durable command is already published. A Telegram failure
                # must not turn this into a rejection/retry path.
                log.exception(
                    "Could not report exact-ratio print submission job=%s",
                    job_id,
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
        choice_message_id = await _tg_send_photo(
            telegram,
            base,
            chat_id,
            preview.payload or b"",
            caption,
            keyboard,
            message.get("message_id"),
        )
        if choice_message_id is None:
            await asyncio.to_thread(print_jobs.delete_pending, job_id)
            raise RuntimeError("не удалось показать варианты печати")
        awaiting = await database.mark_print_job_awaiting_choice(
            job_id=job_id,
            choice_message_id=choice_message_id,
        )
        if awaiting["outcome"] != "awaiting_choice":
            raise RuntimeError("задание не перешло к выбору режима")
        log.info(
            "TG print choice requested job=%s user=%s source=%sx%s "
            "orientation=%s overflow=%s",
            job_id, user_id,
            preview.source_size[0], preview.source_size[1],
            preview.orientation, preview.overflow_axis,
        )
    except Exception as exc:
        log.warning("TG print job rejected user=%s job=%s: %s", user_id, job_id, exc)
        if database_job_created:
            try:
                await database.fail_print_job_before_dispatch(
                    job_id=job_id,
                    last_error=str(exc),
                )
            except Exception:
                log.exception("Could not close failed print job=%s", job_id)
        try:
            await asyncio.to_thread(print_jobs.delete_pending, job_id)
        except Exception:
            log.exception("Could not remove failed pending print job=%s", job_id)
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
    sender = callback.get("from") or {}
    user_id = sender.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id", user_id)

    if job_id in _print_callbacks_in_progress:
        await _tg_answer_callback(
            telegram, base, callback_id, "Задание уже обрабатывается")
        return True
    _print_callbacks_in_progress.add(job_id)
    try:
        try:
            database_user_id = await _ensure_telegram_bot_user(sender)
        except Exception as exc:
            log.exception("Could not prepare print callback job=%s", job_id)
            await _tg_answer_callback(
                telegram,
                base,
                callback_id,
                f"Печать временно недоступна: {exc}",
                show_alert=True,
            )
            return True

        if mode == "cancel":
            try:
                cancelled = await database.cancel_print_job(
                    job_id=job_id,
                    user_id=database_user_id,
                )
            except Exception as exc:
                log.exception("Could not cancel durable print job=%s", job_id)
                await _tg_answer_callback(
                    telegram,
                    base,
                    callback_id,
                    f"Не удалось отменить печать: {exc}",
                    show_alert=True,
                )
                return True

            if cancelled["outcome"] == "not_owner":
                await _tg_answer_callback(
                    telegram, base, callback_id,
                    "Отменить может только отправитель фото", show_alert=True)
                return True
            if cancelled["outcome"] != "cancelled":
                await _tg_answer_callback(
                    telegram, base, callback_id,
                    "Задание уже неактивно", show_alert=True)
                return True
            try:
                await asyncio.to_thread(print_jobs.delete_pending, job_id)
            except Exception:
                log.exception("Could not remove cancelled print files job=%s", job_id)

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

        try:
            event_name, event_token, cafe_mode = event_access.current_event()
        except Exception as exc:
            log.exception("Could not resolve current print event job=%s", job_id)
            await _tg_answer_callback(
                telegram,
                base,
                callback_id,
                f"Печать временно недоступна: {exc}",
                show_alert=True,
            )
            return True

        selected_text = _print_mode_text(mode)
        try:
            claim = await database.claim_print_job_choice(
                job_id=job_id,
                user_id=database_user_id,
                current_event_name=event_name,
                print_mode=mode,
                current_event_token=event_token,
                cafe_mode=cafe_mode,
                allowlisted=_is_print_allowlisted(user_id),
            )
        except Exception as exc:
            log.exception("Could not claim print choice job=%s mode=%s", job_id, mode)
            await _tg_answer_callback(
                telegram, base, callback_id,
                f"Не удалось сохранить выбор: {exc}", show_alert=True)
            return True

        outcome = claim["outcome"]
        if outcome == "not_owner":
            await _tg_answer_callback(
                telegram, base, callback_id,
                "Выбрать может только отправитель фото", show_alert=True)
            return True
        if outcome == "not_found":
            await _tg_answer_callback(
                telegram, base, callback_id,
                "Кнопка уже неактивна. Пришлите фото ещё раз.", show_alert=True)
            return True
        if outcome == "event_changed":
            await database.cancel_print_job(
                job_id=job_id,
                user_id=database_user_id,
                close_reason="event_changed",
            )
            try:
                await asyncio.to_thread(print_jobs.delete_pending, job_id)
            except Exception:
                log.exception("Could not remove stale-event print files job=%s", job_id)
            await _tg_answer_callback(
                telegram, base, callback_id,
                "Мероприятие уже изменилось. Пришлите фото ещё раз.",
                show_alert=True,
            )
            return True
        if outcome == "access_denied":
            await _tg_answer_callback(
                telegram, base, callback_id,
                PRINT_EVENT_ACCESS_REQUIRED_MESSAGE,
                show_alert=True,
            )
            return True
        if outcome == "cooldown":
            retry_seconds = int(claim.get("retry_after_seconds") or 0)
            retry_minutes = max(1, (retry_seconds + 59) // 60)
            await _tg_answer_callback(
                telegram, base, callback_id,
                f"Следующую фотографию можно напечатать через {retry_minutes} мин.",
                show_alert=True,
            )
            return True
        if outcome == "already_claimed":
            status = claim.get("status")
            text = (
                "Фото ожидает оплаты или подтверждения."
                if status == "awaiting_authorization"
                else "Задание уже обрабатывается или передано на печать."
            )
            await _tg_answer_callback(telegram, base, callback_id, text)
            return True
        if outcome == "awaiting_authorization":
            try:
                await _tg_answer_callback(
                    telegram, base, callback_id, "Вариант сохранён")
            except Exception:
                log.exception(
                    "Could not acknowledge cafe print choice job=%s", job_id)
            if isinstance(message.get("message_id"), int):
                try:
                    await _tg_edit_print_caption(
                        telegram,
                        base,
                        chat_id,
                        message["message_id"],
                        f"✅ Выбрано: {selected_text}.",
                    )
                except Exception:
                    log.exception(
                        "Could not update cafe print choice job=%s", job_id)
            try:
                await _tg_send_text(
                    telegram,
                    base,
                    chat_id,
                    "💳 Оплатите печать администратору.\n"
                    "После подтверждения оплаты фото будет добавлено в очередь.",
                )
            except Exception:
                log.exception(
                    "Could not send cafe payment instructions job=%s", job_id)
            try:
                payload, metadata = await asyncio.to_thread(
                    print_jobs.load_pending,
                    job_id,
                )
                metadata = await asyncio.to_thread(
                    print_jobs.update_pending,
                    job_id,
                    print_mode=mode,
                    print_choice="telegram_button",
                    print_selected_at=time.time(),
                    pending_status="awaiting_authorization",
                )
                await _send_admin_print_request(
                    telegram,
                    base,
                    job_id=job_id,
                    payload=payload,
                    metadata=metadata,
                    mode=mode,
                )
            except Exception as exc:
                log.exception("Could not request admin print approval job=%s", job_id)
                try:
                    await database.fail_print_job_before_dispatch(
                        job_id=job_id,
                        last_error=str(exc),
                    )
                except Exception:
                    log.exception("Could not close failed cafe print job=%s", job_id)
                try:
                    await asyncio.to_thread(print_jobs.delete_pending, job_id)
                except Exception:
                    log.exception("Could not remove failed cafe print job=%s", job_id)
                try:
                    await _tg_send_text(
                        telegram,
                        base,
                        chat_id,
                        "❌ Не удалось отправить запрос администратору. "
                        "Пришлите фото ещё раз.",
                    )
                except Exception:
                    log.exception(
                        "Could not report failed cafe approval request job=%s",
                        job_id,
                    )
                return True
            return True
        if outcome != "authorized":
            log.error("Unexpected print choice outcome job=%s outcome=%s", job_id, outcome)
            await _tg_answer_callback(
                telegram, base, callback_id,
                "Не удалось обработать выбранный вариант.", show_alert=True)
            return True

        try:
            await _tg_answer_callback(
                telegram, base, callback_id, "Принято, отправляю на печать")
        except Exception:
            log.exception(
                "Could not acknowledge authorized print callback job=%s", job_id)
        if isinstance(message.get("message_id"), int):
            try:
                await _tg_edit_print_caption(
                    telegram,
                    base,
                    chat_id,
                    message["message_id"],
                    f"✅ Выбрано: {selected_text}.\n⏳ Передаём на печать…",
                )
            except Exception:
                log.exception(
                    "Could not update authorized print caption job=%s", job_id)

        try:
            payload, metadata = await asyncio.to_thread(
                print_jobs.load_pending, job_id)
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
        except Exception as exc:
            log.exception(
                "TG print choice submission failed job=%s mode=%s", job_id, mode)
            try:
                await database.fail_print_job_before_dispatch(
                    job_id=job_id,
                    last_error=str(exc),
                )
            except Exception:
                log.exception("Could not close failed print job=%s", job_id)
            try:
                await asyncio.to_thread(print_jobs.delete_pending, job_id)
            except Exception:
                log.exception("Could not remove failed print files job=%s", job_id)
            await _tg_send_text(
                telegram,
                base,
                chat_id,
                f"❌ Не удалось передать фото на печать: {exc}. Пришлите фото ещё раз.",
            )
            return True

        if isinstance(message.get("message_id"), int):
            try:
                await _tg_edit_print_caption(
                    telegram,
                    base,
                    chat_id,
                    message["message_id"],
                    f"✅ Выбрано: {selected_text}. Фото передано на печать.",
                )
            except Exception:
                log.exception(
                    "Could not update submitted print caption job=%s", job_id)
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


def _telegram_message_id(value) -> int | None:
    try:
        message_id = int(value)
    except (TypeError, ValueError):
        return None
    return message_id if message_id > 0 else None


async def _tg_handle_print_admin_callback(
    telegram: aiohttp.ClientSession,
    base: str,
    callback: dict,
) -> bool:
    matched = PRINT_ADMIN_CALLBACK_RE.fullmatch(str(callback.get("data") or ""))
    if not matched:
        return False
    action, job_id = matched.groups()
    callback_id = callback.get("id")
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    admin_chat_id = (message.get("chat") or {}).get("id", sender.get("id"))
    admin_message_id = _telegram_message_id(message.get("message_id"))

    if not is_admin(sender.get("id")):
        await _tg_answer_callback(
            telegram,
            base,
            callback_id,
            "Это действие доступно только администратору.",
            show_alert=True,
        )
        return True
    if job_id in _print_callbacks_in_progress:
        await _tg_answer_callback(
            telegram, base, callback_id, "Задание уже обрабатывается")
        return True

    _print_callbacks_in_progress.add(job_id)
    try:
        try:
            current_event_name, _event_token, cafe_mode = (
                event_access.current_event()
            )
        except Exception as exc:
            log.exception("Could not resolve event for admin callback job=%s", job_id)
            await _tg_answer_callback(
                telegram, base, callback_id,
                f"Печать временно недоступна: {exc}", show_alert=True)
            return True
        if not cafe_mode:
            await _tg_answer_callback(
                telegram,
                base,
                callback_id,
                "Режим «Кафе» уже завершён; задание не отправлено.",
                show_alert=True,
            )
            return True

        if action == "reject":
            try:
                result = await database.reject_print_job_by_admin(
                    job_id=job_id,
                    current_event_name=current_event_name,
                )
            except Exception as exc:
                log.exception("Could not reject cafe print job=%s", job_id)
                await _tg_answer_callback(
                    telegram, base, callback_id,
                    f"Не удалось отклонить: {exc}", show_alert=True)
                return True
            if result.get("outcome") != "cancelled":
                await _tg_answer_callback(
                    telegram,
                    base,
                    callback_id,
                    "Задание уже неактивно или event изменился.",
                    show_alert=True,
                )
                return True
            try:
                await asyncio.to_thread(print_jobs.delete_pending, job_id)
            except Exception:
                log.exception("Could not remove rejected pending job=%s", job_id)
            try:
                await _tg_answer_callback(
                    telegram, base, callback_id, "Печать отклонена")
            except Exception:
                log.exception("Could not acknowledge rejected print job=%s", job_id)
            if admin_message_id is not None:
                try:
                    await _tg_edit_print_caption(
                        telegram,
                        base,
                        admin_chat_id,
                        admin_message_id,
                        "🚫 Печать отклонена администратором.",
                    )
                except Exception:
                    log.exception("Could not edit rejected admin request job=%s", job_id)
            user_chat_id = result.get("conversation_id")
            if user_chat_id:
                try:
                    await _tg_send_text(
                        telegram,
                        base,
                        user_chat_id,
                        "❌ Печать фотографии отклонена администратором.",
                    )
                except Exception:
                    log.exception("Could not notify user about rejection job=%s", job_id)
            return True

        try:
            result = await database.authorize_print_job_by_admin(
                job_id=job_id,
                current_event_name=current_event_name,
            )
        except Exception as exc:
            log.exception("Could not authorize cafe print job=%s", job_id)
            await _tg_answer_callback(
                telegram, base, callback_id,
                f"Не удалось разрешить печать: {exc}", show_alert=True)
            return True
        if result.get("outcome") != "authorized":
            await _tg_answer_callback(
                telegram,
                base,
                callback_id,
                "Задание уже обработано или event изменился.",
                show_alert=True,
            )
            return True

        user_chat_id = result.get("conversation_id")
        try:
            await _tg_answer_callback(
                telegram,
                base,
                callback_id,
                "Печать разрешена, передаю на будку",
            )
        except Exception:
            log.exception("Could not acknowledge approved print job=%s", job_id)
        if user_chat_id:
            try:
                await _tg_send_text(
                    telegram,
                    base,
                    user_chat_id,
                    "✅ Оплата подтверждена. Ваше фото добавлено в очередь "
                    "и скоро будет распечатано.",
                )
            except Exception:
                log.exception("Could not notify user about approval job=%s", job_id)
        try:
            payload, metadata = await asyncio.to_thread(print_jobs.load_pending, job_id)
            mode = str(result.get("print_mode") or metadata.get("print_mode") or "")
            metadata = await asyncio.to_thread(
                print_jobs.update_pending,
                job_id,
                pending_status="submitting",
                print_mode=mode,
                print_choice=str(metadata.get("print_choice") or "admin_approved"),
                print_authorized_at=time.time(),
            )
            suffix = str(metadata["source_suffix"])
            external_user_id = int(
                result.get("provider_user_id")
                or result.get("user_provider_user_id")
                or metadata["sender_id"]
            )
            command_id = await _submit_print_job(
                job_id,
                external_user_id,
                suffix,
                payload,
                metadata,
                user_chat_id,
            )
        except Exception as exc:
            log.exception("Could not submit admin-approved print job=%s", job_id)
            try:
                await database.fail_print_job_before_dispatch(
                    job_id=job_id,
                    last_error=str(exc),
                )
            except Exception:
                log.exception("Could not close admin-approved print job=%s", job_id)
            try:
                await asyncio.to_thread(print_jobs.delete_pending, job_id)
            except Exception:
                log.exception("Could not delete failed admin print job=%s", job_id)
            try:
                await _tg_send_text(
                    telegram,
                    base,
                    admin_chat_id,
                    f"❌ Job {job_id}: команда на будку не отправлена: {exc}",
                )
                if admin_message_id is not None:
                    await _tg_edit_print_caption(
                        telegram,
                        base,
                        admin_chat_id,
                        admin_message_id,
                        "❌ Печать была разрешена, но команда на будку не отправлена.",
                    )
            except Exception:
                log.exception("Could not report failed admin dispatch job=%s", job_id)
            if user_chat_id:
                try:
                    await _tg_send_text(
                        telegram,
                        base,
                        user_chat_id,
                        "❌ Не удалось передать фото на печать. "
                        "Обратитесь к администратору.",
                    )
                except Exception:
                    log.exception("Could not report failed cafe print job=%s", job_id)
            return True

        try:
            await asyncio.to_thread(print_jobs.delete_pending, job_id)
        except Exception:
            log.exception("Could not delete submitted cafe print job=%s", job_id)
        if admin_message_id is not None:
            try:
                await _tg_edit_print_caption(
                    telegram,
                    base,
                    admin_chat_id,
                    admin_message_id,
                    "✅ Печать разрешена и передана на будку.",
                )
            except Exception:
                log.exception("Could not edit approved admin request job=%s", job_id)
        log.info(
            "Admin approved cafe print job=%s user=%s command=%s",
            job_id,
            result.get("provider_user_id"),
            command_id,
        )
        return True
    finally:
        _print_callbacks_in_progress.discard(job_id)


async def _tg_run_update_command(
    telegram: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
) -> None:
    await _tg_send_text(telegram, base, chat_id, "⏳ Скачиваю полный релиз...")

    async def report_progress(message: str) -> None:
        await _tg_send_text(telegram, base, chat_id, message)

    try:
        updates_folder = CONFIG.get(
            "yadisk_updates_folder",
            "photobooth_system/updates",
        )
        result = await vps_update.publish_latest_release(
            updates_folder,
            report_progress,
        )
    except Exception as exc:
        log.exception("VPS update failed")
        result = f"❌ Ошибка: {exc}"
    await _tg_send_text(telegram, base, chat_id, result)


async def _tg_handle_admin_command(
    telegram: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    text: str,
) -> None:
    """Parse and execute one command from an already authorized admin."""
    try:
        parsed = admin_commands.parse(text)
    except (ValueError, RuntimeError) as exc:
        await _tg_send_text(telegram, base, chat_id, f"❌ {exc}")
        return

    if parsed is None:
        await _tg_show_keyboard(telegram, base, chat_id)
        return

    command, data = parsed

    # /update runs on the VPS. Every other recognized command goes to the booth
    # through the existing Yandex.Disk command channel.
    if command == "update":
        await _tg_run_update_command(telegram, base, chat_id)
        return

    try:
        await _send_disk_command(command, chat_id, data)
    except Exception as exc:
        await _tg_send_text(
            telegram,
            base,
            chat_id,
            admin_commands.failed_message(command, exc),
        )
        return

    await _tg_send_text(
        telegram,
        base,
        chat_id,
        admin_commands.sent_message(command, data),
    )


async def _tg_route_message_update(
    telegram: aiohttp.ClientSession,
    base: str,
    update: dict,
    message: dict,
) -> None:
    """Route one Telegram message in priority order: start, photo, text."""
    # /start must be stored before access is checked, so a freshly scanned
    # event QR grants access in this same update.
    if await _record_telegram_start(update, message):
        await _tg_reply_to_start(telegram, base, message)
        return

    # Photos and image documents use the same print flow for admins and users.
    # Cafe/event authorization stays inside that flow, not in this router.
    if await _tg_handle_print_message(telegram, base, message):
        return

    user_id = (message.get("from") or {}).get("id")
    if is_admin(user_id):
        chat_id = (message.get("chat") or {}).get("id", user_id)
        await _tg_handle_admin_command(
            telegram,
            base,
            chat_id,
            message.get("text", ""),
        )
        return

    await _tg_reply_to_plain_user_message(telegram, base, message)


async def _tg_route_callback_update(
    telegram: aiohttp.ClientSession,
    base: str,
    callback: dict,
) -> None:
    """Route one button press: print choice, cashier decision, admin command."""
    if await _tg_handle_print_callback(telegram, base, callback):
        return
    if await _tg_handle_print_admin_callback(telegram, base, callback):
        return

    user_id = (callback.get("from") or {}).get("id")
    if not is_admin(user_id):
        return

    # Admin command buttons carry the same text as their slash commands.
    await _tg_answer_callback(telegram, base, callback.get("id"))
    chat_id = (
        (callback.get("message") or {}).get("chat") or {}
    ).get("id", user_id)
    await _tg_handle_admin_command(
        telegram,
        base,
        chat_id,
        callback.get("data", ""),
    )


async def tg_poll_commands() -> None:
    """Long-poll Telegram and sequentially route bot messages and callbacks."""
    if not telegram_api.BOT_TOKEN or not TG_ADMIN:
        log.warning("Telegram bot/admin is not configured")
        return
    base = telegram_api.BOT_API_BASE
    offset = 0
    allowed_updates = ("message", "callback_query")
    async with aiohttp.ClientSession() as telegram:
        while True:
            try:
                data = await telegram_api.get_updates(
                    telegram,
                    base,
                    offset=offset,
                    allowed_updates=allowed_updates,
                )
                for update in data.get("result", []):
                    next_offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        await _tg_route_message_update(
                            telegram, base, update, message)
                    else:
                        callback = update.get("callback_query")
                        if callback:
                            await _tg_route_callback_update(
                                telegram, base, callback)
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


async def _tg_send_log(chat_id: str | int, payload: bytes) -> bool:
    return await _tg_send_document(
        chat_id, payload, "photobooth.log", "text/plain")


async def _handle_control_response(response: dict) -> bool:
    if response.get("command") == "print_image":
        try:
            if response.get("status") == "ok":
                transition = await database.mark_print_job_queued(
                    command_id=response["command_id"],
                )
                expected_status = "queued"
            else:
                transition = await database.mark_print_job_failed(
                    command_id=response["command_id"],
                    last_error=str(response.get("message") or "ошибка будки"),
                )
                expected_status = "failed"
        except Exception as exc:
            # Keep the durable control response on Disk and retry it instead of
            # losing the queue/cooldown transition in PostgreSQL.
            log.warning("Control: cannot persist print response: %s", exc)
            return False
        transition_outcome = transition.get("outcome")
        transition_is_expected = (
            transition_outcome == expected_status
            or (
                transition_outcome == "already_finished"
                and transition.get("status") == expected_status
            )
        )
        if not transition_is_expected:
            log.warning(
                "Control: unexpected print response transition command=%s "
                "outcome=%s status=%s",
                response.get("command_id"),
                transition_outcome,
                transition.get("status"),
            )
            return False
        if response.get("status") == "ok":
            # The user was already notified when the cashier authorized the
            # job (or when an authorized job was submitted).  The booth's
            # success response is still required for the durable `queued`
            # transition, but forwarding its text would duplicate that UI.
            return True

    chat_id = response.get("reply_chat_id") or TG_ADMIN
    if not chat_id or not telegram_api.BOT_TOKEN:
        return False

    event_public_url: str | None = None
    event_publish_error: str | None = None
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
        try:
            event_public_url = await yadisk_poll.publish_current_folder()
        except Exception as exc:
            event_publish_error = str(exc)
            log.warning("Control: event activated but sharing failed: %s", exc)

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
    response_message = response["message"]
    event_qr_png: bytes | None = None
    if response["status"] == "ok" and response["command"] == "set_event":
        event_name = str(response.get("event_folder") or "")
        if event_public_url:
            response_message += f"\n\nПубличная папка: {event_public_url}"
        elif event_publish_error:
            response_message += (
                "\n⚠️ Event активирован, но папку не удалось "
                f"опубликовать: {event_publish_error}"
            )
        if event_name and event_name != event_access.TECHNICAL_EVENT_NAME:
            try:
                start_link = event_access.start_link(event_name)
                event_qr_png = await asyncio.to_thread(
                    event_access.qr_code_png,
                    start_link,
                )
            except Exception as exc:
                log.warning("Control: event QR unavailable: %s", exc)
                response_message += f"\n⚠️ QR-код не создан: {exc}"
            else:
                response_message += f"\n\nСсылка для гостей:\n{start_link}"
    async with aiohttp.ClientSession() as telegram:
        telegram_base = telegram_api.BOT_API_BASE
        if event_qr_png is not None:
            message_id = await _tg_send_photo(
                telegram,
                telegram_base,
                chat_id,
                event_qr_png,
                f"{prefix} {response_message}",
                None,
                None,
                filename="event_access_qr.png",
                content_type="image/png",
                parse_mode=None,
            )
            delivered = message_id is not None
        else:
            delivered = await _tg_send_text(
                telegram,
                telegram_base,
                chat_id,
                f"{prefix} {response_message}",
            )
    if not delivered:
        return False
    return True


async def main() -> None:
    log.info("Database: applying pending migrations")
    await asyncio.to_thread(migrate.apply_migrations)
    log.info("Database: schema is ready")
    recovered_jobs = await database.recover_interrupted_print_jobs()
    if recovered_jobs:
        log.warning(
            "Database: closed %d interrupted local print jobs after restart",
            recovered_jobs,
        )

    yadisk_folder = CONFIG.get("yadisk_folder", "")
    control_folder = CONFIG.get("yadisk_control_folder", "photobooth_system/control")
    inbox_ready = await yadisk_poll.yadisk_init(
        yadisk_folder, control_folder, telegram_api.BOT_TOKEN, TG_CHAT)
    await yadisk_control.control_init(control_folder)
    if not inbox_ready:
        log.error("Yandex.Disk inbox poller is not configured")
    else:
        asyncio.create_task(yadisk_poll.yadisk_poll_loop(_handle_control_response))

    asyncio.create_task(tg_poll_commands())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
