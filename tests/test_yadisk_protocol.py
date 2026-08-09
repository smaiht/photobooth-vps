import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

import delivery_retry
import telegram_api
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
        yadisk_poll._retry_after.clear()
        yadisk_poll._failures.clear()

    def tearDown(self):
        yadisk_poll._inflight.clear()
        yadisk_poll._retry_after.clear()
        yadisk_poll._failures.clear()

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

    def test_poll_interval_matches_configured_value(self):
        self.assertEqual(yadisk_poll.POLL_INTERVAL, 5)

    async def test_failed_message_is_deferred_instead_of_retried_each_poll(self):
        manifest = {
            "schema_version": 2,
            "message_type": "session_ready",
            "event_folder": "event",
            "session_id": "abc123",
            "files": [{
                "name": "photo.jpg", "kind": "photo", "size": 5, "md5": None,
            }],
        }
        item = {
            "name": "session_abc123.json",
            "path": "disk:/bus/to_vps/session_abc123.json",
        }
        session_queue = asyncio.Queue()
        response_queue = asyncio.Queue()
        yadisk_poll._state = {"handled_messages": []}
        download = AsyncMock(return_value=json.dumps(manifest).encode())

        with patch("yadisk_poll._connect", AsyncMock(return_value=True)), \
             patch("yadisk_poll._list_inbox", AsyncMock(return_value=[item])), \
             patch("yadisk_poll._download_bytes", download):
            await yadisk_poll._poll_once(session_queue, response_queue)
            self.assertEqual(session_queue.qsize(), 1)
            # Simulate the worker failing to deliver this message.
            queued = await session_queue.get()
            yadisk_poll._inflight.discard(queued[0]["name"])
            yadisk_poll._note_failure(queued[0]["name"])

            await yadisk_poll._poll_once(session_queue, response_queue)

        self.assertEqual(session_queue.qsize(), 0)
        self.assertEqual(download.await_count, 1)

    async def test_deferred_message_runs_again_after_backoff_expires(self):
        manifest = {
            "schema_version": 2,
            "message_type": "session_ready",
            "event_folder": "event",
            "session_id": "abc123",
            "files": [{
                "name": "photo.jpg", "kind": "photo", "size": 5, "md5": None,
            }],
        }
        item = {
            "name": "session_abc123.json",
            "path": "disk:/bus/to_vps/session_abc123.json",
        }
        session_queue = asyncio.Queue()
        response_queue = asyncio.Queue()
        yadisk_poll._state = {"handled_messages": []}
        yadisk_poll._note_failure(item["name"])
        yadisk_poll._retry_after[item["name"]] = time.monotonic() - 1

        with patch("yadisk_poll._connect", AsyncMock(return_value=True)), \
             patch("yadisk_poll._list_inbox", AsyncMock(return_value=[item])), \
             patch("yadisk_poll._download_bytes", AsyncMock(
                 return_value=json.dumps(manifest).encode())):
            await yadisk_poll._poll_once(session_queue, response_queue)

        self.assertEqual(session_queue.qsize(), 1)

    def test_backoff_delay_grows_and_stays_bounded(self):
        name = "session_abc123.json"
        delays = []
        for _ in range(6):
            before = time.monotonic()
            yadisk_poll._note_failure(name)
            delays.append(round(yadisk_poll._retry_after[name] - before))
        self.assertEqual(delays[:4], list(yadisk_poll.RETRY_BACKOFF_SECONDS))
        self.assertEqual(delays[4], yadisk_poll.RETRY_BACKOFF_SECONDS[-1])
        yadisk_poll._note_success(name)
        self.assertNotIn(name, yadisk_poll._retry_after)
        self.assertNotIn(name, yadisk_poll._failures)

    async def test_state_of_removed_message_is_forgotten(self):
        yadisk_poll._note_failure("gone.json")
        session_queue = asyncio.Queue()
        response_queue = asyncio.Queue()

        with patch("yadisk_poll._connect", AsyncMock(return_value=True)), \
             patch("yadisk_poll._list_inbox", AsyncMock(return_value=[])):
            await yadisk_poll._poll_once(session_queue, response_queue)

        self.assertNotIn("gone.json", yadisk_poll._retry_after)
        self.assertNotIn("gone.json", yadisk_poll._failures)


class PhotoSizeLimitTests(unittest.IsolatedAsyncioTestCase):
    """Telegram rejects a photo over 10 MiB with a permanent HTTP 400."""

    @staticmethod
    def _noisy_jpeg(path: Path, size: tuple[int, int]) -> None:
        # Random pixels defeat JPEG compression, so the file is genuinely large.
        noise = os.urandom(size[0] * size[1] * 3)
        image = Image.frombytes("RGB", size, noise)
        try:
            image.save(path, "JPEG", quality=100, subsampling=0)
        finally:
            image.close()

    async def test_oversized_photo_is_recompressed_under_the_limit(self):
        with TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "photo.jpg"
            self._noisy_jpeg(photo, (3000, 2400))
            self.assertGreater(
                photo.stat().st_size,
                telegram_session_delivery.PHOTO_SIZE_LIMIT,
            )

            prepared = await telegram_session_delivery._prepare_files(
                [(photo, "photo")],
            )

            self.assertIsNotNone(prepared)
            sent_path, kind = prepared[0]
            self.assertEqual(kind, "photo")
            self.assertNotEqual(sent_path, photo)
            self.assertLessEqual(
                sent_path.stat().st_size,
                telegram_session_delivery.PHOTO_SIZE_LIMIT,
            )
            with Image.open(sent_path) as encoded:
                self.assertEqual(encoded.format, "JPEG")
            # The camera original stays untouched on disk.
            self.assertGreater(
                photo.stat().st_size,
                telegram_session_delivery.PHOTO_SIZE_LIMIT,
            )

    async def test_small_photo_and_video_are_passed_through_unchanged(self):
        with TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "photo.jpg"
            Image.new("RGB", (64, 64), "navy").save(photo, "JPEG")
            video = Path(tmpdir) / "clip.mp4"
            video.write_bytes(b"mp4")

            prepared = await telegram_session_delivery._prepare_files(
                [(photo, "photo"), (video, "video")],
            )

            self.assertEqual(prepared, [(photo, "photo"), (video, "video")])

    async def test_hopeless_photo_fails_without_uploading(self):
        with TemporaryDirectory() as tmpdir:
            broken = Path(tmpdir) / "photo.jpg"
            broken.write_bytes(b"not an image")

            with patch(
                "telegram_session_delivery._post",
                AsyncMock(return_value=True),
            ) as post, \
                 patch.object(telegram_api, "BOT_TOKEN", "token"), \
                 patch.object(telegram_api, "ARCHIVE_CHAT_ID", "1"), \
                 patch.object(
                     telegram_session_delivery,
                     "PHOTO_SIZE_LIMIT",
                     4,
                 ):
                delivered = await telegram_session_delivery.send_session(
                    [(broken, "photo")],
                )

            self.assertFalse(delivered)
            post.assert_not_awaited()

    async def test_send_session_uploads_the_recompressed_copy(self):
        with TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "photo.jpg"
            self._noisy_jpeg(photo, (3000, 2400))

            with patch(
                "telegram_session_delivery._post",
                AsyncMock(return_value=True),
            ) as post, \
                 patch.object(telegram_api, "BOT_TOKEN", "token"), \
                 patch.object(telegram_api, "ARCHIVE_CHAT_ID", "1"):
                self.assertTrue(
                    await telegram_session_delivery.send_session(
                        [(photo, "photo")],
                    )
                )

            uploaded = post.await_args.args[1][0][0]
            self.assertLessEqual(
                uploaded.stat().st_size,
                telegram_session_delivery.PHOTO_SIZE_LIMIT,
            )

    def test_error_description_surfaces_the_provider_reason(self):
        body = json.dumps({
            "ok": False,
            "error_code": 400,
            "description": (
                "Bad Request: file of size 11079028 bytes is too big for a "
                "photo; the maximum size is 10485760 bytes"
            ),
        }).encode()

        self.assertIn(
            "too big for a photo",
            delivery_retry.error_description(body),
        )
        self.assertEqual(delivery_retry.error_description(b""), "")
        self.assertEqual(delivery_retry.error_description(b"<html>"), "<html>")


if __name__ == "__main__":
    unittest.main()
