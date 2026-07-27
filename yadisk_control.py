"""VPS helpers for commands and control artifacts on Yandex.Disk."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import aiohttp

from messaging import ReplyTarget

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
SCHEMA_VERSION = 3
COMMAND_ID_RE = re.compile(r"^[a-f0-9]{32}$")

_session: aiohttp.ClientSession | None = None
_transfer_session: aiohttp.ClientSession | None = None
_root = ""
_token = ""
_configured = False


def normalize_folder(folder: str) -> str:
    name = str(folder or "").strip().strip("/")
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("invalid Yandex.Disk control folder")
    return "/" + name


def validate_response(data: dict, filename: str = "") -> dict:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported response schema")
    if data.get("message_type") != "command_response":
        raise ValueError("invalid response message_type")
    command_id = data.get("command_id")
    if not isinstance(command_id, str) or not COMMAND_ID_RE.fullmatch(command_id):
        raise ValueError("invalid command_id")
    if filename and filename != f"response_{command_id}.json":
        raise ValueError("response filename does not match command_id")
    command = data.get("command")
    if not isinstance(command, str) or not command or len(command) > 50:
        raise ValueError("invalid command")
    status = data.get("status")
    if status not in ("ok", "error"):
        raise ValueError("invalid response status")
    artifact_path = data.get("artifact_path")
    if artifact_path is not None and (
            not isinstance(artifact_path, str) or not artifact_path.startswith("/")
            or ".." in artifact_path.split("/")):
        raise ValueError("invalid artifact path")
    event_folder = data.get("event_folder")
    if event_folder is not None and not isinstance(event_folder, str):
        raise ValueError("invalid event folder")
    try:
        reply_target = ReplyTarget.from_value(data.get("reply_target"))
    except ValueError as exc:
        raise ValueError("invalid reply_target") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "message_type": "command_response",
        "command_id": command_id,
        "command": command,
        "status": status,
        "message": str(data.get("message", ""))[:4000],
        "artifact_path": artifact_path,
        "event_folder": event_folder,
        "reply_target": reply_target,
        "created_at": str(data.get("created_at", "")),
    }


async def _close_sessions() -> None:
    global _session, _transfer_session
    if _session and not _session.closed:
        await _session.close()
    if _transfer_session and not _transfer_session.closed:
        await _transfer_session.close()
    _session = None
    _transfer_session = None


async def _ensure_directory(path: str) -> None:
    async with _session.put(f"{API}/resources", params={"path": path}) as response:
        if response.status not in (201, 409):
            raise RuntimeError(
                f"create control directory {path}: {response.status} {await response.text()}")


async def _connect() -> bool:
    global _session, _transfer_session
    if not _configured:
        return False
    if _session and not _session.closed:
        return True
    await _close_sessions()
    _session = aiohttp.ClientSession(
        headers={"Authorization": f"OAuth {_token}"},
        timeout=aiohttp.ClientTimeout(total=60, connect=15),
    )
    _transfer_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120, connect=20))
    try:
        current = ""
        for part in _root.strip("/").split("/"):
            current += "/" + part
            await _ensure_directory(current)
        for suffix in ("to_booth", "to_vps", "logs", "configs"):
            await _ensure_directory(f"{_root}/{suffix}")
        return True
    except Exception as exc:
        log.warning(f"Control: connection failed: {exc}")
        await _close_sessions()
        return False


async def _resource_matches(path: str, payload: bytes) -> bool:
    expected_md5 = hashlib.md5(payload).hexdigest()
    for attempt in range(10):
        async with _session.get(
            f"{API}/resources", params={"path": path, "fields": "size,md5"},
        ) as response:
            if response.status == 200:
                metadata = await response.json()
                if metadata.get("size") == len(payload) and metadata.get("md5") == expected_md5:
                    return True
            elif response.status not in (404, 423):
                return False
        await asyncio.sleep(min(attempt + 1, 3))
    return False


async def _upload_bytes(payload: bytes, remote_path: str) -> None:
    async with _session.get(
        f"{API}/resources/upload",
        params={"path": remote_path, "overwrite": "true"},
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"get upload URL: {response.status} {await response.text()}")
        href = (await response.json())["href"]
    async with _transfer_session.put(href, data=payload) as response:
        if response.status not in (201, 202):
            raise RuntimeError(f"upload control file: {response.status} {await response.text()}")
    if not await _resource_matches(remote_path, payload):
        raise RuntimeError(f"uploaded control file did not verify: {remote_path}")


async def send_command(
    command: str,
    reply_target: ReplyTarget,
    data: dict | str | None = None,
    *,
    command_id: str | None = None,
) -> dict:
    if not isinstance(command, str) or not command or len(command) > 50:
        raise ValueError("invalid command")
    if data is not None and not isinstance(data, (dict, str)):
        raise ValueError("invalid command data")
    reply_target = ReplyTarget.from_value(reply_target)
    if command_id is None:
        command_id = uuid.uuid4().hex
    elif not isinstance(command_id, str) or not COMMAND_ID_RE.fullmatch(command_id):
        raise ValueError("invalid command_id")
    if not await _connect():
        raise RuntimeError("Yandex.Disk control is unavailable")
    body = {
        "schema_version": SCHEMA_VERSION,
        "message_type": "command",
        "command_id": command_id,
        "command": command,
        "data": data,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reply_target": reply_target.to_dict(),
    }
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await _upload_bytes(payload, f"{_root}/to_booth/{command_id}.json")
    log.info(f"Control: sent {command} ({command_id})")
    return body


async def download_bytes(remote_path: str, max_size: int = 10 * 1024 * 1024) -> bytes:
    if (not isinstance(remote_path, str) or not remote_path.startswith("/")
            or ".." in remote_path.split("/")):
        raise ValueError("invalid download path")
    if not await _connect():
        raise RuntimeError("Yandex.Disk control is unavailable")
    async with _session.get(
        f"{API}/resources/download", params={"path": remote_path},
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"get download URL: {response.status} {await response.text()}")
        href = (await response.json())["href"]
    async with _transfer_session.get(href) as response:
        if response.status != 200:
            raise RuntimeError(f"download control file: {response.status} {await response.text()}")
        payload = await response.read()
    if len(payload) > max_size:
        raise ValueError("control artifact is too large")
    return payload


async def delete_resource(remote_path: str) -> bool:
    if (not isinstance(remote_path, str) or not remote_path.startswith("/")
            or ".." in remote_path.split("/")):
        raise ValueError("invalid delete path")
    if not await _connect():
        return False
    async with _session.delete(
        f"{API}/resources",
        params={"path": remote_path, "permanently": "true"},
    ) as response:
        if response.status in (202, 204, 404):
            return True
        log.warning(f"Control: delete {remote_path}: {response.status} {await response.text()}")
        return False


async def control_init(folder: str) -> bool:
    global _root, _token, _configured
    _token = os.environ.get("YADISK_TOKEN", "").strip()
    if not _token:
        log.warning("Control: YADISK_TOKEN not set")
        return False
    _root = normalize_folder(folder)
    _configured = True
    return await _connect()
async def control_close() -> None:
    await _close_sessions()
