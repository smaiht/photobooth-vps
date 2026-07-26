"""Download, validate and publish the latest full VPS release."""

import io
import logging
import os
import time
import zipfile
from typing import Awaitable, Callable

import aiohttp

import yadisk_updates

log = logging.getLogger(__name__)

GITHUB_RELEASE_URL = os.environ.get("GITHUB_RELEASE_URL", "")
ProgressCallback = Callable[[str], Awaitable[None]]


async def publish_latest_release(
    updates_folder: str,
    progress_callback: ProgressCallback | None = None,
) -> str:
    if not GITHUB_RELEASE_URL:
        raise RuntimeError("GITHUB_RELEASE_URL не задан")

    started_at = time.monotonic()
    log.info("Update: requested, downloading full release from GITHUB_RELEASE_URL")
    async with aiohttp.ClientSession() as download_session:
        async with download_session.get(
            GITHUB_RELEASE_URL,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as response:
            content_length = response.content_length
            expected_text = (
                f"{content_length / 1048576:.1f} MiB"
                if content_length is not None
                else "unknown size"
            )
            log.info(
                "Update: GitHub responded HTTP %s, content-length=%s",
                response.status,
                expected_text,
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
                    current_speed = (
                        (total - last_report_bytes) / interval / 1048576
                    )
                    average_speed = total / elapsed / 1048576
                    progress = (
                        f", {total * 100 / content_length:.1f}%"
                        if content_length
                        else ""
                    )
                    log.info(
                        "Update: GitHub download %.1f MiB%s, "
                        "speed=%.1f MiB/s, average=%.1f MiB/s",
                        total / 1048576,
                        progress,
                        current_speed,
                        average_speed,
                    )
                    last_report_at = now
                    last_report_bytes = total
                    next_report_bytes = total + 10 * 1024 * 1024

            zip_data = downloaded.getvalue()
            download_elapsed = max(time.monotonic() - download_started, 0.001)
            if content_length is not None and len(zip_data) != content_length:
                raise RuntimeError(
                    "GitHub download size mismatch: "
                    f"{len(zip_data)}/{content_length}"
                )
            log.info(
                "Update: GitHub download complete, %.1f MiB in %.1fs "
                "(average %.1f MiB/s)",
                len(zip_data) / 1048576,
                download_elapsed,
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
        len(downloaded_names),
        time.monotonic() - validation_started,
    )

    with zipfile.ZipFile(io.BytesIO(zip_data)) as release_zip:
        names = [name.replace("\\", "/") for name in release_zip.namelist()]
        if "app.py" not in names:
            raise RuntimeError("ZIP не содержит app.py в корне")
    log.info(
        "Update: release ZIP structure accepted, entries=%d, size=%.1f MiB",
        len(names),
        len(zip_data) / 1048576,
    )

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
        time.monotonic() - started_at,
        artifact["sha256"][:16],
        len(zip_data) / 1048576,
    )
    return (
        "✅ Полное обновление загружено на Диск\n"
        f"ZIP: {len(zip_data) / 1048576:.1f} MB\n"
        f"SHA: {artifact['sha256'][:16]}\n"
        "Для установки отправь /restart"
    )
