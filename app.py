"""Photobooth VPS: poll WebDAV -> download -> unpack -> thumbnails -> Telegram."""

import asyncio
import json
import logging
import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from PIL import Image

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

WEBDAV_URL = os.environ.get("WEBDAV_URL", "")
WEBDAV_LOGIN = os.environ.get("BEELINECLOUD_LOGIN", "")
WEBDAV_PASS = os.environ.get("BEELINECLOUD_PASSWORD", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)
POLL_INTERVAL = 10
PROCESSING_TTL = 300
THUMB_SIZE = (400, 400)

_processing: dict[str, float] = {}


def _is_processing(filename: str) -> bool:
    now = asyncio.get_event_loop().time()
    expired = [k for k, t in _processing.items() if now - t > PROCESSING_TTL]
    for k in expired:
        del _processing[k]
    return filename in _processing


def _mark_processing(filename: str):
    _processing[filename] = asyncio.get_event_loop().time()


# --- WebDAV ---

async def webdav_list(session: aiohttp.ClientSession) -> list[str]:
    async with session.request("PROPFIND", f"{WEBDAV_URL}/",
                               headers={"Depth": "1"},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 207:
            return []
        body = await r.text()
        root = ET.fromstring(body)
        files = []
        for href in root.findall(".//{DAV:}href"):
            if href.text and href.text.endswith(".zip"):
                files.append(href.text.split("/")[-1])
        return files


async def webdav_download(session: aiohttp.ClientSession, filename: str, dest: Path) -> bool:
    """Download file in chunks to disk. Returns True on success."""
    url = f"{WEBDAV_URL}/{filename}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=600, sock_read=300)) as r:
            if r.status != 200:
                log.warning(f"Download {filename}: HTTP {r.status}")
                return False
            total = int(r.headers.get("Content-Length", 0))
            log.info(f"Downloading {filename} ({total/1024/1024:.1f}MB)...")
            with open(dest, "wb") as f:
                downloaded = 0
                async for chunk in r.content.iter_chunked(64 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
            log.info(f"Downloaded {filename} ({downloaded/1024/1024:.1f}MB)")
            return True
    except Exception as e:
        log.warning(f"Download {filename} failed: {e}")
        dest.unlink(missing_ok=True)
        return False


async def webdav_delete(session: aiohttp.ClientSession, filename: str):
    async with session.delete(f"{WEBDAV_URL}/{filename}",
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
        log.info(f"Deleted {filename} from cloud ({r.status})")


# --- Thumbnails ---

def generate_thumbnails(session_dir: Path):
    thumbs = []
    for f in sorted(session_dir.glob("photo_*.jpg")):
        thumb_name = f"thumb_{f.name}"
        thumb = session_dir / thumb_name
        if not thumb.exists():
            img = Image.open(f)
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            img.save(thumb, "JPEG", quality=60, optimize=True)
        thumbs.append({"photo": f.name, "thumb": thumb_name})

    video = session_dir / "video.mp4"
    files = {"photos": thumbs, "video": "video.mp4" if video.exists() else None}
    (session_dir / "files.json").write_text(json.dumps(files))
    log.info(f"Thumbnails + files.json generated in {session_dir.name}")


# --- Telegram ---

async def tg_upload(session: aiohttp.ClientSession, session_dir: Path):
    photos = sorted(session_dir.glob("photo_*.jpg"))
    video = session_dir / "video.mp4"
    if not video.exists():
        video = None

    base = f"https://api.telegram.org/bot{TG_TOKEN}"

    # Media album: compressed photos + video
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
        await _tg_send_media_group(session, base, media, files)
        log.info(f"TG: sent media album ({len(media)} items)")

    # Document album: originals
    media = []
    files = {}
    for i, p in enumerate(photos):
        key = f"doc{i}"
        media.append({"type": "document", "media": f"attach://{key}"})
        files[key] = p

    if media:
        await _tg_send_media_group(session, base, media, files)
        log.info(f"TG: sent originals ({len(media)} items)")


async def _tg_send_media_group(session: aiohttp.ClientSession, base: str,
                                media: list, files: dict[str, Path]):
    data = aiohttp.FormData()
    data.add_field("chat_id", TG_CHAT)
    data.add_field("media", json.dumps(media))
    for name, path in files.items():
        data.add_field(name, open(path, "rb"), filename=path.name)

    async with session.post(f"{base}/sendMediaGroup", data=data,
                            timeout=aiohttp.ClientTimeout(total=120)) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Telegram {resp.status}: {body}")


# --- Process ---

async def process_zip(session: aiohttp.ClientSession, filename: str):
    log.info(f"Processing {filename}...")

    session_id = filename.replace(".zip", "")
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(exist_ok=True)
    zip_path = session_dir / filename

    # Download with retries
    ok = False
    for attempt in range(5):
        ok = await webdav_download(session, filename, zip_path)
        if ok:
            break
        log.warning(f"Retry {attempt+1}/5 for {filename}")
        await asyncio.sleep(5)

    if not ok:
        log.error(f"Failed to download {filename} after 5 attempts")
        _processing.pop(filename, None)
        return

    await webdav_delete(session, filename)

    # Unpack
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(session_dir)
        zip_path.unlink()
        log.info(f"Unpacked {session_id}")
    except Exception as e:
        log.error(f"Unpack failed: {e}")
        _processing.pop(filename, None)
        return

    # Generate thumbnails
    await asyncio.get_event_loop().run_in_executor(None, generate_thumbnails, session_dir)

    # Upload to Telegram
    if TG_TOKEN and TG_CHAT:
        for attempt in range(5):
            try:
                await tg_upload(session, session_dir)
                log.info(f"Session {session_id} done")
                return
            except Exception as e:
                log.warning(f"TG attempt {attempt + 1}/5: {e}")
                await asyncio.sleep(2)
        log.error(f"TG failed for {session_id}")

    _processing.pop(filename, None)


# --- Main ---

async def main():
    log.info(f"Started. WebDAV: {WEBDAV_URL}, TG chat: {TG_CHAT}")

    auth = aiohttp.BasicAuth(WEBDAV_LOGIN, WEBDAV_PASS)
    async with aiohttp.ClientSession(auth=auth) as session:
        while True:
            try:
                files = await webdav_list(session)
                for f in files:
                    if not _is_processing(f):
                        _mark_processing(f)
                        asyncio.create_task(process_zip(session, f))
            except Exception as e:
                log.warning(f"Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
