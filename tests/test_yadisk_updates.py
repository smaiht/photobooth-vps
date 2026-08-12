import io
import json
import unittest
import zipfile
from unittest.mock import AsyncMock, call, patch

import runtime_config
import vps_update
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

    async def test_api_session_uses_desktop_client_user_agent(self):
        with patch.dict("os.environ", {"YADISK_TOKEN": "test-token"}), \
             patch("yadisk_updates.aiohttp.ClientSession", side_effect=[
                 _Session(),
                 _Session(),
             ]) as client_session, \
             patch("yadisk_updates._ensure_directories", AsyncMock()), \
             patch("yadisk_updates._upload_bytes", AsyncMock()):
            await yadisk_updates.publish_update(
                b"zip payload",
                "photobooth_system/updates",
            )

        headers = client_session.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["Authorization"], "OAuth test-token")
        self.assertEqual(
            headers["User-Agent"],
            yadisk_updates.YADISK_API_USER_AGENT,
        )

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
        self.assertEqual(
            uploads[1][0],
            "/photobooth_system/updates/status_bundle/status.json",
        )
        self.assertEqual(
            status["artifacts"]["full"]["bundle_path"],
            "/photobooth_system/updates/artifacts/full_bundle",
        )
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
        self.assertEqual(
            uploads[0][0],
            "/photobooth_system/updates/status_bundle/status.json",
        )

    async def test_falls_back_to_direct_put_when_import_fails(self):
        uploads = []
        progress = AsyncMock()

        async def capture_upload(api_session, transfer_session, path, payload):
            uploads.append((path, payload))

        with patch.dict("os.environ", {"YADISK_TOKEN": "test-token"}), \
             patch("yadisk_updates.aiohttp.ClientSession", side_effect=[_Session(), _Session()]), \
             patch("yadisk_updates._ensure_directories", AsyncMock()), \
             patch("yadisk_updates._import_url", AsyncMock(
                 side_effect=RuntimeError("import unavailable"))) as import_url, \
             patch("yadisk_updates.asyncio.sleep", AsyncMock()) as sleep, \
             patch("yadisk_updates._upload_bytes", side_effect=capture_upload):
            status = await yadisk_updates.publish_update(
                b"zip payload",
                "photobooth_system/updates",
                source_url="https://release-assets.test/immutable.zip",
                progress_callback=progress,
            )

        self.assertEqual(import_url.await_count, 5)
        self.assertEqual(
            sleep.await_args_list,
            [call(2), call(4), call(8), call(16)],
        )
        self.assertEqual(len(uploads), 2)
        self.assertEqual(uploads[0][0], status["artifacts"]["full"]["path"])
        self.assertEqual(
            uploads[1][0],
            "/photobooth_system/updates/status_bundle/status.json",
        )
        messages = [item.args[0] for item in progress.await_args_list]
        self.assertEqual(len(messages), 6)
        self.assertIn("попытка 1/5", messages[0])
        self.assertIn("Повтор через 2 с", messages[0])
        self.assertIn("попытка 5/5", messages[4])
        self.assertIn("медленную прямую загрузку", messages[5])

    async def test_server_import_retry_can_recover_without_direct_artifact_upload(self):
        uploads = []
        progress = AsyncMock()

        async def capture_upload(api_session, transfer_session, path, payload):
            uploads.append((path, payload))

        import_url = AsyncMock(side_effect=[
            RuntimeError("temporary 1"),
            RuntimeError("temporary 2"),
            None,
        ])
        with patch.dict("os.environ", {"YADISK_TOKEN": "test-token"}), \
             patch("yadisk_updates.aiohttp.ClientSession", side_effect=[_Session(), _Session()]), \
             patch("yadisk_updates._ensure_directories", AsyncMock()), \
             patch("yadisk_updates._import_url", import_url), \
             patch("yadisk_updates.asyncio.sleep", AsyncMock()) as sleep, \
             patch("yadisk_updates._upload_bytes", side_effect=capture_upload):
            await yadisk_updates.publish_update(
                b"zip payload",
                "photobooth_system/updates",
                source_url="https://release-assets.test/immutable.zip",
                progress_callback=progress,
            )

        self.assertEqual(import_url.await_count, 3)
        self.assertEqual(sleep.await_args_list, [call(2), call(4)])
        self.assertEqual(
            [path for path, _payload in uploads],
            ["/photobooth_system/updates/status_bundle/status.json"],
        )
        messages = [item.args[0] for item in progress.await_args_list]
        self.assertEqual(len(messages), 3)
        self.assertIn("попытка 1/5", messages[0])
        self.assertIn("попытка 2/5", messages[1])
        self.assertIn("выполнен с попытки 3/5", messages[2])

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

        progress = AsyncMock()
        updates_folder = runtime_config.updates_folder()
        with patch.object(
            vps_update,
            "GITHUB_RELEASE_URL",
            "https://github.test/release.zip",
        ), patch(
            "vps_update.aiohttp.ClientSession",
            return_value=_DownloadSession(payload),
        ), patch(
            "vps_update.yadisk_updates.publish_update",
            AsyncMock(return_value=published),
        ) as publish, self.assertLogs("vps_update", level="INFO") as captured:
            result = await vps_update.publish_latest_release(
                updates_folder,
                progress,
            )

        publish.assert_awaited_once_with(
            payload,
            updates_folder,
            source_url="https://release-assets.test/immutable.zip",
            progress_callback=progress,
        )
        self.assertIn("Полное обновление загружено на Диск", result)
        messages = "\n".join(captured.output)
        self.assertIn("GitHub download complete", messages)
        self.assertIn("validating downloaded ZIP CRC", messages)
        self.assertIn("publishing to Yandex.Disk", messages)
        self.assertIn("finished successfully", messages)


if __name__ == "__main__":
    unittest.main()
