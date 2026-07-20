import io
import json
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

import app
import yadisk_updates


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DownloadContent:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def iter_chunked(self, chunk_size: int):
        for start in range(0, len(self.payload), chunk_size):
            yield self.payload[start:start + chunk_size]


class _DownloadResponse:
    status = 200

    def __init__(self, payload: bytes):
        self.content_length = len(payload)
        self.content = _DownloadContent(payload)
        self.url = "https://release-assets.test/immutable.zip"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DownloadSession(_Session):
    def __init__(self, payload: bytes):
        self.response = _DownloadResponse(payload)

    def get(self, *args, **kwargs):
        return self.response


class _PayloadWriter:
    def __init__(self):
        self.data = bytearray()

    async def write(self, chunk):
        self.data.extend(chunk)


class PublishUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_payload_streams_bytes_and_logs_progress(self):
        payload = b"artifact bytes"
        body = yadisk_updates._ProgressBytesPayload(payload, "/updates/full.zip")
        writer = _PayloadWriter()

        with self.assertLogs("yadisk_updates", level="INFO") as captured:
            await body.write(writer)

        self.assertEqual(bytes(writer.data), payload)
        self.assertEqual(body.size, len(payload))
        self.assertIn("upload progress", "\n".join(captured.output))

    async def test_uploads_full_artifact_before_status_pointer(self):
        uploads = []

        async def capture_upload(api_session, transfer_session, path, payload):
            uploads.append((path, payload))

        with patch.dict("os.environ", {"YADISK_TOKEN": "test-token"}), \
             patch("yadisk_updates.aiohttp.ClientSession", side_effect=[_Session(), _Session()]), \
             patch("yadisk_updates._ensure_directories", AsyncMock()), \
             patch("yadisk_updates._upload_bytes", side_effect=capture_upload):
            status = await yadisk_updates.publish_update(
                b"zip payload", "photobooth_system/updates")

        self.assertEqual(len(uploads), 2)
        self.assertEqual(uploads[0][0], status["artifacts"]["full"]["path"])
        self.assertTrue(uploads[0][0].endswith("/full.zip"))
        self.assertEqual(uploads[0][1], b"zip payload")
        self.assertEqual(uploads[1][0], "/photobooth_system/updates/status.json")
        self.assertEqual(json.loads(uploads[1][1]), status)
        self.assertEqual(status["active"], "full")
        self.assertEqual(set(status["artifacts"]), {"full"})

    async def test_imports_resolved_release_before_status_pointer(self):
        imports = []
        uploads = []

        async def capture_import(api_session, source_url, path, payload):
            imports.append((source_url, path, payload))

        async def capture_upload(api_session, transfer_session, path, payload):
            uploads.append((path, payload))

        with patch.dict("os.environ", {"YADISK_TOKEN": "test-token"}), \
             patch("yadisk_updates.aiohttp.ClientSession", side_effect=[_Session(), _Session()]), \
             patch("yadisk_updates._ensure_directories", AsyncMock()), \
             patch("yadisk_updates._import_url", side_effect=capture_import), \
             patch("yadisk_updates._upload_bytes", side_effect=capture_upload):
            status = await yadisk_updates.publish_update(
                b"zip payload",
                "photobooth_system/updates",
                source_url="https://release-assets.test/immutable.zip",
            )

        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0][0], "https://release-assets.test/immutable.zip")
        self.assertEqual(imports[0][1], status["artifacts"]["full"]["path"])
        self.assertEqual(imports[0][2], b"zip payload")
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][0], "/photobooth_system/updates/status.json")

    async def test_falls_back_to_direct_put_when_import_fails(self):
        uploads = []

        async def capture_upload(api_session, transfer_session, path, payload):
            uploads.append((path, payload))

        with patch.dict("os.environ", {"YADISK_TOKEN": "test-token"}), \
             patch("yadisk_updates.aiohttp.ClientSession", side_effect=[_Session(), _Session()]), \
             patch("yadisk_updates._ensure_directories", AsyncMock()), \
             patch("yadisk_updates._import_url", AsyncMock(
                 side_effect=RuntimeError("import unavailable"))), \
             patch("yadisk_updates._upload_bytes", side_effect=capture_upload):
            status = await yadisk_updates.publish_update(
                b"zip payload",
                "photobooth_system/updates",
                source_url="https://release-assets.test/immutable.zip",
            )

        self.assertEqual(len(uploads), 2)
        self.assertEqual(uploads[0][0], status["artifacts"]["full"]["path"])
        self.assertEqual(uploads[1][0], "/photobooth_system/updates/status.json")

    async def test_update_streams_download_and_logs_pipeline(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("app.py", "print('updated')")
        payload = archive.getvalue()
        digest = "a" * 64
        published = {
            "artifacts": {
                "full": {"sha256": digest},
            },
        }

        with patch.object(app, "GITHUB_RELEASE_URL", "https://github.test/release.zip"), \
             patch("app.aiohttp.ClientSession", return_value=_DownloadSession(payload)), \
             patch("app.yadisk_updates.publish_update", AsyncMock(return_value=published)) as publish, \
             self.assertLogs("app", level="INFO") as captured:
            result = await app._do_update()

        publish.assert_awaited_once_with(
            payload, app.CONFIG.get(
                "yadisk_updates_folder", "photobooth_system/updates"),
            source_url="https://release-assets.test/immutable.zip",
        )
        self.assertIn("Полное обновление загружено на Диск", result)
        messages = "\n".join(captured.output)
        self.assertIn("GitHub download complete", messages)
        self.assertIn("validating downloaded ZIP CRC", messages)
        self.assertIn("publishing to Yandex.Disk", messages)
        self.assertIn("finished successfully", messages)


if __name__ == "__main__":
    unittest.main()
