"""Photobooth VPS: poll Yandex Notes -> decrypt -> unpack -> thumbnails -> Telegram."""

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import os
import zipfile
from pathlib import Path
from PIL import Image
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- Config ---
YANOTES_SESSION_ID = os.environ.get("YANOTES_SESSION_ID", "")
YANOTES_SECRET = os.environ.get("YANOTES_SECRET", "photobooth")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
TG_ADMIN = os.environ.get("TG_ADMIN_ID", "")
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)
POLL_INTERVAL = 1
THUMB_SIZE = (400, 400)

BASE = "https://cloud-api.yandex.ru/yadisk_web/v1"
UPLOAD_NOTES = ["pb2vps_1", "pb2vps_2", "pb2vps_3", "pb2vps_4", "pb2vps_5", "pb2vps_6"]
CMD_NOTE = "vps2pb"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://disk.yandex.ru",
    "Referer": "https://disk.yandex.ru/",
    "Accept": "application/json",
}


# --- Crypto ---

def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(YANOTES_SECRET.encode()).digest()))


def _encrypt_str(text: str) -> str:
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> bytes:
    return _fernet().decrypt(token.encode("ascii"))


def _decrypt_str(token: str) -> str:
    return _decrypt(token).decode("utf-8")


# --- Yandex Notes (async aiohttp) ---

async def _list_notes(s: aiohttp.ClientSession) -> list[dict]:
    async with s.get(f"{BASE}/notes/notes", timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        notes = await r.json()
    if isinstance(notes, dict):
        notes = notes.get("items", notes.get("notes", []))
    return [n for n in notes if 1 not in n.get("tags", [])]


async def _get_note_content(s: aiohttp.ClientSession, note_id: str) -> dict:
    async with s.get(f"{BASE}/notes/notes/{note_id}/content",
                     timeout=aiohttp.ClientTimeout(total=120)) as r:
        r.raise_for_status()
        return await r.json()


async def _put_note_content(s: aiohttp.ClientSession, note_id: str, payload: str, snippet: str):
    content = {"name": "$root", "children": [
        {"name": "paragraph", "children": [
            {"data": ".", "attributes": [["d", payload]]} if payload else {"data": "."}
        ]},
    ]}
    body = {
        "content": json.dumps(content, separators=(",", ":")),
        "snippet": snippet,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Mtime": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    async with s.put(f"{BASE}/notes/notes/{note_id}/content_with_meta",
                     headers=headers, data=json.dumps(body),
                     timeout=aiohttp.ClientTimeout(total=120)) as r:
        r.raise_for_status()


async def _clear_note(s: aiohttp.ClientSession, note_id: str):
    await _put_note_content(s, note_id, "", "")


async def _get_db_revision(s: aiohttp.ClientSession) -> int:
    async with s.get(f"{BASE}/data/app/databases/.ext.yanotes@notes",
                     timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        data = await r.json()
    return data.get("revision", 0)


async def _get_deltas(s: aiohttp.ClientSession, base_revision: int) -> dict:
    async with s.get(f"{BASE}/data/app/databases/.ext.yanotes@notes/deltas",
                     params={"base_revision": base_revision, "limit": 100},
                     timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json()


# --- Processing ---

def generate_thumbnails(session_dir: Path):
    thumbs = []
    for f in sorted(session_dir.glob("photo_*.jpg")):
        thumb = session_dir / f"thumb_{f.name}"
        if not thumb.exists():
            img = Image.open(f)
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            img.save(thumb, "JPEG", quality=60, optimize=True)
        thumbs.append({"photo": f.name, "thumb": thumb.name})

    video = session_dir / "video.mp4"
    files = {"photos": thumbs, "video": "video.mp4" if video.exists() else None}
    (session_dir / "files.json").write_text(json.dumps(files))
    log.info(f"Thumbnails + files.json: {session_dir.name}")


def process_zip_data(zip_data: bytes, session_id: str) -> Path:
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(exist_ok=True)
    zip_path = session_dir / f"{session_id}.zip"
    zip_path.write_bytes(zip_data)
    log.info(f"Wrote ZIP: {zip_path} ({len(zip_data)/1048576:.1f}MB)")

    with zipfile.ZipFile(zip_path, "r") as zf:
        log.info(f"ZIP contents: {zf.namelist()}")
        zf.extractall(session_dir)
    zip_path.unlink()
    log.info(f"Unpacked {session_id}")

    generate_thumbnails(session_dir)
    return session_dir


# --- Telegram ---

async def tg_upload(session_dir: Path):
    photos = sorted(session_dir.glob("photo_*.jpg"))
    video = session_dir / "video.mp4"
    if not video.exists():
        video = None
    base = f"https://api.telegram.org/bot{TG_TOKEN}"

    async with aiohttp.ClientSession() as s:
        media, files = [], {}
        for i, p in enumerate(photos):
            key = f"photo{i}"
            media.append({"type": "photo", "media": f"attach://{key}"})
            files[key] = p
        if video:
            media.append({"type": "video", "media": "attach://video"})
            files["video"] = video
        if media:
            await _tg_send(s, base, media, files)
            log.info(f"TG: sent media ({len(media)} items)")

        media, files = [], {}
        for i, p in enumerate(photos):
            key = f"doc{i}"
            media.append({"type": "document", "media": f"attach://{key}"})
            files[key] = p
        if media:
            await _tg_send(s, base, media, files)
            log.info(f"TG: sent originals ({len(media)} items)")


async def _tg_send(session, base, media, files):
    data = aiohttp.FormData()
    data.add_field("chat_id", TG_CHAT)
    data.add_field("media", json.dumps(media))
    for name, path in files.items():
        data.add_field(name, open(path, "rb"), filename=path.name)
    async with session.post(f"{base}/sendMediaGroup", data=data,
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
        if r.status != 200:
            raise RuntimeError(f"TG {r.status}: {await r.text()}")


# --- Send command to photobooth ---

async def send_command(s: aiohttp.ClientSession, cmd_note_id: str, cmd: str, data: str | None = None):
    encrypted_snippet = _encrypt_str(cmd)
    encrypted_data = _encrypt_str(data) if data else ""
    log.info(f"Sending command '{cmd}' to photobooth...")
    await _put_note_content(s, cmd_note_id, encrypted_data, encrypted_snippet)
    log.info(f"Command '{cmd}' sent OK")


# --- Process logs note ---

async def _process_logs(s: aiohttp.ClientSession, note_id: str, title: str):
    """Extract logs from note, send to TG, clear."""
    try:
        content = await _get_note_content(s, note_id)
        if isinstance(content, list):
            content = content[0]
        attrs = content["children"][0]["children"][0].get("attributes", [])
        payload = None
        for attr in attrs:
            if attr[0] == "d" and attr[1]:
                payload = attr[1]
                break
        if not payload:
            await _clear_note(s, note_id)
            return
        text = _decrypt_str(payload)
        await _clear_note(s, note_id)
        log.info(f"Logs from {title} ({len(text)//1024}KB), sending to TG")

        if TG_TOKEN and TG_ADMIN:
            base = f"https://api.telegram.org/bot{TG_TOKEN}"
            async with aiohttp.ClientSession() as tg:
                data = aiohttp.FormData()
                data.add_field("chat_id", TG_ADMIN)
                data.add_field("document", text.encode("utf-8"),
                               filename="photobooth.log", content_type="text/plain")
                async with tg.post(f"{base}/sendDocument", data=data,
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        log.warning(f"TG logs send failed: {await r.text()}")
                    else:
                        log.info("Logs sent to TG")
    except Exception as e:
        log.warning(f"Process logs failed: {e}")


# --- Process upload note ---

async def process_note(s: aiohttp.ClientSession, note_id: str, title: str, encrypted_snippet: str):
    try:
        snippet_text = _decrypt_str(encrypted_snippet)
        log.info(f"Processing {title}: type={snippet_text[:20]}")

        if snippet_text == "logs":
            await _process_logs(s, note_id, title)
            return

        # It's a session upload
        session_id = snippet_text

        # Download content
        log.info(f"Fetching content from {title}...")
        content = await _get_note_content(s, note_id)
        if isinstance(content, list):
            content = content[0]

        # Extract payload
        try:
            attrs = content["children"][0]["children"][0].get("attributes", [])
            payload = None
            for attr in attrs:
                if attr[0] == "d" and attr[1]:
                    payload = attr[1]
                    break
        except (KeyError, IndexError):
            payload = None

        if not payload:
            log.error(f"No payload in {title}, clearing")
            await _clear_note(s, note_id)
            return

        # Decrypt
        log.info(f"Decrypting {title} ({len(payload)/1048576:.1f}MB encrypted)...")
        zip_data = _decrypt(payload)
        log.info(f"Decrypted: {len(zip_data)/1048576:.1f}MB")

        # Unpack (CPU-bound)
        session_dir = await asyncio.get_event_loop().run_in_executor(
            None, process_zip_data, zip_data, session_id)

        # Clear note
        log.info(f"Clearing {title}...")
        await _clear_note(s, note_id)
        log.info(f"Cleared {title} -> FREE")

        # Telegram
        if TG_TOKEN and TG_CHAT:
            for attempt in range(3):
                try:
                    await tg_upload(session_dir)
                    break
                except Exception as e:
                    log.warning(f"TG attempt {attempt+1}/3: {e}")
                    await asyncio.sleep(2)

        log.info(f"Session {session_id} DONE")
    except Exception as e:
        log.error(f"Process {title} failed: {e}")
        try:
            await _clear_note(s, note_id)
            log.info(f"Cleared {title} after error")
        except Exception:
            pass


# --- Telegram bot commands ---

async def tg_poll_commands(s: aiohttp.ClientSession, note_map: dict):
    """Poll Telegram for /logs command from admin."""
    if not TG_TOKEN or not TG_CHAT:
        return
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    offset = 0
    async with aiohttp.ClientSession() as tg:
        while True:
            try:
                async with tg.get(f"{base}/getUpdates",
                                  params={"offset": offset, "timeout": 10},
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    data = await r.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message", {})
                    if str(msg.get("chat", {}).get("id")) != TG_ADMIN:
                        continue
                    text = msg.get("text", "")
                    if text == "/logs":
                        cmd_id = note_map.get(CMD_NOTE, {}).get("id")
                        if cmd_id:
                            await send_command(s, cmd_id, "send_logs")
                            log.info("TG: /logs command -> send_logs sent to photobooth")
            except Exception as e:
                log.warning(f"TG poll error: {e}")
                await asyncio.sleep(5)


# --- Main ---

async def main():
    log.info("Starting VPS service...")
    cookies = {"Session_id": YANOTES_SESSION_ID}

    async with aiohttp.ClientSession(headers=HEADERS, cookies=cookies) as s:
        # Find notes
        log.info("Listing notes...")
        notes = await _list_notes(s)
        note_map = {}
        for n in notes:
            t = n.get("title", "")
            if t in UPLOAD_NOTES or t == CMD_NOTE:
                note_map[t] = {"id": n["id"], "snippet": n.get("snippet", "")}
                status = "OCCUPIED" if n.get("snippet") else "FREE"
                log.info(f"  {t}: {n['id']} [{status}]")

        missing = [t for t in UPLOAD_NOTES if t not in note_map]
        if missing:
            log.error(f"Missing notes: {missing}. Run photobooth first to create them.")
            return

        log.info(f"Watching {len(note_map)} notes, TG chat: {TG_CHAT}")

        # Initial revision
        revision = await _get_db_revision(s)
        log.info(f"Base revision: {revision}")

        # Process pending uploads
        for title, info in note_map.items():
            if title in UPLOAD_NOTES and info["snippet"]:
                log.info(f"Pending upload in {title}, processing...")
                asyncio.create_task(process_note(s, info["id"], title, info["snippet"]))

        # Poll loop
        log.info(f"Polling every {POLL_INTERVAL}s...")
        asyncio.create_task(tg_poll_commands(s, note_map))
        while True:
            try:
                deltas = await _get_deltas(s, revision)
                new_rev = deltas.get("revision", revision)
                items = deltas.get("items", [])
                if new_rev != revision:
                    revision = new_rev

                if items:
                    log.info(f"Deltas: {len(items)} changes, revision {revision}")
                    notes = await _list_notes(s)
                    for n in notes:
                        t = n.get("title", "")
                        snippet = n.get("snippet", "")
                        if t in UPLOAD_NOTES and t in note_map:
                            old = note_map[t]["snippet"]
                            if snippet and snippet != old:
                                log.info(f"New upload in {t}")
                                note_map[t]["snippet"] = snippet
                                asyncio.create_task(process_note(s, note_map[t]["id"], t, snippet))
                            elif not snippet and old:
                                log.info(f"Slot {t} confirmed FREE")
                                note_map[t]["snippet"] = ""
            except Exception as e:
                log.warning(f"Poll error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
