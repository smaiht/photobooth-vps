"""Publish photobooth update artifacts through the Yandex.Disk REST API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
SCHEMA_VERSION = 1


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
    artifacts = f"{root}/artifacts"
    async with session.put(f"{API}/resources", params={"path": artifacts}) as response:
        if response.status not in (201, 409):
            raise RuntimeError(
                f"create update directory {artifacts}: {response.status} {await response.text()}")
        log.info(
            "YaDisk update: directory ready path=%s HTTP %s",
            artifacts, response.status,
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
    async with transfer_session.put(href, data=payload) as response:
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


async def publish_update(payload: bytes, folder: str) -> dict:
    """Overwrite the full artifact, then publish its status pointer last."""
    token = os.environ.get("YADISK_TOKEN", "").strip()
    if not token:
        raise RuntimeError("YADISK_TOKEN is not set")

    root = normalize_folder(folder)
    sha256 = hashlib.sha256(payload).hexdigest()
    artifact_path = f"{root}/artifacts/full.zip"
    status_path = f"{root}/status.json"
    artifact = {
        "path": artifact_path,
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
    transfer_timeout = aiohttp.ClientTimeout(total=600, connect=30)
    async with aiohttp.ClientSession(
        headers={"Authorization": f"OAuth {token}"}, timeout=api_timeout,
    ) as api_session, aiohttp.ClientSession(timeout=transfer_timeout) as transfer_session:
        await _ensure_directories(api_session, root)
        status = {
            "schema_version": SCHEMA_VERSION,
            "active": "full",
            "artifacts": {"full": artifact},
        }
        status_payload = json.dumps(
            status, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        log.info(
            "YaDisk update: stage 1/2 uploading artifact path=%s",
            artifact_path,
        )
        await _upload_bytes(api_session, transfer_session, artifact_path, payload)
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
