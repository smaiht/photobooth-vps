"""Provider-neutral execution of administrator bot commands."""

from __future__ import annotations

import logging

import admin_commands
import messenger_delivery
import runtime_config
import vps_update
import yadisk_control
from messaging import ReplyTarget


log = logging.getLogger(__name__)


async def _reply(target: ReplyTarget, text: str) -> bool:
    try:
        return await messenger_delivery.send_text(target, text)
    except Exception as exc:
        # A command may already be durable on Disk. Do not let an optional
        # acknowledgement failure make a long-poll retry submit it again.
        log.warning(
            "Admin reply failed provider=%s conversation=%s: %s",
            target.provider,
            target.conversation_id,
            exc,
        )
        return False


async def _run_update(
    target: ReplyTarget,
    updates_folder: str,
) -> None:
    await _reply(target, "⏳ Скачиваю полный релиз...")

    async def report_progress(message: str) -> None:
        await _reply(target, message)

    try:
        result = await vps_update.publish_latest_release(
            updates_folder,
            report_progress,
        )
    except Exception as exc:
        log.exception("VPS update failed")
        result = f"❌ Ошибка: {exc}"
    await _reply(target, result)


async def handle_message(
    target: ReplyTarget,
    text: str,
    *,
    updates_folder: str | None = None,
) -> None:
    """Parse and execute one message from an authenticated administrator."""
    target = ReplyTarget.from_value(target)
    try:
        parsed = admin_commands.parse(text)
    except (ValueError, RuntimeError) as exc:
        await _reply(target, f"❌ {exc}")
        return

    if parsed is None:
        await _reply(target, admin_commands.HELP_MESSAGE)
        return

    command, data = parsed
    if command == "update":
        await _run_update(
            target,
            updates_folder
            if updates_folder is not None
            else runtime_config.updates_folder(),
        )
        return

    try:
        await yadisk_control.send_command(command, target, data)
    except Exception as exc:
        await _reply(target, admin_commands.failed_message(command, exc))
        return

    await _reply(target, admin_commands.sent_message(command, data))
