"""Consume completed photobooth sessions from a Yandex.Disk manifest inbox.

The booth uploads event media to the event folder and publishes a manifest to
``_sessions/inbox`` last.  After all files are verified and Telegram accepts
them, this service moves only the manifest to ``_sessions/done``.  Media stay
in the event folder for its public owner link.
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

import aiohttp

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
POLL_INTERVAL = 5
PAGE_SIZE = 1000
STATE_FILE = Path("vps_yadisk_state.json")
SCHEMA_VERSION = 1
MD5_RE = re.compile(r"^[a-f0-9]{32}$")

_state: dict = {"sent_manifests": []}
_session: aiohttp.ClientSession | None = None
_transfer_session: aiohttp.ClientSession | None = None
_folder = ""
_token = ""
_tg_token = ""
_tg_chat = ""
_configured = False


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
        _state = {"sent_manifests": []}
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        sent = data.get("sent_manifests", []) if isinstance(data, dict) else []
        _state = {"sent_manifests": [str(name) for name in sent]}
    except Exception as exc:
        log.warning(f"YaDisk: state load failed: {exc}")
        _state = {"sent_manifests": []}


def validate_manifest(data: dict) -> dict:
    """Validate untrusted manifest data and return a normalized copy."""
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema")

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
        for path in (_folder, f"{_folder}/_sessions", f"{_folder}/_sessions/inbox",
                     f"{_folder}/_sessions/done"):
            if not await _ensure_directory(path):
                await _close_sessions()
                return False
        return True
    except Exception as exc:
        log.warning(f"YaDisk: connection failed: {exc}")
        await _close_sessions()
        return False


async def _list_inbox() -> list[dict]:
    """List every current inbox manifest using documented pagination."""
    result = []
    offset = 0
    inbox = f"{_folder}/_sessions/inbox"
    while True:
        params = {
            "path": inbox,
            "limit": PAGE_SIZE,
            "offset": offset,
            "sort": "name",
            "fields": "_embedded.total,_embedded.items.name,_embedded.items.path,_embedded.items.type",
        }
        async with _session.get(f"{API}/resources", params=params) as response:
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


async def _download_bytes(remote_path: str) -> bytes:
    async with _session.get(
        f"{API}/resources/download", params={"path": remote_path}
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"download URL {response.status}: {await response.text()}")
        href = (await response.json())["href"]
    async with _transfer_session.get(href) as response:
        if response.status != 200:
            raise RuntimeError(f"download {response.status}: {await response.text()}")
        return await response.read()


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


async def _move_to_done(manifest_name: str) -> bool:
    source = f"{_folder}/_sessions/inbox/{manifest_name}"
    target = f"{_folder}/_sessions/done/{manifest_name}"
    params = {"from": source, "path": target, "overwrite": "true"}
    try:
        async with _session.post(f"{API}/resources/move", params=params) as response:
            if response.status == 201:
                return True
            if response.status == 202:
                href = (await response.json()).get("href")
                return bool(href and await _wait_operation(href))
            log.warning(f"YaDisk: move manifest {response.status}: {await response.text()}")
            return False
    except Exception as exc:
        log.warning(f"YaDisk: move manifest failed: {exc}")
        return False


async def _process_manifest(item: dict) -> bool:
    manifest_name = item["name"]
    manifest_path = str(item.get("path", "")).removeprefix("disk:")
    sent = _state["sent_manifests"]

    if manifest_name not in sent:
        try:
            raw = await _download_bytes(manifest_path)
            manifest = validate_manifest(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            log.warning(f"YaDisk: invalid manifest {manifest_name}: {exc}")
            return False

        log.info(f"YaDisk: session {manifest['session_id']} ready "
                 f"({len(manifest['files'])} files)")
        with tempfile.TemporaryDirectory(prefix=f"photobooth_{manifest['session_id']}_") as tmpdir:
            local_files = []
            try:
                for entry in manifest["files"]:
                    local_path = Path(tmpdir) / entry["name"]
                    await _download_file(f"{_folder}/{entry['name']}", local_path, entry)
                    local_files.append((local_path, entry["kind"]))
            except Exception as exc:
                log.warning(f"YaDisk: session download failed, keeping inbox: {exc}")
                return False

            if not await _tg_send_session(local_files):
                log.warning(f"YaDisk: Telegram failed for {manifest['session_id']}, keeping inbox")
                return False

        sent.append(manifest_name)
        _state_save()

    if not await _move_to_done(manifest_name):
        return False
    if manifest_name in sent:
        sent.remove(manifest_name)
        _state_save()
    log.info(f"YaDisk: completed manifest {manifest_name}")
    return True


async def _poll_once() -> None:
    if not await _connect():
        return
    for item in await _list_inbox():
        await _process_manifest(item)


async def yadisk_poll_loop() -> None:
    """Poll forever; all failed manifests remain in the durable inbox."""
    while True:
        try:
            await _poll_once()
        except Exception as exc:
            log.warning(f"YaDisk: poll failed: {exc}")
            await _close_sessions()
        await asyncio.sleep(POLL_INTERVAL)


async def yadisk_init(folder: str, tg_token: str, tg_chat: str) -> bool:
    """Configure the poller. The loop retries if the initial connection fails."""
    global _folder, _token, _tg_token, _tg_chat, _configured
    _state_load()
    _token = os.environ.get("YADISK_TOKEN", "").strip()
    folder_name = str(folder or "").strip().strip("/")
    if not _token:
        log.warning("YaDisk: YADISK_TOKEN not set")
        return False
    if not folder_name or any(part in ("", ".", "..") for part in folder_name.split("/")):
        log.warning("YaDisk: event folder is missing or invalid")
        return False

    _folder = "/" + folder_name
    _tg_token = tg_token
    _tg_chat = tg_chat
    _configured = True
    connected = await _connect()
    if not connected:
        log.warning("YaDisk: initial connection failed; poll loop will retry")
    else:
        log.info(f"YaDisk: watching {_folder}/_sessions/inbox")
    return True


async def yadisk_close() -> None:
    await _close_sessions()
