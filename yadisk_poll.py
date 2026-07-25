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
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from aiohttp.payload import Payload

import yadisk_control

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
POLL_INTERVAL = 10
PAGE_SIZE = 1000
STATE_FILE = Path("vps_yadisk_state.json")
SCHEMA_VERSION = 2
MD5_RE = re.compile(r"^[a-f0-9]{32}$")
PRINT_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
PRINT_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
MAX_PRINT_FILE_SIZE = 20 * 1024 * 1024
PRINT_UPLOAD_CHUNK_SIZE = 1024 * 1024
PRINT_UPLOAD_PROGRESS_BYTES = 5 * 1024 * 1024
PRINT_UPLOAD_PROGRESS_SECONDS = 3

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


class _PrintUploadPayload(Payload):
    """Stream one Telegram print file and log actual upload progress."""

    _autoclose = True

    def __init__(self, value: bytes, remote_path: str):
        super().__init__(value, content_type="application/octet-stream")
        self._value = memoryview(value)
        self._size = len(value)
        self._remote_path = remote_path

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self._value.tobytes().decode(encoding, errors)

    async def write(self, writer) -> None:
        started_at = time.monotonic()
        last_report_at = started_at
        last_report_bytes = 0
        next_report_bytes = PRINT_UPLOAD_PROGRESS_BYTES

        for start in range(0, self._size, PRINT_UPLOAD_CHUNK_SIZE):
            end = min(start + PRINT_UPLOAD_CHUNK_SIZE, self._size)
            await writer.write(self._value[start:end])
            now = time.monotonic()
            if (end >= next_report_bytes
                    or now - last_report_at >= PRINT_UPLOAD_PROGRESS_SECONDS
                    or end == self._size):
                interval = max(now - last_report_at, 0.001)
                elapsed = max(now - started_at, 0.001)
                log.info(
                    "YaDisk print: upload progress path=%s %.1f/%.1f MiB "
                    "(%.1f%%), speed=%.1f MiB/s, average=%.1f MiB/s",
                    self._remote_path,
                    end / 1048576,
                    self._size / 1048576,
                    end * 100 / self._size,
                    (end - last_report_bytes) / interval / 1048576,
                    end / elapsed / 1048576,
                )
                last_report_at = now
                last_report_bytes = end
                next_report_bytes = end + PRINT_UPLOAD_PROGRESS_BYTES


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
                      f"{_bus_root}/logs", _folder))
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


async def _upload_print_file(remote_path: str, payload: bytes) -> None:
    size = len(payload)
    log.info(
        "YaDisk print: requesting upload URL path=%s size=%s bytes (%.2f MiB)",
        remote_path, size, size / 1048576,
    )
    async with _session.get(
        f"{API}/resources/upload",
        params={"path": remote_path, "overwrite": "true"},
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"print file upload URL {response.status}: {await response.text()}")
        href = (await response.json())["href"]
    started_at = time.monotonic()
    log.info("YaDisk print: upload started path=%s", remote_path)
    async with _transfer_session.put(
        href,
        data=_PrintUploadPayload(payload, remote_path),
    ) as response:
        if response.status not in (201, 202):
            raise RuntimeError(
                f"print file upload {response.status}: {await response.text()}")
        upload_status = response.status
    elapsed = max(time.monotonic() - started_at, 0.001)
    log.info(
        "YaDisk print: upload complete path=%s HTTP %s size=%s bytes in %.2fs "
        "(average %.2f MiB/s)",
        remote_path, upload_status, size, elapsed, size / elapsed / 1048576,
    )


async def store_print_job(
    job_id: str,
    user_id: int,
    suffix: str,
    image_payload: bytes,
    metadata: dict,
    event_folder: str | None = None,
) -> dict:
    """Store one Telegram image and its TXT metadata on Yandex.Disk."""
    normalized_suffix = str(suffix or "").lower()
    if (not PRINT_JOB_ID_RE.fullmatch(str(job_id or ""))
            or not isinstance(user_id, int) or user_id <= 0
            or not PRINT_SUFFIX_RE.fullmatch(normalized_suffix)
            or not isinstance(image_payload, bytes) or not image_payload
            or len(image_payload) > MAX_PRINT_FILE_SIZE
            or not isinstance(metadata, dict)):
        raise ValueError("invalid print job")
    if not await _connect():
        raise RuntimeError("Yandex.Disk poller is unavailable")

    event_name = validate_event_name(event_folder or _folder.lstrip("/"))
    selected_event_folder = f"/{event_name}"
    sessions_root = f"{selected_event_folder}_by_sessions"
    jobs_root = f"{sessions_root}/0000_print_jobs"
    for path in (sessions_root, jobs_root):
        if not await _ensure_directory(path):
            raise RuntimeError(f"cannot create print job directory {path}")

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    basename = f"{user_id}_{timestamp}_{job_id}"
    image_path = f"{jobs_root}/{basename}{normalized_suffix}"
    info_path = f"{jobs_root}/{basename}.txt"
    info = dict(metadata)
    info.update({
        "job_id": job_id,
        "status": "received_by_vps",
        "event_folder": event_name,
        "stored_at": now.isoformat(),
        "image_path": image_path,
        "info_path": info_path,
        "image_size_bytes": len(image_payload),
    })
    info_payload = (
        json.dumps(info, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    await _upload_print_file(image_path, image_payload)
    await _upload_print_file(info_path, info_payload)
    log.info(
        "YaDisk print: two files stored job=%s user=%s image=%s info=%s",
        job_id, user_id, image_path, info_path,
    )
    return {
        "event_folder": event_name,
        "artifact_path": image_path,
        "info_path": info_path,
    }


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


async def _delete_inbox_message(message_name: str) -> bool:
    path = f"{_bus_root}/to_vps/{message_name}"
    try:
        async with _session.delete(
            f"{API}/resources",
            params={"path": path, "permanently": "true"},
        ) as response:
            if response.status in (204, 404):
                return True
            if response.status == 202:
                href = (await response.json()).get("href")
                return bool(href and await _wait_operation(href))
            log.warning(
                "YaDisk: delete inbox message %s: %s %s",
                message_name, response.status, await response.text(),
            )
            return False
    except Exception as exc:
        log.warning("YaDisk: delete inbox message %s failed: %s", message_name, exc)
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
    """Process once, then durably retry only deletion if it fails."""
    message_name = item["name"]
    handled = _state["handled_messages"]
    if message_name not in handled:
        if processor is None or not await processor():
            return False
        handled.append(message_name)
        _state_save()

    if not await _delete_inbox_message(message_name):
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
