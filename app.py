"""Photobooth VPS process composition root."""

import asyncio
import logging

import ai_flow
import control_response_service
import database
import migrate
import runtime_config
import telegram_bot
import vk_bot
import yadisk_control
import yadisk_poll


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def main() -> None:
    """Initialize shared infrastructure, then run all independent workers."""
    log.info("Database: applying pending migrations")
    await asyncio.to_thread(migrate.apply_migrations)
    log.info("Database: schema is ready")
    recovered_jobs = await database.recover_interrupted_print_jobs()
    if recovered_jobs:
        log.warning(
            "Database: closed %d interrupted local print jobs after restart",
            recovered_jobs,
        )

    ai_settings = runtime_config.ai_image_edit_settings()
    if ai_settings["enabled"]:
        recovered_ai = await ai_flow.recover_interrupted_jobs()
        if recovered_ai["failed_job_ids"]:
            log.warning(
                "AI: closed %d interrupted jobs after restart",
                len(recovered_ai["failed_job_ids"]),
            )
        if recovered_ai["restored_prints"]:
            log.warning(
                "AI: restored %d interrupted print buttons",
                recovered_ai["restored_prints"],
            )
        log.info(
            "AI image edit: generator=%s templates=%d",
            ai_settings["generator"],
            len(ai_settings["templates"]),
        )
    else:
        log.info("AI image edit: disabled")

    yadisk_folder = runtime_config.yadisk_folder()
    control_folder = runtime_config.control_folder()
    archive_providers = runtime_config.archive_delivery_providers()
    log.info(
        "Automatic media archive delivery: %s",
        ", ".join(archive_providers) if archive_providers else "disabled",
    )
    inbox_ready = await yadisk_poll.yadisk_init(
        yadisk_folder,
        control_folder,
    )
    await yadisk_control.control_init(control_folder)
    try:
        async with asyncio.TaskGroup() as workers:
            if not inbox_ready:
                log.error("Yandex.Disk inbox poller is not configured")
            else:
                workers.create_task(
                    yadisk_poll.yadisk_poll_loop(
                        control_response_service.handle,
                        control_response_service.handle_notice,
                    ),
                    name="yadisk-inbox-poll",
                )
            workers.create_task(
                telegram_bot.poll_updates(),
                name="telegram-long-poll",
            )
            workers.create_task(
                vk_bot.poll_messages(),
                name="vk-long-poll",
            )
            if ai_settings["enabled"]:
                workers.create_task(
                    ai_flow.worker_loop(),
                    name="ai-image-worker",
                )
    finally:
        results = await asyncio.gather(
            yadisk_poll.yadisk_close(),
            yadisk_control.control_close(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                log.warning("Worker cleanup failed: %s", result)


if __name__ == "__main__":
    asyncio.run(main())
