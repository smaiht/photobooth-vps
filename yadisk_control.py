"""VPS side of the Yandex.Disk command and response channel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
SCHEMA_VERSION = 1
POLL_INTERVAL = 2
PAGE_SIZE = 100
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
    command_id = data.get("command_id")
    if not isinstance(command_id, str) or not COMMAND_ID_RE.fullmatch(command_id):
        raise ValueError("invalid command_id")
    if filename and filename != f"{command_id}.json":
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
    reply_chat_id = data.get("reply_chat_id")
    if reply_chat_id is not None and not isinstance(reply_chat_id, (int, str)):
        raise ValueError("invalid reply_chat_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "command_id": command_id,
        "command": command,
        "status": status,
        "message": str(data.get("message", ""))[:4000],
        "artifact_path": artifact_path,
        "event_folder": event_folder,
        "reply_chat_id": reply_chat_id,
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
        for suffix in ("commands", "commands/inbox", "commands/done", "responses", "logs"):
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


async def send_command(command: str, data: dict | str | None = None,
                       reply_chat_id: int | str | None = None) -> dict:
    if not isinstance(command, str) or not command or len(command) > 50:
        raise ValueError("invalid command")
    if data is not None and not isinstance(data, (dict, str)):
        raise ValueError("invalid command data")
    if not await _connect():
        raise RuntimeError("Yandex.Disk control is unavailable")
    command_id = uuid.uuid4().hex
    body = {
        "schema_version": SCHEMA_VERSION,
        "command_id": command_id,
        "command": command,
        "data": data,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reply_chat_id": reply_chat_id,
    }
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await _upload_bytes(payload, f"{_root}/commands/inbox/{command_id}.json")
    log.info(f"Control: sent {command} ({command_id})")
    return body


async def _list_responses() -> list[dict]:
    result = []
    offset = 0
    path = f"{_root}/responses"
    while True:
        params = {
            "path": path,
            "limit": PAGE_SIZE,
            "offset": offset,
            "sort": "name",
            "fields": "_embedded.total,_embedded.items.name,_embedded.items.path,_embedded.items.type",
        }
        async with _session.get(f"{API}/resources", params=params) as response:
            if response.status != 200:
                raise RuntimeError(f"list responses: {response.status} {await response.text()}")
            embedded = (await response.json()).get("_embedded", {})
        items = embedded.get("items", [])
        result.extend(item for item in items
                      if item.get("type") == "file" and item.get("name", "").endswith(".json"))
        offset += len(items)
        if not items or offset >= int(embedded.get("total", offset)):
            return result


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


async def _process_response(
    item: dict,
    handler: Callable[[dict], Awaitable[bool]],
) -> bool:
    filename = item["name"]
    remote_path = str(item.get("path", "")).removeprefix("disk:")
    try:
        response = validate_response(
            json.loads((await download_bytes(remote_path)).decode("utf-8")), filename)
    except Exception as exc:
        log.warning(f"Control: invalid response {filename}: {exc}")
        return False
    if not await handler(response):
        return False
    return await delete_resource(remote_path)


async def control_init(folder: str) -> bool:
    global _root, _token, _configured
    _token = os.environ.get("YADISK_TOKEN", "").strip()
    if not _token:
        log.warning("Control: YADISK_TOKEN not set")
        return False
    _root = normalize_folder(folder)
    _configured = True
    return await _connect()


async def response_poll_loop(handler: Callable[[dict], Awaitable[bool]]) -> None:
    while True:
        try:
            if await _connect():
                for item in await _list_responses():
                    await _process_response(item, handler)
        except Exception as exc:
            log.warning(f"Control: response poll failed: {exc}")
            await _close_sessions()
        await asyncio.sleep(POLL_INTERVAL)


async def control_close() -> None:
    await _close_sessions()
