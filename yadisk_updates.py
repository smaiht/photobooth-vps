"""Publish photobooth update artifacts through the Yandex.Disk REST API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
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
    current = ""
    for part in root.strip("/").split("/"):
        current += "/" + part
        async with session.put(f"{API}/resources", params={"path": current}) as response:
            if response.status not in (201, 409):
                raise RuntimeError(
                    f"create update directory {current}: {response.status} {await response.text()}")
    artifacts = f"{root}/artifacts"
    async with session.put(f"{API}/resources", params={"path": artifacts}) as response:
        if response.status not in (201, 409):
            raise RuntimeError(
                f"create update directory {artifacts}: {response.status} {await response.text()}")


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
                    return True
            elif response.status not in (404, 423):
                raise RuntimeError(
                    f"verify update resource {path}: {response.status} {await response.text()}")
        await asyncio.sleep(min(attempt + 1, 5))
    return False


async def _upload_bytes(api_session: aiohttp.ClientSession,
                        transfer_session: aiohttp.ClientSession,
                        path: str, payload: bytes) -> None:
    async with api_session.get(
        f"{API}/resources/upload",
        params={"path": path, "overwrite": "true"},
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"get update upload URL {path}: {response.status} {await response.text()}")
        href = (await response.json())["href"]

    async with transfer_session.put(href, data=payload) as response:
        if response.status not in (201, 202):
            raise RuntimeError(
                f"upload update resource {path}: {response.status} {await response.text()}")

    md5 = hashlib.md5(payload).hexdigest()
    if not await _resource_matches(api_session, path, len(payload), md5):
        raise RuntimeError(f"uploaded update resource did not verify: {path}")


async def _read_status(api_session: aiohttp.ClientSession,
                       transfer_session: aiohttp.ClientSession,
                       status_path: str) -> dict:
    async with api_session.get(
        f"{API}/resources/download", params={"path": status_path},
    ) as response:
        if response.status == 404:
            return {}
        if response.status != 200:
            raise RuntimeError(
                f"read update status URL: {response.status} {await response.text()}")
        href = (await response.json())["href"]
    async with transfer_session.get(href) as response:
        if response.status != 200:
            raise RuntimeError(
                f"read update status: {response.status} {await response.text()}")
        data = await response.json()
    return data if isinstance(data, dict) else {}


async def publish_update(payload: bytes, kind: str, folder: str) -> dict:
    """Overwrite one fixed artifact, then publish the dual-artifact status last."""
    if kind not in ("full", "small"):
        raise ValueError(f"unsupported update kind: {kind}")
    token = os.environ.get("YADISK_TOKEN", "").strip()
    if not token:
        raise RuntimeError("YADISK_TOKEN is not set")

    root = normalize_folder(folder)
    sha256 = hashlib.sha256(payload).hexdigest()
    artifact_path = f"{root}/artifacts/{kind}.zip"
    status_path = f"{root}/status.json"
    artifact = {
        "path": artifact_path,
        "size": len(payload),
        "sha256": sha256,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    api_timeout = aiohttp.ClientTimeout(total=90, connect=20)
    transfer_timeout = aiohttp.ClientTimeout(total=600, connect=30)
    async with aiohttp.ClientSession(
        headers={"Authorization": f"OAuth {token}"}, timeout=api_timeout,
    ) as api_session, aiohttp.ClientSession(timeout=transfer_timeout) as transfer_session:
        await _ensure_directories(api_session, root)
        previous = await _read_status(api_session, transfer_session, status_path)
        previous_artifacts = previous.get("artifacts", {})
        artifacts = {
            "full": previous_artifacts.get("full") if isinstance(previous_artifacts, dict) else None,
            "small": previous_artifacts.get("small") if isinstance(previous_artifacts, dict) else None,
        }
        artifacts[kind] = artifact
        status = {
            "schema_version": SCHEMA_VERSION,
            "active": kind,
            "artifacts": artifacts,
        }
        status_payload = json.dumps(
            status, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await _upload_bytes(api_session, transfer_session, artifact_path, payload)
        await _upload_bytes(api_session, transfer_session, status_path, status_payload)

    log.info(f"YaDisk update: published {kind} {sha256[:16]}, {len(payload)} bytes")
    return status
