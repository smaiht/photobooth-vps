"""Photobooth VPS process composition root."""

import asyncio
import logging

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

    yadisk_folder = runtime_config.yadisk_folder()
    control_folder = runtime_config.control_folder()
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
                    yadisk_poll.yadisk_poll_loop(control_response_service.handle),
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
