import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import yadisk_poll
import telegram_session_delivery
from yadisk_poll import _process_manifest, validate_manifest


class ManifestTests(unittest.TestCase):
    def test_accepts_complete_manifest(self):
        manifest = validate_manifest({
            "schema_version": 2,
            "message_type": "session_ready",
            "event_folder": "event_2026",
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
        self.assertEqual(manifest["event_folder"], "/event_2026")
        self.assertEqual(manifest["files"][0]["size"], 1234)

    def test_rejects_paths_and_duplicate_names(self):
        base = {
            "schema_version": 2,
            "message_type": "session_ready",
            "event_folder": "event",
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

        with self.assertRaisesRegex(ValueError, "event"):
            validate_manifest(dict(base, event_folder="../other", files=[{
                "name": "photo.jpg", "kind": "photo", "size": 1, "md5": None,
            }]))


class ConnectionSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_session_uses_desktop_client_user_agent(self):
        api_session = MagicMock(closed=False)
        transfer_session = MagicMock(closed=False)
        with patch.object(yadisk_poll, "_configured", True), \
             patch.object(yadisk_poll, "_token", "secret"), \
             patch.object(yadisk_poll, "_folder", "/event"), \
             patch.object(yadisk_poll, "_bus_root", "/control"), \
             patch.object(yadisk_poll, "_session", None), \
             patch.object(yadisk_poll, "_transfer_session", None), \
             patch("yadisk_poll.aiohttp.ClientSession", side_effect=[
                 api_session,
                 transfer_session,
             ]) as client_session, \
             patch("yadisk_poll._ensure_directory", AsyncMock(return_value=True)):
            self.assertTrue(await yadisk_poll._connect())

        headers = client_session.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["Authorization"], "OAuth secret")
        self.assertEqual(
            headers["User-Agent"],
            yadisk_poll.YADISK_API_USER_AGENT,
        )


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        yadisk_poll._inflight.clear()

    def tearDown(self):
        yadisk_poll._inflight.clear()

    async def test_single_photo_uses_send_photo_not_media_group(self):
        with TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "photo.jpg"
            photo.write_bytes(b"jpeg")
            with patch(
                "telegram_session_delivery._post",
                AsyncMock(return_value=True),
            ) as post:
                self.assertTrue(await telegram_session_delivery._send_chunk(
                    [(photo, "photo")],
                ))
            self.assertEqual(post.await_args.args[0], "sendPhoto")

    async def test_download_failure_keeps_manifest_in_inbox(self):
        manifest = {
            "schema_version": 2,
            "message_type": "session_ready",
            "event_folder": "event",
            "session_id": "abc123",
            "created_at": "2026-07-17T12:00:00+00:00",
            "files": [{
                "name": "photo.jpg",
                "kind": "photo",
                "size": 5,
                "md5": None,
            }],
        }
        yadisk_poll._state = {"handled_messages": []}

        download_file = AsyncMock(side_effect=RuntimeError("network down"))
        with patch("yadisk_poll._download_bytes", AsyncMock(
                return_value=json.dumps(manifest).encode("utf-8"))), \
             patch("yadisk_poll._download_file", download_file), \
             patch(
                 "yadisk_poll.telegram_session_delivery.send_session",
                 AsyncMock(),
             ) as send, \
             patch("yadisk_poll._delete_inbox_message", AsyncMock()) as delete:
            ok = await _process_manifest({
                "name": "abc123.json",
                "path": "disk:/photobooth_system/control/to_vps/abc123.json",
            })

        self.assertFalse(ok)
        send.assert_not_awaited()
        delete.assert_not_awaited()
        self.assertEqual(download_file.await_args.args[0], "/event/photo.jpg")
        self.assertEqual(yadisk_poll._state["handled_messages"], [])

    async def test_one_inbox_dispatches_sessions_and_responses_separately(self):
        session = {
            "schema_version": 2,
            "message_type": "session_ready",
            "event_folder": "event",
            "session_id": "abc123",
            "files": [{
                "name": "photo.jpg", "kind": "photo", "size": 5, "md5": None,
            }],
        }
        response = {
            "schema_version": 3,
            "message_type": "command_response",
            "command_id": "a" * 32,
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        items = [
            {"name": "session_abc123.json", "path": "disk:/bus/to_vps/session_abc123.json"},
            {"name": f"response_{'a' * 32}.json", "path": "disk:/bus/to_vps/response.json"},
        ]
        payloads = [json.dumps(session).encode(), json.dumps(response).encode()]
        session_queue = asyncio.Queue()
        response_queue = asyncio.Queue()
        yadisk_poll._state = {"handled_messages": []}

        with patch("yadisk_poll._connect", AsyncMock(return_value=True)), \
             patch("yadisk_poll._list_inbox", AsyncMock(return_value=items)), \
             patch("yadisk_poll._download_bytes", AsyncMock(side_effect=payloads)):
            await yadisk_poll._poll_once(session_queue, response_queue)

        self.assertEqual(session_queue.qsize(), 1)
        self.assertEqual(response_queue.qsize(), 1)
        self.assertEqual((await session_queue.get())[2], "session_ready")
        self.assertEqual((await response_queue.get())[2], "command_response")

    def test_poll_interval_is_ten_seconds(self):
        self.assertEqual(yadisk_poll.POLL_INTERVAL, 10)


if __name__ == "__main__":
    unittest.main()
