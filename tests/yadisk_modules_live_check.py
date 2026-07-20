"""Live-check the production booth uploader and VPS manifest consumer together.

Telegram is replaced with an in-process receiver that verifies the exact
downloaded bytes. Yandex.Disk requests use the real production modules.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


VPS_ROOT = Path(__file__).resolve().parent.parent
BOOTH_ROOT = VPS_ROOT.parent / "photobooth"
sys.path.insert(0, str(BOOTH_ROOT))

from backend import yadisk_cloud as booth_disk  # noqa: E402


def load_vps_poller():
    spec = importlib.util.spec_from_file_location(
        "photobooth_vps_yadisk_poll", VPS_ROOT / "yadisk_poll.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


async def delete_test_folder(token: str, folder: str) -> None:
    import aiohttp

    headers = {"Authorization": f"OAuth {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.delete(
            f"{booth_disk.API}/resources",
            params={"path": folder, "permanently": "true", "force_async": "true"},
        ) as response:
            if response.status in (204, 404):
                return
            if response.status != 202:
                raise RuntimeError(f"cleanup HTTP {response.status}: {await response.text()}")
            href = (await response.json()).get("href")
        if not href:
            return
        for _ in range(60):
            async with session.get(href) as response:
                data = await response.json()
            if data.get("status") == "success":
                return
            if data.get("status") == "failed":
                raise RuntimeError("cleanup operation failed")
            await asyncio.sleep(1)
        raise RuntimeError("cleanup operation timeout")


async def main() -> int:
    token = os.environ.get("YADISK_TOKEN", "").strip()
    if not token:
        print("YADISK_TOKEN is required", file=sys.stderr)
        return 2

    folder = (
        f"/photobooth_modules_test_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_"
        f"{secrets.token_hex(4)}"
    )
    bus = f"{folder}/control"
    print(f"production module temporary folder: {folder}")
    poller = load_vps_poller()

    try:
        with tempfile.TemporaryDirectory(prefix="photobooth_module_test_") as tmpdir:
            root = Path(tmpdir)
            originals = {
                root / "camera.jpg": b"original-camera-jpeg-bytes",
                root / "print.jpg": b"original-print-jpeg-bytes",
                root / "session.mp4": b"original-session-video-bytes",
            }
            for path, payload in originals.items():
                path.write_bytes(payload)

            job = booth_disk.build_session_job(
                "modulelivecheck",
                [str(root / "camera.jpg")],
                str(root / "print.jpg"),
                str(root / "session.mp4"),
                event_folder=folder,
            )
            booth_disk._token = token
            booth_disk._folder = folder
            booth_disk._bus_root = bus
            booth_disk._configured = True
            if not await booth_disk._connect():
                raise RuntimeError("production booth uploader failed to connect")
            if not await booth_disk._upload_job(job):
                raise RuntimeError("production booth uploader failed to publish session")
            await booth_disk.yadisk_close()
            print("production booth uploader: PASS")

            poller._token = token
            poller._folder = folder
            poller._bus_root = bus
            poller._tg_token = "live-check-not-sent"
            poller._tg_chat = "live-check-not-sent"
            poller._configured = True
            poller._state = {"handled_messages": []}
            poller.STATE_FILE = root / "vps_state.json"
            received = []

            async def receive_originals(files):
                received.extend((path.read_bytes(), kind) for path, kind in files)
                return True

            poller._tg_send_session = receive_originals
            if not await poller._connect():
                raise RuntimeError("production VPS poller failed to connect")
            inbox = await poller._list_inbox()
            if len(inbox) != 1:
                raise RuntimeError(f"expected one manifest, got {len(inbox)}")
            if not await poller._process_manifest(inbox[0]):
                raise RuntimeError("production VPS poller failed to process manifest")
            if await poller._list_inbox():
                raise RuntimeError("manifest remained in inbox after successful delivery")

            expected = [
                (originals[root / "camera.jpg"], "photo"),
                (originals[root / "print.jpg"], "print"),
                (originals[root / "session.mp4"], "video"),
            ]
            if received != expected:
                raise RuntimeError(f"receiver bytes/order mismatch: {received!r}")
            print("production VPS download/verify/original-byte delivery/move: PASS")
            await poller.yadisk_close()

        print("Production module live check: PASS")
        return 0
    finally:
        await booth_disk.yadisk_close()
        await poller.yadisk_close()
        await delete_test_folder(token, folder)
        print("production module temporary folder cleanup: OK")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
