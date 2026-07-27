"""Provider-neutral photo-print workflow shared by Telegram and VK."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

import admin_notifications
import database
import event_access
import messenger_delivery
import print_jobs
import print_media
import telegram_print_archive
import yadisk_control
import yadisk_poll
from messaging import ReplyTarget


log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
EVENT_ACCESS_REQUIRED_MESSAGE = (
    "Для печати сначала отсканируйте QR-код текущего мероприятия."
)
ADMIN_REQUEST_FAILED_MESSAGE = (
    "❌ Не удалось отправить запрос администраторам. Пришлите фото ещё раз."
)
PRINT_CHOICE_LABELS = {
    "fit": "1️⃣ Как есть",
    "fill": "2️⃣ Увеличить",
    "cancel": "❌ Отмена",
}
_actions_in_progress: set[str] = set()
_background_tasks: set[asyncio.Task] = set()


class PreviewDeliveryError(RuntimeError):
    """A valid photo whose messenger choice card could not be delivered."""


@dataclass(frozen=True)
class PrintUser:
    """One messenger identity normalized by a transport adapter."""

    provider: str
    provider_user_id: int
    conversation_id: str | int
    source_message_id: str | int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    allowlisted: bool = False
    is_admin: bool = False
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_user_id, int)
            or isinstance(self.provider_user_id, bool)
            or self.provider_user_id <= 0
        ):
            raise ValueError("messenger user id must be a positive integer")
        target = ReplyTarget(self.provider, self.conversation_id)
        object.__setattr__(self, "provider", target.provider)
        object.__setattr__(self, "conversation_id", target.conversation_id)

    @property
    def target(self) -> ReplyTarget:
        return ReplyTarget(self.provider, self.conversation_id)

    @property
    def display_name(self) -> str:
        return " ".join(
            part.strip()
            for part in (str(self.first_name or ""), str(self.last_name or ""))
            if part.strip()
        )[:100]


@dataclass(frozen=True)
class PrintUpload:
    """A lazily downloadable image normalized by a transport adapter."""

    user: PrintUser
    suffix: str
    download: Callable[[], Awaitable[bytes]]
    declared_size: int | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PrintAction:
    """A normalized user-choice or cashier action."""

    user: PrintUser
    action: str
    job_id: str
    action_id: str | None = None
    context: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not JOB_ID_RE.fullmatch(str(self.job_id or "")):
            raise ValueError("invalid print job id")


class PrintUI(Protocol):
    """The small provider-specific surface needed by the shared workflow."""

    async def send_text(self, user: PrintUser, text: str) -> bool: ...

    async def send_choice(
        self,
        upload: PrintUpload,
        preview: bytes,
        job_id: str,
    ) -> str | int | None: ...

    async def acknowledge(
        self,
        action: PrintAction,
        text: str,
        *,
        alert: bool = False,
    ) -> None: ...

    async def update_choice(self, action: PrintAction, text: str) -> None: ...

    async def update_admin(self, action: PrintAction, status: str) -> None: ...


def mode_text(mode: str) -> str:
    if mode == "fit":
        return "как есть, с белыми полями"
    if mode == "fill":
        return "увеличить под размер, края обрежутся"
    raise ValueError("неизвестный вариант печати")


def connected_event_message(event_name: str) -> str:
    return (
        f'✅ Вы подключены к мероприятию «{event_name}». '
        "Теперь можно отправить фотографию."
    )


def print_choice_message(*, telegram_html: bool) -> str:
    title = f"Фото не совпадает с форматом {print_media.PRINT_FORMAT_LABEL}."
    if telegram_html:
        return (
            f"<b>{title}</b>\n\n"
            "1 — <b>как есть</b> — будут белые поля.\n"
            "2 — <b>увеличить под размер</b> — обрежутся затемнённые края."
        )
    return (
        f"⚠️ {title.upper()}\n\n"
        "1 — КАК ЕСТЬ — будут белые поля.\n"
        "2 — УВЕЛИЧИТЬ ПОД РАЗМЕР — обрежутся затемнённые края."
    )


def rejected_photo_message(error: Exception | str) -> str:
    return f"❌ Фото не принято: {error}"


def admin_request_text(
    job_id: str,
    metadata: dict,
    mode: str,
    *,
    telegram_html: bool = False,
) -> str:
    event_label = str(
        metadata.get("event_folder") or event_access.TECHNICAL_EVENT_NAME
    )
    if telegram_html:
        return (
            f"<b>Новая печать в «{html.escape(event_label)}»</b>\n"
            f"Job: <code>{html.escape(job_id)}</code>\n"
            f"Выбор: <b>{html.escape(mode_text(mode))}</b>\n"
            f"{print_media.sender_caption(metadata, telegram_html=True, include_filename=False)}"
        )
    return (
        f"Новая печать в «{event_label}»\n"
        f"Job: {job_id}\n"
        f"Выбор: {mode_text(mode)}\n"
        f"{print_media.sender_caption(metadata, include_filename=False)}"
    )


def _admin_result_metadata(result: dict, metadata: dict | None = None) -> dict:
    source = dict(metadata or {})
    first_name = str(result.get("first_name") or "").strip()
    last_name = str(result.get("last_name") or "").strip()
    result_name = " ".join(part for part in (first_name, last_name) if part)
    if result_name:
        source["sender_name"] = result_name
    source["provider"] = str(
        result.get("provider") or source.get("provider") or "messenger"
    )
    source["sender_id"] = (
        result.get("provider_user_id")
        or result.get("user_provider_user_id")
        or source.get("sender_id")
        or "—"
    )
    if result.get("username"):
        source["username"] = result["username"]
    return source


def admin_job_result_text(
    result: dict,
    status: str,
    *,
    metadata: dict | None = None,
) -> str:
    """Build one compact final job notification for every administrator."""
    event_name = str(
        result.get("event_name")
        or (metadata or {}).get("event_folder")
        or "—"
    )
    job_id = str(result.get("job_id") or (metadata or {}).get("job_id") or "—")
    sender = _admin_result_metadata(result, metadata)
    return (
        f"{status}\n"
        f"Мероприятие: «{event_name}»\n"
        f"Job: {job_id}\n"
        f"{print_media.sender_caption(sender, include_filename=False)}"
    )


async def _request_admin_approval(
    *,
    job_id: str,
    payload: bytes,
    metadata: dict,
    mode: str,
) -> None:
    preview = await asyncio.to_thread(print_media.jpeg_preview, payload)
    delivery = await admin_notifications.send_print_approval(
        job_id=job_id,
        preview=preview,
        caption=admin_request_text(job_id, metadata, mode),
        telegram_caption=admin_request_text(
            job_id,
            metadata,
            mode,
            telegram_html=True,
        ),
    )
    if not delivery.delivered_targets:
        raise RuntimeError("не настроен или недоступен ни один администратор")
    if delivery.failed_targets:
        log.warning(
            "Print approval delivered only partially job=%s failed=%s",
            job_id,
            ",".join(target.provider for target in delivery.failed_targets),
        )


async def _safe_send_text(
    ui: PrintUI,
    user: PrintUser,
    text: str,
) -> bool:
    try:
        return await ui.send_text(user, text)
    except Exception:
        log.exception(
            "Could not send optional print status provider=%s user=%s",
            user.provider,
            user.provider_user_id,
        )
        return False


async def user_has_access(
    user: PrintUser,
    *,
    event_token: str | None,
    cafe_mode: bool,
) -> bool:
    if cafe_mode or user.allowlisted:
        return True
    if event_token is None:
        raise RuntimeError("у текущего мероприятия отсутствует токен доступа")
    return await database.user_has_current_start_parameter(
        provider=user.provider,
        provider_user_id=user.provider_user_id,
        start_parameter=event_token,
    )


async def ensure_user(user: PrintUser) -> int:
    return await database.ensure_bot_user(
        provider=user.provider,
        provider_user_id=user.provider_user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


def _metadata(
    upload: PrintUpload,
    *,
    job_id: str,
    event_name: str,
    payload: bytes,
    preview,
) -> dict:
    user = upload.user
    metadata = dict(user.metadata)
    metadata.update(upload.metadata)
    metadata.update({
        "job_id": job_id,
        "provider": user.provider,
        "sender_id": user.provider_user_id,
        "sender_name": user.display_name,
        "username": str(user.username or "")[:64],
        "conversation_id": str(user.conversation_id),
        "source_message_id": (
            str(user.source_message_id)
            if user.source_message_id is not None
            else None
        ),
        "reply_target": user.target.to_dict(),
        "source_size": len(payload),
        "source_width": preview.source_size[0],
        "source_height": preview.source_size[1],
        "print_orientation": preview.orientation,
        "print_target_size": list(preview.target_size),
        "event_folder": event_name,
    })
    return metadata


async def _delete_pending(job_id: str) -> None:
    try:
        await asyncio.to_thread(print_jobs.delete_pending, job_id)
    except Exception:
        log.exception("Could not delete pending print files job=%s", job_id)


async def _fail_before_dispatch(job_id: str, error: Exception | str) -> None:
    try:
        await database.fail_print_job_before_dispatch(
            job_id=job_id,
            last_error=str(error),
        )
    except Exception:
        log.exception("Could not close failed print job=%s", job_id)
    await _delete_pending(job_id)


def _claim_error(claim: dict) -> str:
    outcome = claim.get("outcome")
    if outcome == "cooldown":
        retry_seconds = int(claim.get("retry_after_seconds") or 0)
        return (
            "следующую фотографию можно напечатать через "
            f"{max(1, (retry_seconds + 59) // 60)} мин."
        )
    if outcome == "access_denied":
        return EVENT_ACCESS_REQUIRED_MESSAGE
    if outcome == "event_changed":
        return "мероприятие изменилось; отправьте фотографию ещё раз"
    return "задание уже неактивно; отправьте фотографию ещё раз"


async def submit_print_job(
    *,
    job_id: str,
    external_user_id: int,
    suffix: str,
    payload: bytes,
    metadata: dict,
    reply_target: ReplyTarget,
) -> str:
    """Persist an authorized job, reserve it, and publish one booth command."""
    event_folder = str(
        metadata.get("event_folder") or yadisk_poll.current_event_folder()
    )
    stored_files = await yadisk_poll.store_print_job(
        job_id,
        int(external_user_id),
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
    outcome = dispatch.get("outcome")
    if outcome == "already_dispatching" or (
        outcome != "dispatching" and dispatch.get("status") == "dispatching"
    ):
        raise RuntimeError("задание уже отправляется или было отправлено")
    if outcome != "dispatching":
        raise RuntimeError(
            "задание не удалось зарезервировать для отправки: "
            f"{outcome or 'неизвестный статус'}"
        )

    try:
        command = await yadisk_control.send_command(
            "print_image",
            reply_target,
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
    returned_command_id = (
        command.get("command_id") if isinstance(command, dict) else None
    )
    if returned_command_id != command_id:
        await database.mark_print_job_failed(
            command_id=command_id,
            last_error="Диск вернул неожиданный command_id",
        )
        raise RuntimeError("Диск вернул неверный ID команды")

    try:
        mode_label = mode_text(str(metadata.get("print_mode") or ""))
    except Exception:
        # The command is already durable on Disk; malformed legacy metadata
        # must not change the successful dispatch result.
        log.exception("Could not prepare print archive copy job=%s", job_id)
    else:
        # The Telegram archive is best effort. Do not make the user or the
        # administrators wait for another messenger after the booth command
        # has already been published successfully.
        _start_background(
            telegram_print_archive.send(
                job_id=job_id,
                payload=payload,
                metadata=metadata,
                source_target=reply_target,
                mode_label=mode_label,
            )
        )
    return command_id


async def handle_upload(upload: PrintUpload, ui: PrintUI) -> bool:
    """Create and process one incoming image without transport-specific logic."""
    user = upload.user
    job_id = uuid.uuid4().hex
    database_user_id: int | None = None
    database_job_created = False
    try:
        if (
            upload.declared_size is not None
            and int(upload.declared_size) > print_media.MAX_PRINT_FILE_SIZE
        ):
            raise ValueError(
                f"файл больше {print_media.MAX_PRINT_FILE_SIZE_MB} МБ"
            )

        event_name, event_token, cafe_mode = event_access.current_event()
        database_user_id = await ensure_user(user)
        if not await user_has_access(
            user,
            event_token=event_token,
            cafe_mode=cafe_mode,
        ):
            await ui.send_text(user, f"❌ {EVENT_ACCESS_REQUIRED_MESSAGE}")
            return True

        created = await database.create_print_job(
            job_id=job_id,
            user_id=database_user_id,
            event_name=event_name,
            conversation_id=user.conversation_id,
            source_message_id=user.source_message_id,
        )
        if created.get("outcome") == "already_open":
            await ui.send_text(
                user,
                "❌ Сначала завершите или отмените предыдущее задание печати.",
            )
            return True
        if created.get("outcome") != "created":
            raise RuntimeError("не удалось создать задание печати")
        database_job_created = True

        await ui.send_text(user, "⏳ Ваше фото обрабатывается, подождите немного…")
        payload = await upload.download()
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("мессенджер прислал пустой файл")
        if len(payload) > print_media.MAX_PRINT_FILE_SIZE:
            raise ValueError(
                f"файл больше {print_media.MAX_PRINT_FILE_SIZE_MB} МБ"
            )
        preview = await asyncio.to_thread(print_jobs.build_choice_preview, payload)
        metadata = _metadata(
            upload,
            job_id=job_id,
            event_name=event_name,
            payload=payload,
            preview=preview,
        )

        if preview.exact_ratio:
            metadata.update({
                "print_mode": "fit",
                "print_choice": "automatic_exact_ratio",
                "print_selected_at": time.time(),
            })
            if cafe_mode and not user.allowlisted:
                await asyncio.to_thread(
                    print_jobs.save_pending,
                    job_id,
                    upload.suffix,
                    payload,
                    metadata,
                )

            current_name, current_token, current_cafe = event_access.current_event()
            claim = await database.claim_print_job_choice(
                job_id=job_id,
                user_id=database_user_id,
                current_event_name=current_name,
                print_mode="fit",
                current_event_token=current_token,
                cafe_mode=current_cafe,
                allowlisted=user.allowlisted,
                automatic=True,
            )
            if claim.get("outcome") == "awaiting_authorization":
                metadata = await asyncio.to_thread(
                    print_jobs.update_pending,
                    job_id,
                    pending_status="awaiting_authorization",
                )
                await _safe_send_text(
                    ui,
                    user,
                    f"✅ Фото подходит под формат {print_media.PRINT_FORMAT_LABEL}. "
                    "Оплатите печать администратору; "
                    "фото ожидает его подтверждения.",
                )
                try:
                    await _request_admin_approval(
                        job_id=job_id,
                        payload=payload,
                        metadata=metadata,
                        mode="fit",
                    )
                except Exception as exc:
                    await _fail_before_dispatch(job_id, exc)
                    await _safe_send_text(
                        ui,
                        user,
                        ADMIN_REQUEST_FAILED_MESSAGE,
                    )
                return True
            if claim.get("outcome") != "authorized":
                await database.cancel_print_job(
                    job_id=job_id,
                    user_id=database_user_id,
                    close_reason=str(claim.get("outcome") or "claim_failed"),
                )
                raise ValueError(_claim_error(claim))

            command_id = await submit_print_job(
                job_id=job_id,
                external_user_id=user.provider_user_id,
                suffix=upload.suffix,
                payload=payload,
                metadata=metadata,
                reply_target=user.target,
            )
            await _safe_send_text(
                ui,
                user,
                "✅ Ваше фото добавлено в очередь и скоро будет распечатано.",
            )
            log.info(
                "%s print job sent without choice job=%s user=%s command=%s",
                user.provider.upper(),
                job_id,
                user.provider_user_id,
                command_id,
            )
            return True

        await asyncio.to_thread(
            print_jobs.save_pending,
            job_id,
            upload.suffix,
            payload,
            metadata,
        )
        try:
            choice_message_id = await ui.send_choice(
                upload,
                preview.payload or b"",
                job_id,
            )
        except Exception as exc:
            log.exception(
                "Could not deliver print preview provider=%s user=%s job=%s",
                user.provider,
                user.provider_user_id,
                job_id,
            )
            raise PreviewDeliveryError(
                "❌ Не удалось отправить превью с вариантами печати. "
                "Пришлите фото ещё раз."
            ) from exc
        if choice_message_id is None:
            raise PreviewDeliveryError(
                "❌ Не удалось отправить превью с вариантами печати. "
                "Пришлите фото ещё раз."
            )
        awaiting = await database.mark_print_job_awaiting_choice(
            job_id=job_id,
            choice_message_id=choice_message_id,
        )
        if awaiting.get("outcome") != "awaiting_choice":
            raise RuntimeError("задание не перешло к выбору режима")
        await asyncio.to_thread(
            print_jobs.update_pending,
            job_id,
            choice_message_id=str(choice_message_id),
        )
        log.info(
            "%s print choice requested job=%s user=%s source=%sx%s",
            user.provider.upper(),
            job_id,
            user.provider_user_id,
            preview.source_size[0],
            preview.source_size[1],
        )
    except Exception as exc:
        log.warning(
            "%s print job rejected user=%s job=%s: %s",
            user.provider.upper(),
            user.provider_user_id,
            job_id,
            exc,
        )
        if database_job_created:
            await _fail_before_dispatch(job_id, exc)
        await ui.send_text(
            user,
            str(exc)
            if isinstance(exc, PreviewDeliveryError)
            else rejected_photo_message(exc),
        )
    return True


async def _safe_ack(
    ui: PrintUI,
    action: PrintAction,
    text: str,
    *,
    alert: bool = False,
) -> None:
    try:
        await ui.acknowledge(action, text, alert=alert)
    except Exception:
        log.exception("Could not acknowledge print action job=%s", action.job_id)


async def _safe_choice_update(
    ui: PrintUI,
    action: PrintAction,
    text: str,
) -> None:
    try:
        await ui.update_choice(action, text)
    except Exception:
        log.exception("Could not update print choice job=%s", action.job_id)


async def handle_choice(action: PrintAction, ui: PrintUI) -> bool:
    if action.action not in {"fit", "fill", "cancel"}:
        return False
    job_id = action.job_id
    user = action.user
    if job_id in _actions_in_progress:
        await _safe_ack(ui, action, "Задание уже обрабатывается")
        return True
    _actions_in_progress.add(job_id)
    try:
        try:
            database_user_id = await ensure_user(user)
        except Exception as exc:
            await _safe_ack(
                ui,
                action,
                f"Печать временно недоступна: {exc}",
                alert=True,
            )
            return True

        if action.action == "cancel":
            try:
                result = await database.cancel_print_job(
                    job_id=job_id,
                    user_id=database_user_id,
                )
            except Exception as exc:
                await _safe_ack(
                    ui,
                    action,
                    f"Не удалось отменить печать: {exc}",
                    alert=True,
                )
                return True
            if result.get("outcome") == "not_owner":
                await _safe_ack(
                    ui,
                    action,
                    "Отменить может только отправитель фото",
                    alert=True,
                )
                return True
            if result.get("outcome") != "cancelled":
                await _safe_ack(
                    ui,
                    action,
                    "Задание уже неактивно",
                    alert=True,
                )
                return True
            await _delete_pending(job_id)
            await _safe_ack(ui, action, "Печать отменена")
            await _safe_choice_update(ui, action, "🚫 Печать отменена.")
            return True

        try:
            event_name, event_token, cafe_mode = event_access.current_event()
            claim = await database.claim_print_job_choice(
                job_id=job_id,
                user_id=database_user_id,
                current_event_name=event_name,
                print_mode=action.action,
                current_event_token=event_token,
                cafe_mode=cafe_mode,
                allowlisted=user.allowlisted,
            )
        except Exception as exc:
            await _safe_ack(
                ui,
                action,
                f"Не удалось сохранить выбор: {exc}",
                alert=True,
            )
            return True

        outcome = claim.get("outcome")
        if outcome == "not_owner":
            await _safe_ack(
                ui,
                action,
                "Выбрать может только отправитель фото",
                alert=True,
            )
            return True
        if outcome == "not_found":
            await _safe_ack(
                ui,
                action,
                "Кнопка уже неактивна. Пришлите фото ещё раз.",
                alert=True,
            )
            return True
        if outcome == "event_changed":
            await database.cancel_print_job(
                job_id=job_id,
                user_id=database_user_id,
                close_reason="event_changed",
            )
            await _delete_pending(job_id)
            await _safe_ack(
                ui,
                action,
                "Мероприятие уже изменилось. Пришлите фото ещё раз.",
                alert=True,
            )
            return True
        if outcome in {"access_denied", "cooldown"}:
            await _safe_ack(ui, action, _claim_error(claim), alert=True)
            return True
        if outcome == "already_claimed":
            status = claim.get("status")
            text = (
                "Фото ожидает оплаты или подтверждения."
                if status == "awaiting_authorization"
                else "Задание уже обрабатывается или передано на печать."
            )
            await _safe_ack(ui, action, text)
            await _safe_choice_update(ui, action, f"ℹ️ {text}")
            return True

        selected_text = mode_text(action.action)
        if outcome == "awaiting_authorization":
            await _safe_ack(ui, action, "Вариант сохранён")
            await _safe_choice_update(ui, action, f"✅ Выбрано: {selected_text}.")
            await _safe_send_text(
                ui,
                user,
                "💳 Оплатите печать администратору.\n"
                "После подтверждения оплаты фото будет добавлено в очередь.",
            )
            try:
                payload, _metadata_value = await asyncio.to_thread(
                    print_jobs.load_pending,
                    job_id,
                )
                metadata = await asyncio.to_thread(
                    print_jobs.update_pending,
                    job_id,
                    print_mode=action.action,
                    print_choice=f"{user.provider}_button",
                    print_selected_at=time.time(),
                    pending_status="awaiting_authorization",
                )
                await _request_admin_approval(
                    job_id=job_id,
                    payload=payload,
                    metadata=metadata,
                    mode=action.action,
                )
            except Exception as exc:
                log.exception("Could not request print approval job=%s", job_id)
                await _fail_before_dispatch(job_id, exc)
                await _safe_send_text(
                    ui,
                    user,
                    ADMIN_REQUEST_FAILED_MESSAGE,
                )
            return True

        if outcome != "authorized":
            await _safe_ack(
                ui,
                action,
                "Не удалось обработать выбранный вариант.",
                alert=True,
            )
            return True

        await _safe_ack(ui, action, "Вариант сохранён")
        await _safe_choice_update(
            ui,
            action,
            f"✅ Выбрано: {selected_text}.",
        )
        try:
            payload, _metadata_value = await asyncio.to_thread(
                print_jobs.load_pending,
                job_id,
            )
            metadata = await asyncio.to_thread(
                print_jobs.update_pending,
                job_id,
                pending_status="submitting",
                print_mode=action.action,
                print_choice=f"{user.provider}_button",
                print_selected_at=time.time(),
            )
            command_id = await submit_print_job(
                job_id=job_id,
                external_user_id=user.provider_user_id,
                suffix=str(metadata["source_suffix"]),
                payload=payload,
                metadata=metadata,
                reply_target=user.target,
            )
        except Exception as exc:
            log.exception("Print choice submission failed job=%s", job_id)
            await _fail_before_dispatch(job_id, exc)
            await ui.send_text(
                user,
                f"❌ Не удалось передать фото на печать: {exc}. "
                "Пришлите фото ещё раз.",
            )
            return True

        await _safe_send_text(
            ui,
            user,
            "✅ Ваше фото добавлено в очередь и скоро будет распечатано.",
        )
        await _delete_pending(job_id)
        log.info(
            "%s print choice submitted job=%s user=%s mode=%s command=%s",
            user.provider.upper(),
            job_id,
            user.provider_user_id,
            action.action,
            command_id,
        )
        return True
    finally:
        _actions_in_progress.discard(job_id)


def _start_background(coroutine: Awaitable[None]) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _notify_target(result: dict, text: str) -> None:
    conversation_id = result.get("conversation_id")
    provider = result.get("provider")
    if not conversation_id or not provider:
        return
    try:
        await messenger_delivery.send_text(
            ReplyTarget(str(provider), conversation_id),
            text,
        )
    except Exception:
        log.exception("Could not notify print user job=%s", result.get("job_id"))


async def _update_admin_card(
    ui: PrintUI,
    action: PrintAction,
    status: str,
) -> None:
    try:
        await ui.update_admin(action, status)
    except Exception:
        log.exception("Could not update origin admin request job=%s", action.job_id)


async def _notify_admins(text: str, *, job_id: str) -> None:
    try:
        delivery = await admin_notifications.send_admin_text(text)
        if delivery.failed_targets:
            log.warning(
                "Admin final status delivered only partially job=%s failed=%s",
                job_id,
                ",".join(target.provider for target in delivery.failed_targets),
            )
    except Exception:
        log.exception("Could not broadcast final admin status job=%s", job_id)


async def _load_pending_metadata(job_id: str) -> dict | None:
    """Best-effort metadata fallback for complete final admin messages."""
    try:
        _payload, metadata = await asyncio.to_thread(print_jobs.load_pending, job_id)
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("Could not load pending print metadata job=%s", job_id)
        return None
    return metadata


def _error_summary(error: Exception | str, *, limit: int = 300) -> str:
    value = " ".join(str(error).split()) or type(error).__name__
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


async def _dispatch_admin_approved(result: dict) -> None:
    job_id = str(result["job_id"])
    metadata: dict | None = None
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
        external_user_id = int(
            result.get("provider_user_id")
            or result.get("user_provider_user_id")
            or metadata["sender_id"]
        )
        command_id = await submit_print_job(
            job_id=job_id,
            external_user_id=external_user_id,
            suffix=str(metadata["source_suffix"]),
            payload=payload,
            metadata=metadata,
            reply_target=ReplyTarget(
                str(result["provider"]),
                result["conversation_id"],
            ),
        )
    except Exception as exc:
        log.exception("Could not submit admin-approved print job=%s", job_id)
        await _fail_before_dispatch(job_id, exc)
        await asyncio.gather(
            _notify_target(
                result,
                "❌ Не удалось передать фото на печать. "
                "Обратитесь к администратору.",
            ),
            _notify_admins(
                admin_job_result_text(
                    result,
                    f"❌ Ошибка передачи на печать: {_error_summary(exc)}",
                    metadata=metadata,
                ),
                job_id=job_id,
            ),
        )
        return

    await asyncio.gather(
        _delete_pending(job_id),
        _notify_target(
            result,
            "✅ Оплата подтверждена. Ваше фото добавлено в очередь "
            "и скоро будет распечатано.",
        ),
        _notify_admins(
            admin_job_result_text(
                result,
                "✅ Фото отправлено на печать.",
                metadata=metadata,
            ),
            job_id=job_id,
        ),
    )
    log.info(
        "Admin approved print job=%s provider=%s user=%s command=%s",
        job_id,
        result.get("provider"),
        result.get("provider_user_id"),
        command_id,
    )


async def handle_admin_action(action: PrintAction, ui: PrintUI) -> bool:
    if action.action not in {"approve", "reject"}:
        return False
    if not action.user.is_admin:
        await _safe_ack(
            ui,
            action,
            "Это действие доступно только администратору.",
            alert=True,
        )
        return True
    job_id = action.job_id
    if job_id in _actions_in_progress:
        await _safe_ack(ui, action, "Задание уже обрабатывается")
        return True
    _actions_in_progress.add(job_id)
    try:
        try:
            current_event_name, _event_token, cafe_mode = event_access.current_event()
        except Exception as exc:
            await _safe_ack(
                ui,
                action,
                f"Печать временно недоступна: {exc}",
                alert=True,
            )
            return True
        if not cafe_mode:
            await _safe_ack(
                ui,
                action,
                f"Режим «{event_access.TECHNICAL_EVENT_NAME}» уже завершён; "
                "задание не отправлено.",
                alert=True,
            )
            return True

        try:
            if action.action == "reject":
                result = await database.reject_print_job_by_admin(
                    job_id=job_id,
                    current_event_name=current_event_name,
                    cafe_mode=cafe_mode,
                )
            else:
                result = await database.authorize_print_job_by_admin(
                    job_id=job_id,
                    current_event_name=current_event_name,
                    cafe_mode=cafe_mode,
                )
        except Exception as exc:
            await _safe_ack(
                ui,
                action,
                f"Не удалось обработать решение: {exc}",
                alert=True,
            )
            return True

        expected = "cancelled" if action.action == "reject" else "authorized"
        if result.get("outcome") != expected:
            await _safe_ack(
                ui,
                action,
                "Задание уже обработано или мероприятие изменилось.",
                alert=True,
            )
            await _update_admin_card(
                ui,
                action,
                "ℹ️ Задание уже обработано другим администратором.",
            )
            return True

        if action.action == "reject":
            await _safe_ack(ui, action, "Печать отклонена")
            metadata = await _load_pending_metadata(job_id)
            await _update_admin_card(
                ui,
                action,
                "🚫 Решение администратора: печать отклонена.",
            )
            await asyncio.gather(
                _delete_pending(job_id),
                _notify_target(
                    result,
                    "❌ Печать фотографии отклонена администратором.",
                ),
                _notify_admins(
                    admin_job_result_text(
                        result,
                        "🚫 Печать отклонена администратором.",
                        metadata=metadata,
                    ),
                    job_id=job_id,
                ),
            )
            return True

        await _safe_ack(ui, action, "Печать разрешена")
        # Start the durable booth dispatch immediately after the database CAS;
        # editing the administrator's messenger card can happen in parallel.
        _start_background(_dispatch_admin_approved(result))
        await _update_admin_card(
            ui,
            action,
            "✅ Решение администратора: печать разрешена.",
        )
        return True
    finally:
        _actions_in_progress.discard(job_id)
