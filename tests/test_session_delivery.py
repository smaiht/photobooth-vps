import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import session_delivery
import vk_api
import vk_session_delivery
import yadisk_poll


class SessionDeliveryRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_keeps_provider_adapters_separate(self):
        files = [(Path("photo.jpg"), "photo")]
        with patch(
            "session_delivery.telegram_session_delivery.send_session",
            AsyncMock(return_value=True),
        ) as telegram, patch(
            "session_delivery.vk_session_delivery.send_session",
            AsyncMock(return_value=True),
        ) as vk:
            self.assertTrue(await session_delivery.send_session(
                "telegram", files, "https://disk.yandex.ru/d/test",
            ))
            self.assertTrue(await session_delivery.send_session(
                "vk", files, "https://disk.yandex.ru/d/test",
            ))

        telegram.assert_awaited_once_with(
            files, "https://disk.yandex.ru/d/test",
        )
        vk.assert_awaited_once_with(files, "https://disk.yandex.ru/d/test")


class VkSessionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_attachment_sender_preserves_caption_and_order(self):
        with patch(
            "vk_api._send_message",
            AsyncMock(return_value=77),
        ) as send:
            message_id = await vk_api.send_attachments(
                object(),
                123,
                ["photo-1_10", "doc-1_11"],
                "Оригиналы: link",
            )

        self.assertEqual(message_id, 77)
        params = send.await_args.args[1]
        self.assertEqual(params["peer_id"], 123)
        self.assertEqual(params["attachment"], "photo-1_10,doc-1_11")
        self.assertEqual(params["message"], "Оригиналы: link")
        self.assertGreater(params["random_id"], 0)

    async def test_photos_and_video_use_their_supported_vk_uploads(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            photo = root / "photo.jpg"
            layout = root / "print.png"
            video = root / "session.mp4"
            photo.write_bytes(b"photo")
            layout.write_bytes(b"layout")
            video.write_bytes(b"video")
            files = [
                (photo, "photo"),
                (layout, "print"),
                (video, "video"),
            ]

            with patch.object(vk_api, "BOT_TOKEN", "token"), patch.object(
                vk_api, "ARCHIVE_CHAT_ID", "123",
            ), patch(
                "vk_session_delivery.vk_api.upload_message_photo",
                AsyncMock(side_effect=["photo1_1", "photo1_2"]),
            ) as upload_photo, patch(
                "vk_session_delivery.vk_api.upload_message_document",
                AsyncMock(return_value="doc1_3"),
            ) as upload_document, patch(
                "vk_session_delivery.vk_api.send_attachments",
                AsyncMock(return_value=1),
            ) as send:
                delivered = await vk_session_delivery.send_session(
                    files,
                    "https://disk.yandex.ru/d/test",
                )

        self.assertTrue(delivered)
        self.assertEqual(upload_photo.await_count, 2)
        self.assertEqual(
            upload_photo.await_args_list[0].kwargs["content_type"],
            "image/jpeg",
        )
        self.assertEqual(
            upload_photo.await_args_list[1].kwargs["content_type"],
            "image/png",
        )
        upload_document.assert_awaited_once()
        self.assertEqual(
            upload_document.await_args.kwargs["content_type"],
            "video/mp4",
        )
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1:3], (123, [
            "photo1_1", "photo1_2", "doc1_3",
        ]))
        self.assertEqual(
            send.await_args.args[3],
            "Оригиналы: https://disk.yandex.ru/d/test",
        )

    async def test_more_than_ten_files_are_split_and_captioned_once(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            files = []
            for index in range(12):
                path = root / f"photo_{index}.jpg"
                path.write_bytes(b"photo")
                files.append((path, "photo"))

            with patch.object(vk_api, "BOT_TOKEN", "token"), patch.object(
                vk_api, "ARCHIVE_CHAT_ID", "123",
            ), patch(
                "vk_session_delivery.vk_api.upload_message_photo",
                AsyncMock(side_effect=[f"photo1_{index}" for index in range(12)]),
            ), patch(
                "vk_session_delivery.vk_api.send_attachments",
                AsyncMock(return_value=1),
            ) as send:
                delivered = await vk_session_delivery.send_session(
                    files,
                    "https://disk.yandex.ru/d/test",
                )

        self.assertTrue(delivered)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(len(send.await_args_list[0].args[2]), 10)
        self.assertEqual(len(send.await_args_list[1].args[2]), 2)
        self.assertEqual(
            send.await_args_list[0].args[3],
            "Оригиналы: https://disk.yandex.ru/d/test",
        )
        self.assertEqual(send.await_args_list[1].args[3], "")

    async def test_missing_vk_credentials_fail_without_opening_a_session(self):
        with patch.object(vk_api, "BOT_TOKEN", ""), patch.object(
            vk_api, "ARCHIVE_CHAT_ID", "",
        ), patch("vk_session_delivery.aiohttp.ClientSession") as client:
            delivered = await vk_session_delivery.send_session([])

        self.assertFalse(delivered)
        client.assert_not_called()


class DurableProviderProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        yadisk_poll._state = {
            "handled_messages": [],
            "session_deliveries": {},
        }

    def tearDown(self):
        yadisk_poll._state = {
            "handled_messages": [],
            "session_deliveries": {},
        }

    @staticmethod
    def manifest() -> dict:
        return {
            "schema_version": 2,
            "message_type": "session_ready",
            "event_folder": "event",
            "session_id": "abc123",
            "public_url": "https://disk.yandex.ru/d/test",
            "files": [{
                "name": "photo.jpg",
                "kind": "photo",
                "size": 5,
                "md5": None,
            }],
        }

    async def test_retry_sends_only_the_provider_that_failed(self):
        item = {
            "name": "session_abc123.json",
            "path": "disk:/control/to_vps/session_abc123.json",
        }

        async def download(_remote, local_path, _entry):
            Path(local_path).write_bytes(b"photo")

        first_send = AsyncMock(
            side_effect=lambda provider, *_args: provider == "telegram",
        )
        with patch(
            "yadisk_poll.session_delivery.enabled_providers",
            return_value=("telegram", "vk"),
        ), patch(
            "yadisk_poll._download_file",
            AsyncMock(side_effect=download),
        ), patch(
            "yadisk_poll.session_delivery.send_session",
            first_send,
        ), patch(
            "yadisk_poll._state_save",
        ), patch(
            "yadisk_poll._delete_inbox_message",
            AsyncMock(return_value=True),
        ) as delete:
            delivered = await yadisk_poll._process_manifest(
                item, self.manifest(),
            )

        self.assertFalse(delivered)
        self.assertEqual(
            [call.args[0] for call in first_send.await_args_list],
            ["telegram", "vk"],
        )
        self.assertEqual(
            yadisk_poll._state["session_deliveries"][item["name"]],
            ["telegram"],
        )
        delete.assert_not_awaited()

        second_send = AsyncMock(return_value=True)
        with patch(
            "yadisk_poll.session_delivery.enabled_providers",
            return_value=("telegram", "vk"),
        ), patch(
            "yadisk_poll._download_file",
            AsyncMock(side_effect=download),
        ), patch(
            "yadisk_poll.session_delivery.send_session",
            second_send,
        ), patch(
            "yadisk_poll._state_save",
        ), patch(
            "yadisk_poll._delete_inbox_message",
            AsyncMock(return_value=True),
        ):
            delivered = await yadisk_poll._process_manifest(
                item, self.manifest(),
            )

        self.assertTrue(delivered)
        self.assertEqual(
            [call.args[0] for call in second_send.await_args_list],
            ["vk"],
        )
        self.assertEqual(yadisk_poll._state["handled_messages"], [])
        self.assertEqual(yadisk_poll._state["session_deliveries"], {})

    async def test_both_disabled_acknowledges_without_downloading(self):
        item = {"name": "session_abc123.json"}
        with patch(
            "yadisk_poll.session_delivery.enabled_providers",
            return_value=(),
        ), patch(
            "yadisk_poll._download_file",
            AsyncMock(),
        ) as download, patch(
            "yadisk_poll._state_save",
        ), patch(
            "yadisk_poll._delete_inbox_message",
            AsyncMock(return_value=True),
        ):
            delivered = await yadisk_poll._process_manifest(
                item, self.manifest(),
            )

        self.assertTrue(delivered)
        download.assert_not_awaited()

    def test_state_loader_preserves_only_known_provider_progress(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({
                "handled_messages": ["handled.json"],
                "session_deliveries": {
                    "one.json": ["telegram", "unknown", "telegram"],
                    "two.json": "vk",
                },
            }), encoding="utf-8")
            with patch.object(yadisk_poll, "STATE_FILE", state_file):
                yadisk_poll._state_load()

        self.assertEqual(
            yadisk_poll._state,
            {
                "handled_messages": ["handled.json"],
                "session_deliveries": {"one.json": ["telegram"]},
            },
        )


if __name__ == "__main__":
    unittest.main()
