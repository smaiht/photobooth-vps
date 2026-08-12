"""Publish photobooth update artifacts through the Yandex.Disk REST API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp
from aiohttp.payload import Payload

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
# Direct fallback uploads use a .zip destination, one of the media types for
# which generic REST clients can receive upload links capped near 128 KiB/s.
YADISK_API_USER_AGENT = 'Yandex.Disk {"os":"windows"}'
SCHEMA_VERSION = 1
TRANSFER_CHUNK_SIZE = 1024 * 1024
PROGRESS_BYTES = 10 * 1024 * 1024
PROGRESS_SECONDS = 5
TRANSFER_TIMEOUT_SECONDS = 30 * 60
OPERATION_MAX_ATTEMPTS = 300
OPERATION_POLL_SECONDS = 1
IMPORT_MAX_ATTEMPTS = 5
IMPORT_RETRY_BASE_SECONDS = 2

ProgressCallback = Callable[[str], Awaitable[None]]


class _ProgressBytesPayload(Payload):
    """Stream an in-memory artifact while reporting actual socket writes."""

    _autoclose = True

    def __init__(self, value: bytes, path: str):
        super().__init__(value, content_type="application/octet-stream")
        self._value = memoryview(value)
        self._size = len(value)
        self._path = path

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self._value.tobytes().decode(encoding, errors)

    async def write(self, writer) -> None:
        started_at = time.monotonic()
        last_report_at = started_at
        last_report_bytes = 0
        next_report_bytes = PROGRESS_BYTES

        for start in range(0, self._size, TRANSFER_CHUNK_SIZE):
            end = min(start + TRANSFER_CHUNK_SIZE, self._size)
            await writer.write(self._value[start:end])
            now = time.monotonic()
            if (end >= next_report_bytes or now - last_report_at >= PROGRESS_SECONDS
                    or end == self._size):
                interval = max(now - last_report_at, 0.001)
                elapsed = max(now - started_at, 0.001)
                current_speed = (end - last_report_bytes) / interval / 1048576
                average_speed = end / elapsed / 1048576
                log.info(
                    "YaDisk update: upload progress path=%s %.1f/%.1f MiB "
                    "(%.1f%%), speed=%.1f MiB/s, average=%.1f MiB/s",
                    self._path, end / 1048576, self._size / 1048576,
                    end * 100 / self._size, current_speed, average_speed,
                )
                last_report_at = now
                last_report_bytes = end
                next_report_bytes = end + PROGRESS_BYTES


def normalize_folder(folder: str) -> str:
    name = str(folder or "").strip().strip("/")
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("invalid Yandex.Disk updates folder")
    return "/" + name


async def _ensure_directories(session: aiohttp.ClientSession, root: str) -> None:
    log.info("YaDisk update: ensuring directories under %s", root)
    current = ""
    for part in root.strip("/").split("/"):
        current += "/" + part
        async with session.put(f"{API}/resources", params={"path": current}) as response:
            if response.status not in (201, 409):
                raise RuntimeError(
                    f"create update directory {current}: {response.status} {await response.text()}")
            log.info(
                "YaDisk update: directory ready path=%s HTTP %s",
                current, response.status,
            )
    for path in (
        f"{root}/artifacts",
        f"{root}/artifacts/full_bundle",
        f"{root}/status_bundle",
    ):
        async with session.put(
            f"{API}/resources", params={"path": path},
        ) as response:
            if response.status not in (201, 409):
                raise RuntimeError(
                    f"create update directory {path}: "
                    f"{response.status} {await response.text()}")
            log.info(
                "YaDisk update: directory ready path=%s HTTP %s",
                path, response.status,
            )


async def _resource_matches(session: aiohttp.ClientSession, path: str,
                            size: int, md5: str) -> bool:
    for attempt in range(12):
        async with session.get(
            f"{API}/resources",
            params={"path": path, "fields": "size,md5"},
        ) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("size") == size and data.get("md5") == md5:
                    log.info(
                        "YaDisk update: verified path=%s on attempt %d/12 "
                        "(size=%d, md5=%s)",
                        path, attempt + 1, size, md5,
                    )
                    return True
                log.info(
                    "YaDisk update: metadata not ready path=%s attempt=%d/12 "
                    "size=%s/%d md5=%s/%s",
                    path, attempt + 1, data.get("size"), size,
                    data.get("md5"), md5,
                )
            elif response.status in (404, 423):
                log.info(
                    "YaDisk update: resource pending path=%s attempt=%d/12 HTTP %s",
                    path, attempt + 1, response.status,
                )
            elif response.status not in (404, 423):
                raise RuntimeError(
                    f"verify update resource {path}: {response.status} {await response.text()}")
        delay = min(attempt + 1, 5)
        log.info("YaDisk update: verification retry path=%s in %ds", path, delay)
        await asyncio.sleep(delay)
    log.error("YaDisk update: verification timed out path=%s after 12 attempts", path)
    return False


async def _wait_operation(
    session: aiohttp.ClientSession,
    href: str,
    label: str,
) -> None:
    for attempt in range(1, OPERATION_MAX_ATTEMPTS + 1):
        async with session.get(href) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Yandex.Disk operation {label}: HTTP {response.status} "
                    f"{await response.text()}")
            operation = await response.json()
        status = operation.get("status")
        if attempt == 1 or attempt % 5 == 0 or status != "in-progress":
            log.info(
                "YaDisk update: operation=%s attempt=%d/%d status=%s",
                label, attempt, OPERATION_MAX_ATTEMPTS, status,
            )
        if status == "success":
            return
        if status == "failed":
            raise RuntimeError(f"Yandex.Disk operation failed: {label}")
        await asyncio.sleep(OPERATION_POLL_SECONDS)
    raise TimeoutError(f"Yandex.Disk operation timed out: {label}")


async def _delete_staging(session: aiohttp.ClientSession, path: str) -> None:
    try:
        async with session.delete(
            f"{API}/resources",
            params={"path": path, "permanently": "true"},
        ) as response:
            if response.status not in (202, 204, 404):
                log.warning(
                    "YaDisk update: staging cleanup path=%s HTTP %s: %s",
                    path, response.status, await response.text(),
                )
                return
            log.info(
                "YaDisk update: staging cleanup path=%s HTTP %s",
                path, response.status,
            )
    except Exception as exc:
        log.warning("YaDisk update: staging cleanup failed path=%s: %s", path, exc)


async def _move_overwrite(
    session: aiohttp.ClientSession,
    source: str,
    destination: str,
) -> None:
    async with session.post(
        f"{API}/resources/move",
        params={"from": source, "path": destination, "overwrite": "true"},
    ) as response:
        if response.status == 201:
            log.info("YaDisk update: staging move completed synchronously")
            return
        if response.status != 202:
            raise RuntimeError(
                f"move imported update: HTTP {response.status} {await response.text()}")
        href = (await response.json()).get("href")
    if not href:
        raise RuntimeError("move imported update did not return operation URL")
    await _wait_operation(session, href, "move imported artifact")


async def _import_url(
    session: aiohttp.ClientSession,
    source_url: str,
    destination: str,
    payload: bytes,
) -> None:
    """Let Yandex fetch an immutable release URL, verify it, then move atomically."""
    expected_size = len(payload)
    expected_md5 = hashlib.md5(payload).hexdigest()
    bundle_path, filename = destination.rsplit("/", 1)
    staging_parent, bundle_name = bundle_path.rsplit("/", 1)
    staging = (
        f"{staging_parent}/.{bundle_name}.{filename}."
        f"{uuid.uuid4().hex}.incoming.zip"
    )
    log.info(
        "YaDisk update: server-side import requested staging=%s "
        "size=%.1f MiB md5=%s",
        staging, expected_size / 1048576, expected_md5,
    )

    staging_moved = False
    try:
        async with session.post(
            f"{API}/resources/upload",
            params={
                "url": source_url,
                "path": staging,
                "disable_redirects": "false",
            },
        ) as response:
            if response.status != 202:
                raise RuntimeError(
                    f"server-side import: HTTP {response.status} {await response.text()}")
            href = (await response.json()).get("href")
        if not href:
            raise RuntimeError("server-side import did not return operation URL")

        await _wait_operation(session, href, "import release URL")
        if not await _resource_matches(
            session, staging, expected_size, expected_md5,
        ):
            raise RuntimeError("server-side imported artifact did not verify")
        log.info(
            "YaDisk update: imported artifact verified; moving %s -> %s",
            staging, destination,
        )
        await _move_overwrite(session, staging, destination)
        staging_moved = True
        if not await _resource_matches(
            session, destination, expected_size, expected_md5,
        ):
            raise RuntimeError("moved imported artifact did not verify")
        log.info("YaDisk update: server-side import complete path=%s", destination)
    finally:
        if not staging_moved:
            await _delete_staging(session, staging)


async def _notify_progress(
    progress_callback: ProgressCallback | None,
    message: str,
) -> None:
    if progress_callback is None:
        return
    try:
        await progress_callback(message)
    except Exception:
        # A Telegram outage must not turn a recoverable Disk operation into a
        # failed update. The same information remains available in VPS logs.
        log.exception("YaDisk update: progress notification failed")


async def _import_url_with_retries(
    session: aiohttp.ClientSession,
    source_url: str,
    destination: str,
    payload: bytes,
    progress_callback: ProgressCallback | None = None,
) -> None:
    for attempt in range(1, IMPORT_MAX_ATTEMPTS + 1):
        log.info(
            "YaDisk update: server-side import attempt %d/%d",
            attempt, IMPORT_MAX_ATTEMPTS,
        )
        try:
            await _import_url(session, source_url, destination, payload)
        except Exception as exc:
            if attempt == IMPORT_MAX_ATTEMPTS:
                log.warning(
                    "YaDisk update: server-side import attempt %d/%d failed; "
                    "fast attempts exhausted: %s",
                    attempt, IMPORT_MAX_ATTEMPTS, exc,
                )
                await _notify_progress(
                    progress_callback,
                    "⚠️ Быстрый импорт на Яндекс.Диск: попытка "
                    f"{attempt}/{IMPORT_MAX_ATTEMPTS} не удалась: {exc}. "
                    "Быстрые попытки исчерпаны.",
                )
                raise

            delay = IMPORT_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "YaDisk update: server-side import attempt %d/%d failed: %s; "
                "retrying in %ds",
                attempt, IMPORT_MAX_ATTEMPTS, exc, delay,
            )
            await _notify_progress(
                progress_callback,
                "⚠️ Быстрый импорт на Яндекс.Диск: попытка "
                f"{attempt}/{IMPORT_MAX_ATTEMPTS} не удалась: {exc}. "
                f"Повтор через {delay} с.",
            )
            await asyncio.sleep(delay)
            continue

        if attempt > 1:
            await _notify_progress(
                progress_callback,
                "✅ Быстрый импорт на Яндекс.Диск выполнен с попытки "
                f"{attempt}/{IMPORT_MAX_ATTEMPTS}.",
            )
        return


async def _upload_bytes(api_session: aiohttp.ClientSession,
                        transfer_session: aiohttp.ClientSession,
                        path: str, payload: bytes) -> None:
    size = len(payload)
    md5 = hashlib.md5(payload).hexdigest()
    log.info(
        "YaDisk update: requesting upload URL path=%s size=%.1f MiB md5=%s",
        path, size / 1048576, md5,
    )
    async with api_session.get(
        f"{API}/resources/upload",
        params={"path": path, "overwrite": "true"},
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"get update upload URL {path}: {response.status} {await response.text()}")
        href = (await response.json())["href"]
    log.info("YaDisk update: upload URL received path=%s", path)

    upload_started = time.monotonic()
    log.info("YaDisk update: upload started path=%s size=%.1f MiB", path, size / 1048576)
    body = _ProgressBytesPayload(payload, path)
    async with transfer_session.put(href, data=body) as response:
        if response.status not in (201, 202):
            raise RuntimeError(
                f"upload update resource {path}: {response.status} {await response.text()}")
        upload_status = response.status
    elapsed = max(time.monotonic() - upload_started, 0.001)
    log.info(
        "YaDisk update: upload accepted path=%s HTTP %s in %.1fs "
        "(average %.1f MiB/s); waiting for metadata",
        path, upload_status, elapsed, size / elapsed / 1048576,
    )

    if not await _resource_matches(api_session, path, size, md5):
        raise RuntimeError(f"uploaded update resource did not verify: {path}")


async def publish_update(
    payload: bytes,
    folder: str,
    source_url: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Overwrite the full artifact, then publish its status pointer last."""
    token = os.environ.get("YADISK_TOKEN", "").strip()
    if not token:
        raise RuntimeError("YADISK_TOKEN is not set")

    root = normalize_folder(folder)
    sha256 = hashlib.sha256(payload).hexdigest()
    artifact_bundle_path = f"{root}/artifacts/full_bundle"
    artifact_path = f"{artifact_bundle_path}/full.zip"
    status_path = f"{root}/status_bundle/status.json"
    artifact = {
        "path": artifact_path,
        "bundle_path": artifact_bundle_path,
        "size": len(payload),
        "sha256": sha256,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    started_at = time.monotonic()
    log.info(
        "YaDisk update: publish started root=%s size=%.1f MiB sha256=%s",
        root, len(payload) / 1048576, sha256[:16],
    )
    api_timeout = aiohttp.ClientTimeout(total=90, connect=20)
    transfer_timeout = aiohttp.ClientTimeout(
        total=TRANSFER_TIMEOUT_SECONDS, connect=30)
    async with aiohttp.ClientSession(
        headers={
            "Authorization": f"OAuth {token}",
            "User-Agent": YADISK_API_USER_AGENT,
        },
        timeout=api_timeout,
    ) as api_session, aiohttp.ClientSession(timeout=transfer_timeout) as transfer_session:
        await _ensure_directories(api_session, root)
        status = {
            "schema_version": SCHEMA_VERSION,
            "active": "full",
            "artifacts": {"full": artifact},
        }
        status_payload = json.dumps(
            status, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if source_url:
            log.info(
                "YaDisk update: stage 1/2 importing artifact from resolved release URL "
                "path=%s",
                artifact_path,
            )
            try:
                await _import_url_with_retries(
                    api_session,
                    source_url,
                    artifact_path,
                    payload,
                    progress_callback,
                )
            except Exception:
                log.exception(
                    "YaDisk update: all server-side import attempts failed; "
                    "falling back to direct PUT")
                await _notify_progress(
                    progress_callback,
                    "🐢 Быстрый импорт не сработал после "
                    f"{IMPORT_MAX_ATTEMPTS} попыток. Перехожу на медленную "
                    "прямую загрузку; она может занять до 30 минут.",
                )
                await _upload_bytes(
                    api_session, transfer_session, artifact_path, payload)
        else:
            log.info(
                "YaDisk update: stage 1/2 uploading artifact directly path=%s",
                artifact_path,
            )
            await _upload_bytes(
                api_session, transfer_session, artifact_path, payload)
        log.info(
            "YaDisk update: stage 1/2 artifact verified; "
            "stage 2/2 publishing status pointer path=%s",
            status_path,
        )
        await _upload_bytes(api_session, transfer_session, status_path, status_payload)

    log.info(
        "YaDisk update: publish complete sha256=%s size=%d bytes in %.1fs",
        sha256[:16], len(payload), time.monotonic() - started_at,
    )
    return status
