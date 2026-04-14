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
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- Config ---
YANOTES_SESSION_ID = os.environ.get("YANOTES_SESSION_ID", "")
YANOTES_SECRET = os.environ.get("YANOTES_SECRET", "photobooth")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)
POLL_INTERVAL = 2
THUMB_SIZE = (400, 400)

BASE = "https://cloud-api.yandex.ru/yadisk_web/v1"
UPLOAD_NOTES = ["pb2vps_1", "pb2vps_2", "pb2vps_3"]
CMD_NOTE = "vps2pb"


# --- Crypto ---

def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(YANOTES_SECRET.encode()).digest()))


def _encrypt(data: bytes) -> str:
    return _fernet().encrypt(data).decode("ascii")


def _decrypt(token: str) -> bytes:
    return _fernet().decrypt(token.encode("ascii"))


def _encrypt_str(text: str) -> str:
    return _encrypt(text.encode("utf-8"))


def _decrypt_str(token: str) -> str:
    return _decrypt(token).decode("utf-8")


# --- Yandex Notes ---

def _build_session():
    s = requests.Session()
    s.cookies.set("Session_id", YANOTES_SESSION_ID)
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://disk.yandex.ru",
        "Referer": "https://disk.yandex.ru/",
        "Accept": "application/json",
    })
    return s


def _list_notes(s):
    r = s.get(f"{BASE}/notes/notes", timeout=15)
    r.raise_for_status()
    notes = r.json()
    if isinstance(notes, dict):
        notes = notes.get("items", notes.get("notes", []))
    return [n for n in notes if 1 not in n.get("tags", [])]


def _get_note_content(s, note_id):
    r = s.get(f"{BASE}/notes/notes/{note_id}/content", timeout=120)
    r.raise_for_status()
    return r.json()


def _put_note_content(s, note_id, payload, snippet):
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
    r = s.put(f"{BASE}/notes/notes/{note_id}/content_with_meta",
              headers=headers, data=json.dumps(body), timeout=120)
    r.raise_for_status()


def _clear_note(s, note_id):
    _put_note_content(s, note_id, "", "")


def _get_db_revision(s):
    r = s.get(f"{BASE}/data/app/databases/.ext.yanotes@notes", timeout=15)
    r.raise_for_status()
    return r.json().get("revision", 0)


def _get_deltas(s, base_revision):
    r = s.get(f"{BASE}/data/app/databases/.ext.yanotes@notes/deltas",
              params={"base_revision": base_revision, "limit": 100}, timeout=15)
    r.raise_for_status()
    return r.json()


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


def download_and_decrypt(s, note_id, title):
    """Download note content, decrypt, return (session_id, zip_bytes) or None."""
    log.info(f"Fetching content from {title} (note {note_id})...")
    content = _get_note_content(s, note_id)
    if isinstance(content, list):
        content = content[0]

    try:
        attrs = content["children"][0]["children"][0].get("attributes", [])
        payload = None
        for attr in attrs:
            if attr[0] == "d" and attr[1]:
                payload = attr[1]
                break
        if not payload:
            log.warning(f"No payload in {title}")
            return None
    except (KeyError, IndexError):
        log.warning(f"Bad content structure in {title}")
        return None

    log.info(f"Decrypting {title} ({len(payload)/1048576:.1f}MB encrypted)...")
    decrypted = _decrypt(payload)
    log.info(f"Decrypted: {len(decrypted)/1048576:.1f}MB")
    return decrypted


def process_zip_data(zip_data: bytes, session_id: str):
    """Unpack ZIP and generate thumbnails."""
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(exist_ok=True)
    zip_path = session_dir / f"{session_id}.zip"
    zip_path.write_bytes(zip_data)
    log.info(f"Wrote ZIP: {zip_path} ({len(zip_data)/1048576:.1f}MB)")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        log.info(f"ZIP contents: {names}")
        zf.extractall(session_dir)
    zip_path.unlink()
    log.info(f"Unpacked {session_id}")

    generate_thumbnails(session_dir)
    return session_dir


# --- Telegram ---

async def tg_upload(session_dir: Path):
    import aiohttp
    photos = sorted(session_dir.glob("photo_*.jpg"))
    video = session_dir / "video.mp4"
    if not video.exists():
        video = None
    base = f"https://api.telegram.org/bot{TG_TOKEN}"

    async with aiohttp.ClientSession() as s:
        media = []
        files = {}
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

        media = []
        files = {}
        for i, p in enumerate(photos):
            key = f"doc{i}"
            media.append({"type": "document", "media": f"attach://{key}"})
            files[key] = p
        if media:
            await _tg_send(s, base, media, files)
            log.info(f"TG: sent originals ({len(media)} items)")


async def _tg_send(session, base, media, files):
    import aiohttp
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

def send_command(s, note_map, cmd: str, data: str | None = None):
    """Send encrypted command to photobooth via vps2pb note."""
    note_id = note_map.get(CMD_NOTE, {}).get("id")
    if not note_id:
        log.error("Cannot send command: vps2pb note not found")
        return
    encrypted_snippet = _encrypt_str(cmd)
    encrypted_data = _encrypt_str(data) if data else ""
    log.info(f"Sending command '{cmd}' to photobooth...")
    _put_note_content(s, note_id, encrypted_data, encrypted_snippet)
    log.info(f"Command '{cmd}' sent OK")


# --- Main loop ---

async def process_note(s, note_id, title, encrypted_snippet):
    """Download, decrypt, process one upload note."""
    try:
        # Decrypt session_id from snippet
        session_id = _decrypt_str(encrypted_snippet)
        log.info(f"Processing {title}: session={session_id}")

        # Download and decrypt ZIP
        zip_data = await asyncio.get_event_loop().run_in_executor(
            None, download_and_decrypt, s, note_id, title)
        if not zip_data:
            log.error(f"No data in {title}, clearing")
            await asyncio.get_event_loop().run_in_executor(None, _clear_note, s, note_id)
            return

        # Unpack
        session_dir = await asyncio.get_event_loop().run_in_executor(
            None, process_zip_data, zip_data, session_id)

        # Clear note (mark as free)
        log.info(f"Clearing {title}...")
        await asyncio.get_event_loop().run_in_executor(None, _clear_note, s, note_id)
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
        # Try to clear note so it doesn't block the slot
        try:
            await asyncio.get_event_loop().run_in_executor(None, _clear_note, s, note_id)
            log.info(f"Cleared {title} after error")
        except Exception:
            pass


async def main():
    log.info("Starting VPS service...")
    s = _build_session()

    # Find upload notes
    log.info("Listing notes...")
    notes = _list_notes(s)
    note_map = {}  # {title: {id, snippet}}
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

    # Get initial revision
    revision = _get_db_revision(s)
    log.info(f"Base revision: {revision}")

    # Check for pending uploads (non-empty snippets)
    for title, info in note_map.items():
        if title in UPLOAD_NOTES and info["snippet"]:
            log.info(f"Pending upload in {title}, processing...")
            asyncio.create_task(process_note(s, info["id"], title, info["snippet"]))

    # Poll loop
    log.info(f"Polling every {POLL_INTERVAL}s...")
    while True:
        try:
            deltas = _get_deltas(s, revision)
            new_rev = deltas.get("revision", revision)
            items = deltas.get("items", [])

            if new_rev != revision:
                revision = new_rev

            if not items:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            log.info(f"Deltas: {len(items)} changes, revision now {revision}")

            # Refresh note list to get current snippets
            notes = _list_notes(s)
            for n in notes:
                t = n.get("title", "")
                snippet = n.get("snippet", "")

                if t in UPLOAD_NOTES and t in note_map:
                    old_snippet = note_map[t]["snippet"]
                    if snippet and snippet != old_snippet:
                        log.info(f"New upload detected in {t}")
                        note_map[t]["snippet"] = snippet
                        asyncio.create_task(process_note(s, note_map[t]["id"], t, snippet))
                    elif not snippet and old_snippet:
                        log.info(f"Slot {t} confirmed FREE")
                        note_map[t]["snippet"] = ""

        except Exception as e:
            log.warning(f"Poll error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
