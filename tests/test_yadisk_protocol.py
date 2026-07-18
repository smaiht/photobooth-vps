import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import yadisk_poll
from yadisk_poll import _process_manifest, _tg_send_chunk, validate_manifest


class ManifestTests(unittest.TestCase):
    def test_accepts_complete_manifest(self):
        manifest = validate_manifest({
            "schema_version": 1,
            "session_id": "abc123def456",
            "created_at": "2026-07-17T12:00:00+00:00",
            "files": [{
                "name": "20260717_150000_abc123def456_photo_01.jpg",
                "kind": "photo",
                "size": 1234,
                "md5": "d41d8cd98f00b204e9800998ecf8427e",
            }],
        })

        self.assertEqual(manifest["session_id"], "abc123def456")
        self.assertEqual(manifest["files"][0]["size"], 1234)

    def test_rejects_paths_and_duplicate_names(self):
        base = {
            "schema_version": 1,
            "session_id": "abc123",
            "files": [],
        }
        bad_path = dict(base, files=[{
            "name": "../photo.jpg", "kind": "photo", "size": 1, "md5": None,
        }])
        duplicate = dict(base, files=[
            {"name": "photo.jpg", "kind": "photo", "size": 1, "md5": None},
            {"name": "photo.jpg", "kind": "photo", "size": 1, "md5": None},
        ])

        with self.assertRaises(ValueError):
            validate_manifest(bad_path)
        with self.assertRaises(ValueError):
            validate_manifest(duplicate)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_photo_uses_send_photo_not_media_group(self):
        with TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "photo.jpg"
            photo.write_bytes(b"jpeg")
            with patch("yadisk_poll._tg_post", AsyncMock(return_value=True)) as post:
                self.assertTrue(await _tg_send_chunk([(photo, "photo")]))
            self.assertEqual(post.await_args.args[0], "sendPhoto")

    async def test_download_failure_keeps_manifest_in_inbox(self):
        manifest = {
            "schema_version": 1,
            "session_id": "abc123",
            "created_at": "2026-07-17T12:00:00+00:00",
            "files": [{
                "name": "photo.jpg",
                "kind": "photo",
                "size": 5,
                "md5": None,
            }],
        }
        yadisk_poll._state = {"sent_manifests": []}
        yadisk_poll._folder = "/event"

        with patch("yadisk_poll._download_bytes", AsyncMock(
                return_value=json.dumps(manifest).encode("utf-8"))), \
             patch("yadisk_poll._download_file", AsyncMock(
                side_effect=RuntimeError("network down"))), \
             patch("yadisk_poll._tg_send_session", AsyncMock()) as send, \
             patch("yadisk_poll._move_to_done", AsyncMock()) as move:
            ok = await _process_manifest({
                "name": "abc123.json",
                "path": "disk:/event/_sessions/inbox/abc123.json",
            })

        self.assertFalse(ok)
        send.assert_not_awaited()
        move.assert_not_awaited()
        self.assertEqual(yadisk_poll._state["sent_manifests"], [])


if __name__ == "__main__":
    unittest.main()
