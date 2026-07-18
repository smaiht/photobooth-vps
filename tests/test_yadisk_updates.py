import json
import unittest
from unittest.mock import AsyncMock, patch

import yadisk_updates


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class PublishUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_artifact_before_status_pointer(self):
        uploads = []

        async def capture_upload(api_session, transfer_session, path, payload):
            uploads.append((path, payload))

        with patch.dict("os.environ", {"YADISK_TOKEN": "test-token"}), \
             patch("yadisk_updates.aiohttp.ClientSession", side_effect=[_Session(), _Session()]), \
             patch("yadisk_updates._ensure_directories", AsyncMock()), \
             patch("yadisk_updates._upload_bytes", side_effect=capture_upload):
            status = await yadisk_updates.publish_update(
                b"zip payload", "small", "photobooth_system/updates", "https://source.test")

        self.assertEqual(len(uploads), 2)
        self.assertEqual(uploads[0][0], status["path"])
        self.assertTrue(uploads[0][0].endswith("-small.zip"))
        self.assertEqual(uploads[0][1], b"zip payload")
        self.assertEqual(uploads[1][0], "/photobooth_system/updates/status.json")
        self.assertEqual(json.loads(uploads[1][1]), status)

    async def test_rejects_unknown_update_kind(self):
        with self.assertRaisesRegex(ValueError, "unsupported update kind"):
            await yadisk_updates.publish_update(b"zip", "delta", "updates")


if __name__ == "__main__":
    unittest.main()
