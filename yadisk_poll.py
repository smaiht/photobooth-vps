"""Consume the booth-to-VPS Yandex.Disk inbox.

The booth publishes both completed-session manifests and command responses to
the stable ``control/to_vps`` channel.  One poller dispatches those message
types to independent asyncio workers, so a large Telegram upload cannot delay
a command response.  Session media stay flat in their event folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

import yadisk_control

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
POLL_INTERVAL = 10
PAGE_SIZE = 1000
STATE_FILE = Path("vps_yadisk_state.json")
SCHEMA_VERSION = 2
MD5_RE = re.compile(r"^[a-f0-9]{32}$")

_state: dict = {"handled_messages": []}
_session: aiohttp.ClientSession | None = None
_transfer_session: aiohttp.ClientSession | None = None
_folder = ""
_bus_root = ""
_token = ""
_tg_token = ""
_tg_chat = ""
_configured = False
_inflight: set[str] = set()

ResponseHandler = Callable[[dict], Awaitable[bool]]


def _state_save() -> None:
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:
        log.error(f"YaDisk: state save failed: {exc}")
        raise


def _state_load() -> None:
    global _state
    if not STATE_FILE.exists():
        _state = {"handled_messages": []}
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        handled = data.get("handled_messages", []) if isinstance(data, dict) else []
        _state = {"handled_messages": [str(name) for name in handled]}
    except Exception as exc:
        log.warning(f"YaDisk: state load failed: {exc}")
        _state = {"handled_messages": []}


def validate_manifest(data: dict) -> dict:
    """Validate untrusted manifest data and return a normalized copy."""
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema")
    if data.get("message_type") != "session_ready":
        raise ValueError("invalid manifest message_type")

    event_name = validate_event_name(data.get("event_folder"))

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.isalnum() or len(session_id) > 80:
        raise ValueError("invalid session_id")

    files = data.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 50:
        raise ValueError("manifest must contain 1-50 files")

    normalized = []
    names = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("invalid file entry")
        name = entry.get("name")
        if (not isinstance(name, str) or not name or name in (".", "..")
                or "/" in name or "\\" in name or name in names):
            raise ValueError("invalid or duplicate file name")
        kind = entry.get("kind")
        if kind not in ("photo", "print", "video"):
            raise ValueError(f"unsupported media kind: {kind}")
        size = entry.get("size")
        if not isinstance(size, int) or size < 0:
            raise ValueError("invalid file size")
        md5 = entry.get("md5")
        if md5 is not None and (not isinstance(md5, str) or not MD5_RE.fullmatch(md5)):
            raise ValueError("invalid file md5")
        names.add(name)
        normalized.append({"name": name, "kind": kind, "size": size, "md5": md5})

    return {
        "schema_version": SCHEMA_VERSION,
        "message_type": "session_ready",
        "event_folder": "/" + event_name,
        "session_id": session_id,
        "created_at": str(data.get("created_at", "")),
        "files": normalized,
    }


async def _close_sessions() -> None:
    global _session, _transfer_session
    if _session and not _session.closed:
        await _session.close()
    if _transfer_session and not _transfer_session.closed:
        await _transfer_session.close()
    _session = None
    _transfer_session = None


async def _ensure_directory(path: str) -> bool:
    try:
        async with _session.put(f"{API}/resources", params={"path": path}) as response:
            if response.status in (201, 409):
                return True
            log.warning(f"YaDisk: create directory {path}: {response.status} "
                        f"{await response.text()}")
            return False
    except Exception as exc:
        log.warning(f"YaDisk: create directory {path} failed: {exc}")
        return False


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
        timeout=aiohttp.ClientTimeout(total=600, connect=30))
    try:
        paths = []
        current = ""
        for part in _bus_root.strip("/").split("/"):
            current += "/" + part
            paths.append(current)
        paths.extend((f"{_bus_root}/to_booth", f"{_bus_root}/to_vps",
                      f"{_bus_root}/done", f"{_bus_root}/done/to_booth",
                      f"{_bus_root}/done/to_vps", f"{_bus_root}/logs", _folder))
        for path in paths:
            if not await _ensure_directory(path):
                await _close_sessions()
                return False
        return True
    except Exception as exc:
        log.warning(f"YaDisk: connection failed: {exc}")
        await _close_sessions()
        return False


async def _list_inbox() -> list[dict]:
    """List every current booth-to-VPS message using documented pagination."""
    result = []
    offset = 0
    inbox = f"{_bus_root}/to_vps"
    while True:
        params = {
            "path": inbox,
            "limit": PAGE_SIZE,
            "offset": offset,
            "sort": "name",
            "fields": "_embedded.total,_embedded.items.name,_embedded.items.path,_embedded.items.type",
        }
        async with _session.get(f"{API}/resources", params=params) as response:
            if response.status == 404:
                return []
            if response.status != 200:
                raise RuntimeError(f"list inbox {response.status}: {await response.text()}")
            embedded = (await response.json()).get("_embedded", {})
        items = embedded.get("items", [])
        result.extend(item for item in items
                      if item.get("type") == "file" and item.get("name", "").endswith(".json"))
        offset += len(items)
        if not items or offset >= int(embedded.get("total", offset)):
            break
    return result


async def _download_bytes(remote_path: str, max_size: int = 1024 * 1024) -> bytes:
    async with _session.get(
        f"{API}/resources/download", params={"path": remote_path}
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"download URL {response.status}: {await response.text()}")
        href = (await response.json())["href"]
    async with _transfer_session.get(href) as response:
        if response.status != 200:
            raise RuntimeError(f"download {response.status}: {await response.text()}")
        payload = await response.read()
    if len(payload) > max_size:
        raise ValueError("inbox message is too large")
    return payload


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _download_file(remote_path: str, local_path: Path, expected: dict) -> None:
    async with _session.get(
        f"{API}/resources/download", params={"path": remote_path}
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"download URL {response.status}: {await response.text()}")
        href = (await response.json())["href"]
    async with _transfer_session.get(href) as response:
        if response.status != 200:
            raise RuntimeError(f"download {response.status}: {await response.text()}")
        with local_path.open("wb") as target:
            async for chunk in response.content.iter_chunked(1024 * 1024):
                target.write(chunk)

    if local_path.stat().st_size != expected["size"]:
        raise ValueError(f"size mismatch for {expected['name']}")
    if expected.get("md5"):
        actual = await asyncio.to_thread(_file_md5, local_path)
        if actual != expected["md5"]:
            raise ValueError(f"md5 mismatch for {expected['name']}")


async def _tg_post(endpoint: str, form: aiohttp.FormData) -> bool:
    base = f"https://api.telegram.org/bot{_tg_token}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=180, connect=30)
        ) as telegram:
            async with telegram.post(f"{base}/{endpoint}", data=form) as response:
                if response.status != 200:
                    log.warning(f"Telegram {endpoint} {response.status}: {await response.text()}")
                    return False
                return True
    except Exception as exc:
        log.warning(f"Telegram {endpoint} failed: {exc}")
        return False


async def _tg_send_chunk(files: list[tuple[Path, str]]) -> bool:
    with ExitStack() as stack:
        form = aiohttp.FormData()
        form.add_field("chat_id", _tg_chat)
        if len(files) == 1:
            path, kind = files[0]
            field = "video" if kind == "video" else "photo"
            form.add_field(field, stack.enter_context(path.open("rb")), filename=path.name)
            return await _tg_post("sendVideo" if kind == "video" else "sendPhoto", form)

        media = []
        for index, (path, kind) in enumerate(files):
            key = f"file{index}"
            media.append({
                "type": "video" if kind == "video" else "photo",
                "media": f"attach://{key}",
            })
            form.add_field(key, stack.enter_context(path.open("rb")), filename=path.name)
        form.add_field("media", json.dumps(media))
        return await _tg_post("sendMediaGroup", form)


async def _tg_send_session(files: list[tuple[Path, str]]) -> bool:
    if not _tg_token or not _tg_chat:
        log.warning("Telegram token/chat is missing")
        return False
    for start in range(0, len(files), 10):
        if not await _tg_send_chunk(files[start:start + 10]):
            return False
    return True


async def _wait_operation(href: str) -> bool:
    for _ in range(30):
        async with _session.get(href) as response:
            if response.status != 200:
                return False
            status = (await response.json()).get("status")
        if status == "success":
            return True
        if status == "failed":
            return False
        await asyncio.sleep(1)
    return False


async def _move_to_done(message_name: str) -> bool:
    source = f"{_bus_root}/to_vps/{message_name}"
    target = f"{_bus_root}/done/to_vps/{message_name}"
    params = {"from": source, "path": target, "overwrite": "true"}
    try:
        async with _session.post(f"{API}/resources/move", params=params) as response:
            if response.status == 201:
                return True
            if response.status == 202:
                href = (await response.json()).get("href")
                return bool(href and await _wait_operation(href))
            log.warning(f"YaDisk: move inbox message {response.status}: {await response.text()}")
            return False
    except Exception as exc:
        log.warning(f"YaDisk: move inbox message failed: {exc}")
        return False


async def _deliver_session(manifest: dict) -> bool:
    log.info(f"YaDisk: session {manifest['session_id']} ready "
             f"({len(manifest['files'])} files from {manifest['event_folder']})")
    with tempfile.TemporaryDirectory(prefix=f"photobooth_{manifest['session_id']}_") as tmpdir:
        local_files = []
        try:
            for entry in manifest["files"]:
                local_path = Path(tmpdir) / entry["name"]
                await _download_file(
                    f"{manifest['event_folder']}/{entry['name']}", local_path, entry)
                local_files.append((local_path, entry["kind"]))
        except Exception as exc:
            log.warning(f"YaDisk: session download failed, keeping inbox: {exc}")
            return False

        if not await _tg_send_session(local_files):
            log.warning(f"YaDisk: Telegram failed for {manifest['session_id']}, keeping inbox")
            return False
    return True


async def _finish_message(
    item: dict,
    processor: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Process once, then durably retry only the move if it fails."""
    message_name = item["name"]
    handled = _state["handled_messages"]
    if message_name not in handled:
        if processor is None or not await processor():
            return False
        handled.append(message_name)
        _state_save()

    if not await _move_to_done(message_name):
        return False
    if message_name in handled:
        handled.remove(message_name)
        _state_save()
    log.info(f"YaDisk: completed inbox message {message_name}")
    return True


async def _process_manifest(item: dict, data: dict | None = None) -> bool:
    """Process one session message directly; primarily useful for live checks."""
    if item["name"] in _state["handled_messages"]:
        return await _finish_message(item)
    try:
        if data is None:
            remote_path = str(item.get("path", "")).removeprefix("disk:")
            data = json.loads((await _download_bytes(remote_path)).decode("utf-8"))
        manifest = validate_manifest(data)
    except Exception as exc:
        log.warning(f"YaDisk: invalid session message {item['name']}: {exc}")
        return False
    return await _finish_message(item, lambda: _deliver_session(manifest))


async def _process_response(
    item: dict,
    data: dict,
    handler: ResponseHandler,
) -> bool:
    try:
        response = yadisk_control.validate_response(data, item["name"])
    except Exception as exc:
        log.warning(f"YaDisk: invalid command response {item['name']}: {exc}")
        return False
    return await _finish_message(item, lambda: handler(response))


async def _message_worker(
    queue: asyncio.Queue,
    response_handler: ResponseHandler,
) -> None:
    while True:
        item, data, message_type = await queue.get()
        try:
            if message_type == "handled":
                await _finish_message(item)
            elif message_type == "session_ready":
                await _process_manifest(item, data)
            else:
                await _process_response(item, data, response_handler)
        except Exception as exc:
            log.warning(f"YaDisk: processing {item.get('name', '?')} failed: {exc}")
        finally:
            _inflight.discard(item.get("name", ""))
            queue.task_done()


async def _poll_once(
    session_queue: asyncio.Queue,
    response_queue: asyncio.Queue,
) -> None:
    if not await _connect():
        return
    for item in await _list_inbox():
        message_name = item["name"]
        if message_name in _inflight:
            continue
        if message_name in _state["handled_messages"]:
            _inflight.add(message_name)
            await response_queue.put((item, None, "handled"))
            continue
        remote_path = str(item.get("path", "")).removeprefix("disk:")
        try:
            data = json.loads((await _download_bytes(remote_path)).decode("utf-8"))
            message_type = data.get("message_type") if isinstance(data, dict) else None
            if message_type == "session_ready":
                validate_manifest(data)
                target_queue = session_queue
            elif message_type == "command_response":
                target_queue = response_queue
            else:
                raise ValueError("unknown message_type")
        except Exception as exc:
            log.warning(f"YaDisk: invalid inbox message {message_name}: {exc}")
            continue
        _inflight.add(message_name)
        await target_queue.put((item, data, message_type))


async def yadisk_poll_loop(response_handler: ResponseHandler) -> None:
    """Poll one stable inbox and dispatch media and responses independently."""
    session_queue = asyncio.Queue()
    response_queue = asyncio.Queue()
    workers = [
        asyncio.create_task(_message_worker(session_queue, response_handler)),
        asyncio.create_task(_message_worker(response_queue, response_handler)),
    ]
    try:
        while True:
            try:
                await _poll_once(session_queue, response_queue)
            except Exception as exc:
                log.warning(f"YaDisk: poll failed: {exc}")
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def yadisk_init(
    folder: str,
    control_folder: str,
    tg_token: str,
    tg_chat: str,
) -> bool:
    """Configure the unified inbox poller and make its initial connection."""
    global _folder, _bus_root, _token, _tg_token, _tg_chat, _configured
    _state_load()
    _inflight.clear()
    _token = os.environ.get("YADISK_TOKEN", "").strip()
    folder_name = str(folder or "").strip().strip("/")
    bus_name = str(control_folder or "").strip().strip("/")
    if not _token:
        log.warning("YaDisk: YADISK_TOKEN not set")
        return False
    if not folder_name or any(part in ("", ".", "..") for part in folder_name.split("/")):
        log.warning("YaDisk: event folder is missing or invalid")
        return False
    if not bus_name or any(part in ("", ".", "..") for part in bus_name.split("/")):
        log.warning("YaDisk: control folder is missing or invalid")
        return False

    _folder = "/" + folder_name
    _bus_root = "/" + bus_name
    _tg_token = tg_token
    _tg_chat = tg_chat
    _configured = True
    connected = await _connect()
    if not connected:
        log.warning("YaDisk: initial connection failed; poll loop will retry")
    else:
        log.info(f"YaDisk: watching {_bus_root}/to_vps every {POLL_INTERVAL}s")
    return True


def validate_event_name(folder: str) -> str:
    name = str(folder or "").strip()
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or any(ord(char) < 32 for char in name) or len(name) > 160):
        raise ValueError("invalid event folder name")
    return name


async def set_event_folder(folder: str) -> None:
    """Activate the event folder used by /link and persisted VPS state."""
    global _folder
    name = validate_event_name(folder)
    target = "/" + name
    if target == _folder:
        return
    if not await _connect():
        raise RuntimeError("Yandex.Disk poller is unavailable")
    if not await _ensure_directory(target):
        raise RuntimeError(f"cannot create event directory {target}")
    _folder = target
    _state_save()
    log.info(f"YaDisk: active event changed to {_folder}")


def current_event_folder() -> str:
    return _folder.lstrip("/")


async def publish_current_folder() -> str:
    if not await _connect():
        raise RuntimeError("Yandex.Disk poller is unavailable")
    async with _session.put(
        f"{API}/resources/publish", params={"path": _folder},
    ) as response:
        if response.status not in (200, 201, 409):
            raise RuntimeError(f"publish event: {response.status} {await response.text()}")
    async with _session.get(
        f"{API}/resources", params={"path": _folder, "fields": "public_url"},
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"read public URL: {response.status} {await response.text()}")
        public_url = (await response.json()).get("public_url")
    if not public_url:
        raise RuntimeError("Yandex.Disk did not return public_url")
    return public_url


async def yadisk_close() -> None:
    await _close_sessions()
