"""Provider-neutral, durable AI image-edit flow."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from PIL import Image, ImageOps

import database
import event_access
import kie_api
import messenger_delivery
import print_flow
import print_jobs
import print_media
import runtime_config
from messaging import ReplyTarget


log = logging.getLogger(__name__)

AI_CAPTIONS = frozenset({"ai", "ии", "изменить"})
AI_JOBS_ROOT = Path(__file__).resolve().parent / "ai_pending_jobs"
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
RESULT_SUFFIX = ".jpg"
WORKER_FALLBACK_SECONDS = 5

_worker_wakeup = asyncio.Event()
_actions_in_progress: set[str] = set()


@dataclass(frozen=True)
class AiUpload:
    user: print_flow.PrintUser
    suffix: str
    download: Callable[[], Awaitable[bytes]]
    declared_size: int | None = None


@dataclass(frozen=True)
class AiAction:
    user: print_flow.PrintUser
    action: str
    job_id: str
    template_id: str | None = None
    action_id: str | None = None
    context: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not JOB_ID_RE.fullmatch(str(self.job_id or "")):
            raise ValueError("invalid AI image job id")


class AiUI(Protocol):
    async def send_text(self, user: print_flow.PrintUser, text: str) -> bool: ...

    async def send_ai_choice(
        self,
        upload: AiUpload,
        preview: bytes,
        job_id: str,
        templates: tuple[dict, ...],
    ) -> str | int | None: ...

    async def acknowledge(
        self,
        action: AiAction,
        text: str,
        *,
        alert: bool = False,
    ) -> None: ...

    async def update_choice(self, action: AiAction, text: str) -> None: ...


def is_ai_caption(value: object) -> bool:
    """Recognize only the three explicit captions that opt into AI editing."""
    return isinstance(value, str) and value.strip().casefold() in AI_CAPTIONS


def valid_template_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and runtime_config.AI_TEMPLATE_ID_RE.fullmatch(value) is not None
        and value not in {"cancel", "print"}
    )


def _normalized_job_id(value: str | uuid.UUID) -> str:
    try:
        job_id = uuid.UUID(str(value)).hex
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid AI image job id") from exc
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid AI image job id")
    return job_id


def _job_dir(job_id: str | uuid.UUID) -> Path:
    return AI_JOBS_ROOT / _normalized_job_id(job_id)


def _save_file(job_id: str, filename: str, payload: bytes) -> None:
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("empty AI image payload")
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / filename
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _save_source(job_id: str, suffix: str, payload: bytes) -> None:
    if not SUFFIX_RE.fullmatch(str(suffix or "")):
        raise ValueError("invalid AI source suffix")
    _save_file(job_id, f"source{suffix}", payload)


def _load_source(job_id: str, suffix: str) -> bytes:
    if not SUFFIX_RE.fullmatch(str(suffix or "")):
        raise ValueError("invalid AI source suffix")
    payload = (_job_dir(job_id) / f"source{suffix}").read_bytes()
    if not payload:
        raise ValueError("stored AI source is empty")
    return payload


def _save_result(job_id: str, payload: bytes) -> None:
    _save_file(job_id, f"result{RESULT_SUFFIX}", payload)


def _load_result(job_id: str, suffix: str) -> bytes:
    if suffix != RESULT_SUFFIX:
        raise ValueError("invalid AI result suffix")
    payload = (_job_dir(job_id) / f"result{suffix}").read_bytes()
    if not payload:
        raise ValueError("stored AI result is empty")
    return payload


def _delete_source(job_id: str, suffix: str) -> None:
    if SUFFIX_RE.fullmatch(str(suffix or "")):
        (_job_dir(job_id) / f"source{suffix}").unlink(missing_ok=True)


def _delete_job_files(job_id: str) -> None:
    job_dir = _job_dir(job_id)
    if not job_dir.is_dir():
        return
    for path in job_dir.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
    job_dir.rmdir()


def _prepare_kie_input(payload: bytes, suffix: str) -> tuple[bytes, str, str]:
    """Return an upload under 10 MB and its print-oriented aspect ratio."""
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("AI source is empty")
    if not SUFFIX_RE.fullmatch(str(suffix or "")):
        raise ValueError("invalid AI source suffix")
    with Image.open(io.BytesIO(payload)) as source:
        if source.width * source.height > print_jobs.MAX_PRINT_PIXELS:
            raise ValueError("изображение слишком большое")
        oriented = ImageOps.exif_transpose(source)
        try:
            aspect_ratio = "3:2" if oriented.width >= oriented.height else "2:3"
            if len(payload) <= kie_api.KIE_INPUT_MAX_BYTES:
                return payload, suffix, aspect_ratio
        finally:
            if oriented is not source:
                oriented.close()

    encoded, _quality, _max_edge = print_media.compress_jpeg(
        payload,
        max_bytes=kie_api.KIE_INPUT_MAX_BYTES,
    )
    return encoded, ".jpg", aspect_ratio


def _print_ready_jpeg(payload: bytes) -> bytes:
    """Turn any generated raster into one exact 10x15 print image."""
    with Image.open(io.BytesIO(payload)) as source:
        if source.width * source.height > print_jobs.MAX_PRINT_PIXELS:
            raise ValueError("изображение слишком большое")
        source.seek(0)
        oriented = ImageOps.exif_transpose(source)
        try:
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, (255, 255, 255))
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
            else:
                rgb = oriented.convert("RGB")
        finally:
            if oriented is not source:
                oriented.close()

    try:
        target_size = (
            print_jobs.LANDSCAPE_PRINT_SIZE
            if rgb.width > rgb.height
            else print_jobs.PORTRAIT_PRINT_SIZE
        )
        fitted = ImageOps.fit(
            rgb,
            target_size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    finally:
        rgb.close()
    try:
        output = io.BytesIO()
        fitted.save(output, "JPEG", quality=95, subsampling=0)
        return output.getvalue()
    finally:
        fitted.close()


def _template_by_id(template_id: str) -> dict | None:
    settings = runtime_config.ai_image_edit_settings()
    for template in settings["templates"]:
        if template["id"] == template_id:
            return template
    return None


async def _safe_send_text(
    ui: AiUI,
    user: print_flow.PrintUser,
    text: str,
) -> None:
    try:
        await ui.send_text(user, text)
    except Exception:
        log.exception(
            "Could not send AI status provider=%s user=%s",
            user.provider,
            user.provider_user_id,
        )


async def _safe_ack(
    ui: AiUI,
    action: AiAction,
    text: str,
    *,
    alert: bool = False,
) -> None:
    try:
        await ui.acknowledge(action, text, alert=alert)
    except Exception:
        log.exception("Could not acknowledge AI action job=%s", action.job_id)


async def _safe_update(ui: AiUI, action: AiAction, text: str) -> None:
    try:
        await ui.update_choice(action, text)
    except Exception:
        log.exception("Could not update AI choice job=%s", action.job_id)


async def handle_upload(upload: AiUpload, ui: AiUI) -> bool:
    """Store one explicitly requested source and show every configured preset."""
    user = upload.user
    job_id = uuid.uuid4().hex
    job_created = False
    try:
        event_name, event_token, cafe_mode = event_access.current_event()
        privileged = user.is_admin or user.allowlisted
        if cafe_mode and not privileged:
            return False

        settings = runtime_config.ai_image_edit_settings()
        if not settings["enabled"]:
            await ui.send_text(user, "❌ AI-обработка сейчас отключена.")
            return True
        if not SUFFIX_RE.fullmatch(str(upload.suffix or "")):
            raise ValueError("неподдерживаемый формат изображения")
        if (
            upload.declared_size is not None
            and int(upload.declared_size) > print_media.MAX_PRINT_FILE_SIZE
        ):
            raise ValueError(
                f"файл больше {print_media.MAX_PRINT_FILE_SIZE_MB} МБ"
            )

        database_user_id = await print_flow.ensure_user(user)
        if not await print_flow.user_has_access(
            user,
            event_token=event_token,
            cafe_mode=cafe_mode,
        ):
            await ui.send_text(
                user,
                f"❌ {print_flow.EVENT_ACCESS_REQUIRED_MESSAGE}",
            )
            return True

        created = await database.create_ai_image_job(
            job_id=job_id,
            user_id=database_user_id,
            event_name=event_name,
            conversation_id=user.conversation_id,
            source_message_id=user.source_message_id,
            source_suffix=upload.suffix,
            bypass_cooldown=privileged,
        )
        for stale_job_id in created.get("stale_job_ids", ()):
            await asyncio.to_thread(_delete_job_files, stale_job_id)
        if created.get("outcome") == "cooldown":
            retry_seconds = int(created.get("retry_after_seconds") or 0)
            await ui.send_text(
                user,
                "❌ Следующую AI-обработку можно запустить через "
                f"{max(1, (retry_seconds + 59) // 60)} мин.",
            )
            return True
        if created.get("outcome") == "already_open":
            await ui.send_text(
                user,
                "❌ Сначала выберите эффект для предыдущего AI-фото.",
            )
            return True
        if created.get("outcome") != "created":
            raise RuntimeError("не удалось создать AI-задание")
        job_created = True

        await ui.send_text(user, "⏳ Загружаем фотографию…")
        payload = await upload.download()
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("мессенджер прислал пустой файл")
        if len(payload) > print_media.MAX_PRINT_FILE_SIZE:
            raise ValueError(
                f"файл больше {print_media.MAX_PRINT_FILE_SIZE_MB} МБ"
            )
        preview = await asyncio.to_thread(print_media.jpeg_preview, payload)
        await asyncio.to_thread(_save_source, job_id, upload.suffix, payload)
        choice_message_id = await ui.send_ai_choice(
            upload,
            preview,
            job_id,
            settings["templates"],
        )
        if choice_message_id is None:
            raise RuntimeError("не удалось отправить меню AI-эффектов")
        awaiting = await database.mark_ai_image_job_awaiting_template(
            job_id=job_id,
            choice_message_id=choice_message_id,
        )
        if awaiting.get("outcome") != "awaiting_template":
            raise RuntimeError("AI-задание не перешло к выбору эффекта")
        log.info(
            "%s AI template choice requested job=%s user=%s templates=%d",
            user.provider.upper(),
            job_id,
            user.provider_user_id,
            len(settings["templates"]),
        )
    except Exception as exc:
        log.warning(
            "%s AI upload rejected user=%s job=%s: %s",
            user.provider.upper(),
            user.provider_user_id,
            job_id,
            exc,
        )
        if job_created:
            try:
                await database.fail_ai_image_job(job_id=job_id, last_error=str(exc))
            except Exception:
                log.exception("Could not close failed AI upload job=%s", job_id)
            await asyncio.to_thread(_delete_job_files, job_id)
        await _safe_send_text(ui, user, print_flow.rejected_photo_message(exc))
    return True


async def _handle_cancel(action: AiAction, ui: AiUI) -> bool:
    database_user_id = await print_flow.ensure_user(action.user)
    result = await database.cancel_ai_image_job(
        job_id=action.job_id,
        user_id=database_user_id,
    )
    if result.get("outcome") == "not_owner":
        await _safe_ack(ui, action, "Отменить может только отправитель", alert=True)
        return True
    if result.get("outcome") != "cancelled":
        await _safe_ack(ui, action, "Это AI-задание уже неактивно", alert=True)
        return True
    await asyncio.to_thread(_delete_job_files, action.job_id)
    await _safe_ack(ui, action, "AI-обработка отменена")
    await _safe_update(ui, action, "🚫 AI-обработка отменена.")
    return True


async def _handle_template(action: AiAction, ui: AiUI) -> bool:
    template = _template_by_id(str(action.template_id or ""))
    if template is None:
        await _safe_ack(ui, action, "Этот эффект больше недоступен", alert=True)
        return True
    settings = runtime_config.ai_image_edit_settings()
    if not settings["enabled"]:
        await _safe_ack(ui, action, "AI-обработка сейчас отключена", alert=True)
        return True

    event_name, event_token, cafe_mode = event_access.current_event()
    if not await print_flow.user_has_access(
        action.user,
        event_token=event_token,
        cafe_mode=cafe_mode,
    ):
        await _safe_ack(
            ui,
            action,
            print_flow.EVENT_ACCESS_REQUIRED_MESSAGE,
            alert=True,
        )
        return True
    database_user_id = await print_flow.ensure_user(action.user)
    result = await database.queue_ai_image_job(
        job_id=action.job_id,
        user_id=database_user_id,
        current_event_name=event_name,
        template_id=template["id"],
        template_label=template["button"],
        prompt=template["prompt"],
    )
    outcome = result.get("outcome")
    if outcome == "not_owner":
        await _safe_ack(ui, action, "Выбрать может только отправитель", alert=True)
        return True
    if outcome == "event_changed":
        await _safe_ack(ui, action, "Мероприятие уже изменилось", alert=True)
        return True
    if outcome != "queued":
        await _safe_ack(ui, action, "Это AI-задание уже неактивно", alert=True)
        return True

    _worker_wakeup.set()
    await _safe_ack(ui, action, "AI-эффект выбран")
    await _safe_update(
        ui,
        action,
        f"⏳ Выбран эффект «{template['button']}». Обрабатываем фото…",
    )
    return True


async def _handle_print(action: AiAction, ui: AiUI) -> bool:
    user = action.user
    event_name, event_token, cafe_mode = event_access.current_event()
    if not await print_flow.user_has_access(
        user,
        event_token=event_token,
        cafe_mode=cafe_mode,
    ):
        await _safe_ack(
            ui,
            action,
            print_flow.EVENT_ACCESS_REQUIRED_MESSAGE,
            alert=True,
        )
        return True
    database_user_id = await print_flow.ensure_user(user)
    if await database.user_has_open_print_job(user_id=database_user_id):
        await _safe_ack(
            ui,
            action,
            "Сначала завершите предыдущее задание печати",
            alert=True,
        )
        return True
    if not cafe_mode and not user.allowlisted:
        retry_after = await database.print_cooldown_retry_after(
            user_id=database_user_id,
            event_name=event_name,
        )
        if retry_after > 0:
            minutes = max(1, (retry_after + 59) // 60)
            await _safe_ack(
                ui,
                action,
                f"Следующую фотографию можно напечатать через {minutes} мин.",
                alert=True,
            )
            return True

    claim = await database.claim_ai_image_job_for_print(
        job_id=action.job_id,
        user_id=database_user_id,
        current_event_name=event_name,
    )
    outcome = claim.get("outcome")
    if outcome == "not_owner":
        await _safe_ack(ui, action, "Напечатать может только отправитель", alert=True)
        return True
    if outcome == "event_changed":
        await _safe_ack(ui, action, "Мероприятие уже изменилось", alert=True)
        return True
    if outcome != "printing":
        await _safe_ack(ui, action, "Этот результат уже неактивен", alert=True)
        return True

    try:
        payload = await asyncio.to_thread(
            _load_result,
            action.job_id,
            str(claim.get("result_suffix") or ""),
        )

        async def download() -> bytes:
            return payload

        upload = print_flow.PrintUpload(
            user=user,
            suffix=RESULT_SUFFIX,
            declared_size=len(payload),
            download=download,
            metadata={
                "ai_image_job_id": action.job_id,
                "ai_template_id": claim.get("template_id"),
                "ai_template_label": claim.get("template_label"),
            },
        )
        await _safe_ack(ui, action, "Передаём фото в печать")
        await _safe_update(ui, action, "🖨 AI-фото передаётся в печать…")
        await print_flow.handle_upload(upload, ui)
        await database.mark_ai_image_job_print_submitted(job_id=action.job_id)
        await asyncio.to_thread(_delete_job_files, action.job_id)
    except Exception as exc:
        log.exception("Could not submit AI result to print job=%s", action.job_id)
        await database.restore_ai_image_job_ready(
            job_id=action.job_id,
            last_error=str(exc),
        )
        await _safe_send_text(
            ui,
            user,
            "❌ Не удалось передать AI-фото в печать. Нажмите кнопку ещё раз.",
        )
    return True


async def handle_action(action: AiAction, ui: AiUI) -> bool:
    if action.action not in {"template", "cancel", "print"}:
        return False
    action_key = f"{action.action}:{action.job_id}"
    if action_key in _actions_in_progress:
        await _safe_ack(ui, action, "Задание уже обрабатывается")
        return True
    _actions_in_progress.add(action_key)
    try:
        if action.action == "cancel":
            return await _handle_cancel(action, ui)
        if action.action == "print":
            return await _handle_print(action, ui)
        return await _handle_template(action, ui)
    except Exception as exc:
        log.exception("AI action failed action=%s job=%s", action.action, action.job_id)
        await _safe_ack(ui, action, f"AI временно недоступен: {exc}", alert=True)
        return True
    finally:
        _actions_in_progress.discard(action_key)


def _result_keyboard(provider: str, job_id: str) -> dict:
    if provider == "telegram":
        return {
            "inline_keyboard": [[{
                "text": "🖨 РАСПЕЧАТАТЬ",
                "callback_data": f"ai:p:{job_id}",
            }]],
        }
    if provider == "vk":
        payload = json.dumps(
            {"type": "ai_print", "action": "print", "job_id": job_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "inline": True,
            "buttons": [[{
                "action": {
                    "type": "callback",
                    "label": "🖨 РАСПЕЧАТАТЬ",
                    "payload": payload,
                },
                "color": "primary",
            }]],
        }
    raise ValueError(f"unsupported AI result provider: {provider}")


async def _deliver_result(job: dict) -> None:
    payload = await asyncio.to_thread(
        _load_result,
        job["job_id"],
        str(job.get("result_suffix") or ""),
    )
    preview = await asyncio.to_thread(print_media.jpeg_preview, payload)
    target = ReplyTarget(job["provider"], job["conversation_id"])
    caption = f"✅ Готово: {job['template_label']}"
    delivered = await messenger_delivery.send_photo(
        target,
        preview,
        caption,
        filename="ai_result.jpg",
        content_type="image/jpeg",
        keyboard=_result_keyboard(target.provider, job["job_id"]),
    )
    if not delivered:
        raise RuntimeError("messenger did not accept AI result")
    await database.mark_ai_image_job_delivered(job_id=job["job_id"])


async def _notify_failed_job(job: dict) -> None:
    try:
        await messenger_delivery.send_text(
            ReplyTarget(job["provider"], job["conversation_id"]),
            "❌ Не удалось обработать AI-фото. Пришлите его ещё раз.",
        )
    except Exception:
        log.exception("Could not notify about failed AI job=%s", job["job_id"])


async def _fail_worker_job(job: dict, exc: Exception) -> None:
    log.error(
        "AI image job failed job=%s",
        job["job_id"],
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    try:
        await database.fail_ai_image_job(
            job_id=job["job_id"],
            last_error=str(exc),
        )
    except Exception:
        log.exception("Could not close failed AI job=%s", job["job_id"])
    await asyncio.to_thread(_delete_job_files, job["job_id"])
    await _notify_failed_job(job)


async def _send_link_fallback(job: dict, url: str, reason: str) -> None:
    job_id = job["job_id"]
    log.warning(
        "AI image job %s sending link as fallback. reason=%s",
        job_id,
        reason,
    )
    await database.fail_ai_image_job(
        job_id=job_id,
        last_error=f"Отправлена ссылка: {reason}",
    )
    await asyncio.to_thread(_delete_job_files, job_id)
    message = (
        "🤖 Не удалось обработать ваше изображение, но его можно скачать по ссылке:\n"
        f"{url}"
    )
    try:
        await messenger_delivery.send_text(
            ReplyTarget(job["provider"], job["conversation_id"]),
            message,
        )
    except Exception:
        log.exception("Could not send AI fallback link for job=%s", job_id)


async def _finish_job(job: dict, generated: bytes, result_url: str) -> None:
    try:
        result = await asyncio.to_thread(_print_ready_jpeg, generated)
    except (OSError, SyntaxError) as exc:
        is_expiring = job["provider_deadline_at"] - datetime.now(
            timezone.utc
        ) < timedelta(minutes=1)
        raise kie_api.KieApiError(
            f"Kie result download: изображение повреждено ({len(generated)} bytes)",
            retryable=not is_expiring,
            result_url=result_url,
        ) from exc
    await asyncio.to_thread(_save_result, job["job_id"], result)
    ready = await database.mark_ai_image_job_ready(
        job_id=job["job_id"],
        result_suffix=RESULT_SUFFIX,
    )
    if ready.get("outcome") != "ready":
        await asyncio.to_thread(_delete_job_files, job["job_id"])
        return
    await asyncio.to_thread(
        _delete_source,
        job["job_id"],
        job["source_suffix"],
    )
    job["result_suffix"] = RESULT_SUFFIX
    await _deliver_result(job)
    log.info(
        "AI image result delivered job=%s user=%s template=%s",
        job["job_id"],
        job["provider_user_id"],
        job["template_id"],
    )


async def _submit_kie_job(job: dict) -> None:
    source = await asyncio.to_thread(
        _load_source,
        job["job_id"],
        job["source_suffix"],
    )
    upload_payload, upload_suffix, aspect_ratio = await asyncio.to_thread(
        _prepare_kie_input,
        source,
        job["source_suffix"],
    )
    if len(upload_payload) != len(source):
        log.info(
            "Kie input compressed job=%s %.1f -> %.1f MB",
            job["job_id"],
            len(source) / 1048576,
            len(upload_payload) / 1048576,
        )
    source_url = await kie_api.upload_image(
        upload_payload,
        filename=f"{job['job_id']}{upload_suffix}",
    )
    task_id = await kie_api.create_image_task(
        prompt=job["prompt"],
        input_url=source_url,
        aspect_ratio=aspect_ratio,
    )
    submitted = await database.mark_ai_image_job_submitted(
        job_id=job["job_id"],
        provider_task_id=task_id,
        poll_seconds=kie_api.KIE_TASK_POLL_SECONDS,
        timeout_seconds=kie_api.KIE_TASK_TIMEOUT_SECONDS,
    )
    if submitted.get("outcome") != "submitted":
        await asyncio.to_thread(_delete_job_files, job["job_id"])
        return
    await asyncio.to_thread(
        _delete_source,
        job["job_id"],
        job["source_suffix"],
    )
    log.info(
        "Kie image task submitted job=%s task=%s",
        job["job_id"],
        task_id,
    )


async def _poll_kie_job(job: dict) -> None:
    deadline = job.get("provider_deadline_at")
    if not isinstance(deadline, datetime):
        raise RuntimeError("Kie task has no deadline")
    if deadline <= datetime.now(timezone.utc):
        raise RuntimeError("Kie generation timed out")
    task_id = str(job.get("provider_task_id") or "").strip()
    if not task_id:
        raise RuntimeError("Kie task id is missing")
    details = await kie_api.get_task_details(task_id)
    result_url = kie_api.task_result_url(details)
    if result_url is None:
        return
    generated = await kie_api.download_result(
        result_url,
        max_bytes=print_media.MAX_PRINT_FILE_SIZE,
    )
    await _finish_job(job, generated, result_url)


async def _process_job(job: dict) -> None:
    try:
        current_event_name, _token, _cafe = event_access.current_event()
        if job["event_name"] != current_event_name:
            raise RuntimeError("мероприятие изменилось до начала AI-обработки")
        if job.get("provider_task_id"):
            await _poll_kie_job(job)
        else:
            await _submit_kie_job(job)
    except asyncio.CancelledError:
        raise
    except kie_api.KieApiError as exc:
        if exc.retryable:
            log_message = "AI image job will be retried job=%s: %s"
            if job.get("provider_task_id"):
                log_message = "Kie poll will retry job=%s task=%s: %s"
                log.warning(log_message, job["job_id"], job["provider_task_id"], exc)
            else:
                log.warning(log_message, job["job_id"], exc)
            return
        if exc.result_url:
            await _send_link_fallback(job, exc.result_url, str(exc))
        else:
            await _fail_worker_job(job, exc)
    except Exception as exc:
        await _fail_worker_job(job, exc)


async def _deliver_recovered_result(job: dict) -> None:
    try:
        await _deliver_result(job)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _fail_worker_job(job, exc)


async def recover_interrupted_jobs() -> dict:
    recovered = await database.recover_interrupted_ai_image_jobs()
    for job_id in recovered["failed_job_ids"]:
        await asyncio.to_thread(_delete_job_files, job_id)
    return recovered


async def worker_loop() -> None:
    """Process local work and poll only provider tasks that are due."""
    while True:
        _worker_wakeup.clear()
        try:
            undelivered = await database.next_undelivered_ai_image_job()
            if undelivered is not None:
                await _deliver_recovered_result(undelivered)
                continue
            job = await database.claim_next_ai_image_job(
                poll_seconds=kie_api.KIE_TASK_POLL_SECONDS,
            )
            if job is not None:
                await _process_job(job)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("AI worker iteration failed")

        try:
            await asyncio.wait_for(
                _worker_wakeup.wait(),
                timeout=WORKER_FALLBACK_SECONDS,
            )
        except TimeoutError:
            pass
