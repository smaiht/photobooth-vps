"""Opt-in smoke test for the production Yandex.Disk update path.

Run with YADISK_TOKEN in the process environment. The test only uses a unique
temporary folder and removes it even when a verification step fails.
"""

import asyncio
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yadisk_updates as publisher


API = "https://cloud-api.yandex.net/v1/disk"


def _load_booth_client():
    client_path = (Path(__file__).resolve().parents[2]
                   / "photobooth" / "backend" / "yadisk_updates.py")
    spec = importlib.util.spec_from_file_location("booth_yadisk_updates", client_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _test_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("smoke_test.txt", "Yandex.Disk update smoke test")
    return output.getvalue()


async def _check_token(token: str) -> None:
    headers = {"Authorization": f"OAuth {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(API) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Yandex.Disk authorization failed: HTTP {response.status} {await response.text()}")


async def _delete_and_confirm(token: str, folder: str) -> None:
    headers = {"Authorization": f"OAuth {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.delete(
            f"{API}/resources",
            params={"path": f"/{folder}", "permanently": "true"},
        ) as response:
            if response.status not in (202, 204, 404):
                raise RuntimeError(f"cleanup failed: HTTP {response.status} {await response.text()}")

        for _ in range(20):
            async with session.get(
                f"{API}/resources", params={"path": f"/{folder}"},
            ) as response:
                if response.status == 404:
                    return
                if response.status not in (200, 423):
                    raise RuntimeError(
                        f"cleanup verification failed: HTTP {response.status} {await response.text()}")
            await asyncio.sleep(1)
    raise RuntimeError("temporary update folder still exists after cleanup")


async def main() -> None:
    token = os.environ.get("YADISK_TOKEN", "").strip()
    if not token:
        raise RuntimeError("YADISK_TOKEN is not set")
    await _check_token(token)

    folder = f"photobooth_system/update_smoke_{uuid.uuid4().hex}"
    payload = _test_zip()
    booth_client = _load_booth_client()
    try:
        published = await publisher.publish_update(
            payload, "small", folder, source_url="live-smoke-test")
        status = await asyncio.to_thread(booth_client.read_status, folder)
        if status != published:
            raise RuntimeError("downloaded status.json differs from the published status")

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "update.zip"
            size, sha256 = await asyncio.to_thread(
                booth_client.download_artifact, status, destination)
            if destination.read_bytes() != payload:
                raise RuntimeError("downloaded artifact bytes differ")
            if size != len(payload) or sha256 != hashlib.sha256(payload).hexdigest():
                raise RuntimeError("downloaded artifact metadata differs")
            with zipfile.ZipFile(destination) as archive:
                if archive.testzip() is not None:
                    raise RuntimeError("downloaded ZIP failed CRC validation")
    finally:
        await _delete_and_confirm(token, folder)

    print(f"OK: published, downloaded, verified and deleted {folder}")


if __name__ == "__main__":
    asyncio.run(main())
