import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import admin_command_service
import admin_commands
import admin_notifications
import app
import control_response_service
import event_access
import telegram_api
import yadisk_control
import yadisk_poll
from messaging import ReplyTarget


class ResponseValidationTests(unittest.TestCase):
    def test_validates_response_and_artifact_path(self):
        command_id = "a" * 32
        response = yadisk_control.validate_response({
            "schema_version": 3,
            "message_type": "command_response",
            "command_id": command_id,
            "command": "send_logs",
            "status": "ok",
            "message": "done",
            "artifact_path": "/photobooth_system/control/logs/test.log",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": 123,
            },
        }, f"response_{command_id}.json")
        self.assertEqual(response["status"], "ok")
        self.assertEqual(
            response["reply_target"],
            ReplyTarget("telegram", 123),
        )

        with self.assertRaisesRegex(ValueError, "artifact"):
            yadisk_control.validate_response({
                **response,
                "artifact_path": "/control/../secret",
            })

    def test_rejects_response_without_reply_target(self):
        command_id = "b" * 32
        with self.assertRaisesRegex(ValueError, "reply_target"):
            yadisk_control.validate_response({
                "schema_version": 3,
                "message_type": "command_response",
                "command_id": command_id,
                "command": "status",
                "status": "ok",
            }, f"response_{command_id}.json")

    def test_rejects_previous_control_schema(self):
        command_id = "c" * 32
        with self.assertRaisesRegex(ValueError, "schema"):
            yadisk_control.validate_response({
                "schema_version": 2,
                "message_type": "command_response",
                "command_id": command_id,
                "command": "status",
                "status": "ok",
                "reply_target": {
                    "provider": "telegram",
                    "conversation_id": "123",
                },
            }, f"response_{command_id}.json")


class SendCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_one_command_json(self):
        uploads = []

        async def upload(payload, path):
            uploads.append((json.loads(payload), path))

        with patch("yadisk_control._connect", AsyncMock(return_value=True)), \
             patch("yadisk_control._upload_bytes", side_effect=upload), \
             patch.object(yadisk_control, "_root", "/photobooth_system/control"), \
             patch("yadisk_control.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = "a" * 32
            command = await yadisk_control.send_command(
                "set_event",
                ReplyTarget("telegram", 123),
                {"name": "Свадьба"},
            )

        self.assertEqual(command["command_id"], "a" * 32)
        self.assertEqual(uploads[0][0]["schema_version"], 3)
        self.assertEqual(uploads[0][0]["message_type"], "command")
        self.assertEqual(uploads[0][0]["data"], {"name": "Свадьба"})
        self.assertEqual(
            uploads[0][0]["reply_target"],
            {
                "provider": "telegram",
                "conversation_id": "123",
            },
        )
        self.assertEqual(
            uploads[0][1],
            f"/photobooth_system/control/to_booth/{'a' * 32}.json",
        )


class EventSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_to_one_active_folder(self):
        yadisk_poll._folder = "/old_event"
        with patch("yadisk_poll._connect", AsyncMock(return_value=True)), \
             patch("yadisk_poll._ensure_directory", AsyncMock(return_value=True)), \
             patch("yadisk_poll._state_save"):
            await yadisk_poll.set_event_folder("Свадьба Ивановых 2026")

        self.assertEqual(yadisk_poll._folder, "/Свадьба Ивановых 2026")

    async def test_event_qr_card_is_delivered_to_both_admin_channels(self):
        response = {
            "status": "ok",
            "command": "set_event",
            "message": "Event активирован: <b>legacy markup</b>",
            "event_folder": "2026-08-17 Свадьба Ивановых",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        links = {
            "telegram": "https://t.me/bot?start=token",
            "vk": "https://vk.me/community?ref=token",
        }
        delivery = admin_notifications.EventAccessDelivery(
            primary_delivered=True,
            delivered_targets=(ReplyTarget("telegram", 123),),
            failed_targets=(),
        )
        with patch(
            "control_response_service.yadisk_poll.set_event_folder",
            AsyncMock(),
        ), patch(
            "control_response_service.runtime_config.save_event",
        ), patch(
            "control_response_service.yadisk_poll.publish_current_folder",
            AsyncMock(return_value="https://disk.example/event"),
        ) as publish, patch(
            "control_response_service.event_access.guest_links",
            return_value=links,
        ), patch(
            "control_response_service.event_access.guest_qr_sheet_png",
            return_value=b"qr-card",
        ), patch(
            "control_response_service.admin_notifications.send_event_update",
            AsyncMock(return_value=delivery),
        ) as send:
            handled = await control_response_service.handle(response)

        self.assertTrue(handled)
        send.assert_awaited_once()
        self.assertEqual(
            send.await_args.args[0],
            ReplyTarget("telegram", 123),
        )
        self.assertEqual(send.await_args.args[1], b"qr-card")
        publish.assert_awaited_once_with()
        plain_caption = send.await_args.args[2]
        telegram_caption = send.await_args.kwargs["telegram_caption"]
        self.assertNotIn("<b>", plain_caption)
        self.assertIn("Event активирован на будке", plain_caption)
        self.assertIn(links["telegram"], plain_caption)
        self.assertIn(links["vk"], plain_caption)
        self.assertIn(
            "<b>2026-08-17 Свадьба Ивановых</b>",
            telegram_caption,
        )

    async def test_cafe_event_text_is_delivered_to_both_admin_channels(self):
        response = {
            "status": "ok",
            "command": "set_event",
            "message": "Event активирован на будке: <b>Кафе</b>",
            "event_folder": "Кафе",
            "start_locked": True,
            "unlock_sessions_remaining": 0,
            "reply_target": {
                "provider": "vk",
                "conversation_id": "556972284",
            },
        }
        delivery = admin_notifications.EventAccessDelivery(
            primary_delivered=True,
            delivered_targets=(ReplyTarget("vk", 556972284),),
            failed_targets=(),
        )
        with patch(
            "control_response_service.yadisk_poll.set_event_folder",
            AsyncMock(),
        ), patch(
            "control_response_service.runtime_config.save_event",
        ), patch(
            "control_response_service.yadisk_poll.publish_current_folder",
            AsyncMock(return_value="https://disk.example/cafe"),
        ) as publish, patch(
            "control_response_service.event_access.guest_links",
        ) as guest_links, patch(
            "control_response_service.admin_notifications.send_event_update",
            AsyncMock(return_value=delivery),
        ) as send:
            handled = await control_response_service.handle(response)

        self.assertTrue(handled)
        publish.assert_not_awaited()
        guest_links.assert_not_called()
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], ReplyTarget("vk", 556972284))
        self.assertIsNone(send.await_args.args[1])
        plain_caption = send.await_args.args[2]
        telegram_caption = send.await_args.kwargs["telegram_caption"]
        self.assertEqual(
            plain_caption,
            "✅ Event активирован на будке: Кафе\n\n"
            "🔒 Запуск заблокирован. Разрешённых фотосессий: 0.",
        )
        self.assertIn("<b>Кафе</b>", telegram_caption)
        self.assertNotIn("<b>", plain_caption)
        self.assertNotIn("Публичная папка", plain_caption)

    def test_event_command_requires_iso_date_except_cafe(self):
        with patch.object(
            event_access,
            "EVENT_KEY",
            "test-event-key",
        ), patch.object(
            event_access.telegram_api,
            "BOT_USERNAME",
            "photobooth_bot",
        ), patch.object(
            event_access.vk_api,
            "GROUP_USERNAME",
            "photobooth_vk",
        ):
            self.assertEqual(
                admin_commands.parse(
                    "/event   2026-08-17   Свадьба   Ивановых  "
                ),
                ("set_event", {"name": "2026-08-17 Свадьба Ивановых"}),
            )
        self.assertEqual(
            admin_commands.parse("/event Кафе"),
            ("set_event", {"name": "Кафе"}),
        )
        for command in (
            "/event Свадьба Ивановых",
            "/event 2026-02-31 Свадьба Ивановых",
            "/event 2026-08-17",
            "/event ../bad",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                admin_commands.parse(command)

    def test_event_access_token_is_stable_url_safe_and_fixed_length(self):
        with patch.object(event_access, "EVENT_KEY", "test-event-key"):
            token = event_access.access_token(
                "  2026-08-17   СВАДЬБА Ивановых  ")
            self.assertEqual(
                token,
                event_access.access_token("2026-08-17 свадьба ивановых"),
            )

        self.assertEqual(len(token), 12)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{12}$")


class CafeUnblockCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_default_and_explicit_session_count(self):
        self.assertEqual(
            admin_commands.parse("/unblock"),
            ("unblock", {"sessions": 1}),
        )
        self.assertEqual(
            admin_commands.parse("/unblock 25"),
            ("unblock", {"sessions": 25}),
        )
        self.assertEqual(
            admin_commands.parse("/unblock@photobooth_bot 1000"),
            ("unblock", {"sessions": 1000}),
        )

    def test_block_and_unblock_zero_parse_to_same_booth_command(self):
        expected = ("unblock", {"sessions": 0})
        self.assertEqual(admin_commands.parse("/block"), expected)
        self.assertEqual(
            admin_commands.parse("/block@photobooth_bot"), expected)
        self.assertEqual(admin_commands.parse("/unblock 0"), expected)

    def test_rejects_invalid_session_count(self):
        for command in (
            "/unblock 1001",
            "/unblock -1",
            "/unblock 1.5",
            "/unblock many",
            "/unblock 2 extra",
            "/block 1",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                admin_commands.parse(command)

    def test_block_is_registered_in_command_map_and_help(self):
        self.assertEqual(admin_commands.KNOWN_COMMANDS["/block"], "unblock")
        self.assertIn("/block", admin_commands.HELP_MESSAGE)

    async def test_forwards_session_count_to_booth(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/unblock 7",
            )

        send.assert_awaited_once_with(
            "unblock",
            ReplyTarget("telegram", 123),
            {"sessions": 7},
        )
        self.assertIn("7", send_text.await_args.args[1])
        self.assertIn("подтверждение будки", send_text.await_args.args[1])

    async def test_without_count_forwards_one_session(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ):
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/unblock",
            )

        send.assert_awaited_once_with(
            "unblock",
            ReplyTarget("telegram", 123),
            {"sessions": 1},
        )

    async def test_block_forwards_zero_with_lock_message(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/block",
            )

        send.assert_awaited_once_with(
            "unblock",
            ReplyTarget("telegram", 123),
            {"sessions": 0},
        )
        self.assertIn("блокирую", send_text.await_args.args[1])
        self.assertIn("подтверждение будки", send_text.await_args.args[1])

    async def test_unblock_zero_forwards_zero(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ):
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/unblock 0",
            )

        send.assert_awaited_once_with(
            "unblock",
            ReplyTarget("telegram", 123),
            {"sessions": 0},
        )

    async def test_invalid_count_is_reported_without_disk_command(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            new_callable=AsyncMock,
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/unblock 1001",
            )

        send.assert_not_awaited()
        self.assertIn("от 0 до 1000", send_text.await_args.args[1])


class CameraSettingCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_dynamic_camera_setting(self):
        self.assertEqual(
            admin_commands.parse("/iso 200"),
            ("set_camera_config", {"field": "iso", "value": "200"}),
        )
        self.assertEqual(
            admin_commands.parse(
                "/white_balance@photobooth_bot AUTO"),
            (
                "set_camera_config",
                {"field": "white_balance", "value": "AUTO"},
            ),
        )

    def test_does_not_intercept_reserved_or_plain_commands(self):
        self.assertEqual(admin_commands.parse("/status"), ("status", None))
        self.assertEqual(
            admin_commands.parse("/get_config"), ("get_config", None))
        with self.assertRaises(ValueError):
            admin_commands.parse("/event Wedding")
        self.assertIsNone(admin_commands.parse("plain text"))

    def test_requires_value_for_dynamic_camera_setting(self):
        with self.assertRaisesRegex(ValueError, "Использование"):
            admin_commands.parse("/continuous_af")

    async def test_forwards_raw_value_to_booth(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/iso auto",
            )

        send.assert_awaited_once_with(
            "set_camera_config",
            ReplyTarget("telegram", 123),
            {"field": "iso", "value": "auto"},
        )
        self.assertIn("ожидаю подтверждение", send_text.await_args.args[1])

    async def test_missing_value_returns_usage_without_disk_command(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            new_callable=AsyncMock,
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/continuous_af",
            )

        send.assert_not_awaited()
        self.assertIn("Использование", send_text.await_args.args[1])

    async def test_get_config_is_forwarded_as_fixed_command(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/get_config",
            )

        send.assert_awaited_once_with(
            "get_config",
            ReplyTarget("telegram", 123),
            None,
        )
        self.assertEqual(
            send_text.await_args.args[1],
            "⏳ Запрашиваю конфиги фотобудки...",
        )


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_uses_runtime_folder_by_default(self):
        with patch(
            "admin_command_service.runtime_config.updates_folder",
            return_value="configured-updates",
        ), patch(
            "admin_command_service.vps_update.publish_latest_release",
            AsyncMock(return_value="published"),
        ) as do_update, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ):
            await admin_command_service.handle_message(
                ReplyTarget("vk", 556972284),
                "/update",
            )

        self.assertEqual(do_update.await_args.args[0], "configured-updates")

    async def test_update_does_not_restart_automatically(self):
        async def update(_updates_folder, progress_callback):
            await progress_callback("retry notice")
            return "published"

        with patch(
            "admin_command_service.vps_update.publish_latest_release",
            AsyncMock(side_effect=update),
        ) as do_update, \
             patch("admin_command_service.yadisk_control.send_command",
                   AsyncMock(return_value="a" * 32)) as send, \
             patch("admin_command_service.messenger_delivery.send_text",
                   AsyncMock(return_value=True)) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/update",
                updates_folder="test-updates",
            )

        do_update.assert_awaited_once()
        self.assertEqual(do_update.await_args.args[0], "test-updates")
        send.assert_not_awaited()
        self.assertEqual(
            [item.args[1] for item in send_text.await_args_list],
            ["⏳ Скачиваю полный релиз...", "retry notice", "published"],
        )


class ProviderNeutralAdminCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_vk_target_is_preserved_for_booth_response(self):
        target = ReplyTarget("vk", 556972284)
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value={"command_id": "a" * 32}),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as reply:
            await admin_command_service.handle_message(target, "/status")

        send.assert_awaited_once_with("status", target, None)
        self.assertEqual(reply.await_args.args[0], target)

    async def test_failed_acknowledgement_cannot_repeat_a_durable_command(self):
        target = ReplyTarget("vk", 556972284)
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value={"command_id": "a" * 32}),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(side_effect=RuntimeError("VK unavailable")),
        ) as reply, patch.object(
            admin_command_service.log,
            "warning",
        ) as warning:
            await admin_command_service.handle_message(target, "/status")

        send.assert_awaited_once_with("status", target, None)
        reply.assert_awaited_once()
        warning.assert_called_once()


class LogDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_integer_chat_id_is_serialized_as_multipart_text(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return b'{"ok":true,"result":{"message_id":1}}'

        class Telegram:
            def post(self, *_args, **_kwargs):
                return Response()

        class ClientSession:
            async def __aenter__(self):
                return Telegram()

            async def __aexit__(self, *_args):
                return False

        form = MagicMock()
        with patch.object(telegram_api, "BOT_TOKEN", "token"), \
             patch("telegram_api.aiohttp.FormData", return_value=form), \
             patch("telegram_api.aiohttp.ClientSession",
                   return_value=ClientSession()):
            self.assertTrue(await telegram_api.send_document(
                5683598562,
                b"log",
                "photobooth.log",
                "text/plain",
            ))

        form.add_field.assert_any_call("chat_id", "5683598562")

    async def test_successful_document_is_not_retried_for_cleanup_or_text(self):
        response = {
            "status": "ok",
            "command": "send_logs",
            "message": "Лог загружен",
            "artifact_path": "/control/logs/test.log",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "5683598562",
            },
        }
        with patch("control_response_service.yadisk_control.download_bytes",
                   AsyncMock(return_value=b"log")), \
             patch("control_response_service.messenger_delivery.send_document",
                   AsyncMock(return_value=True)) as send_document, \
             patch("control_response_service.messenger_delivery.send_text",
                   new_callable=AsyncMock) as send_text, \
             patch("control_response_service.yadisk_control.delete_resource", AsyncMock(
                 side_effect=RuntimeError("cleanup unavailable"))) as delete:
            self.assertTrue(await control_response_service.handle(response))

        send_document.assert_awaited_once_with(
            ReplyTarget("telegram", 5683598562),
            b"log",
            "photobooth.log",
            "text/plain",
        )
        send_text.assert_not_awaited()
        delete.assert_awaited_once_with("/control/logs/test.log")

    async def test_log_response_can_be_delivered_to_vk(self):
        response = {
            "status": "ok",
            "command": "send_logs",
            "message": "Лог загружен",
            "artifact_path": "/control/logs/test.log",
            "reply_target": {
                "provider": "vk",
                "conversation_id": "556972284",
            },
        }
        with patch(
            "control_response_service.yadisk_control.download_bytes",
            AsyncMock(return_value=b"log"),
        ), patch(
            "control_response_service.messenger_delivery.send_document",
            AsyncMock(return_value=True),
        ) as send, patch(
            "control_response_service.yadisk_control.delete_resource",
            AsyncMock(return_value=True),
        ) as delete:
            self.assertTrue(await control_response_service.handle(response))

        send.assert_awaited_once_with(
            ReplyTarget("vk", 556972284),
            b"log",
            "photobooth.log",
            "text/plain",
        )
        delete.assert_awaited_once_with("/control/logs/test.log")


class PrintCommandResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_updates_queue_without_duplicate_user_message(self):
        command_id = "a" * 32
        response = {
            "status": "ok",
            "command": "print_image",
            "command_id": command_id,
            "message": "Ваше фото добавлено в очередь",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        queued = {
            "outcome": "queued",
            "status": "queued",
            "command_id": command_id,
        }
        with patch.object(
            control_response_service.database,
            "mark_print_job_queued",
            AsyncMock(return_value=queued),
        ) as mark_queued, patch(
            "control_response_service.messenger_delivery.send_text",
            new_callable=AsyncMock,
        ) as send_text:
            self.assertTrue(await control_response_service.handle(response))

        mark_queued.assert_awaited_once_with(command_id=command_id)
        send_text.assert_not_awaited()

    async def test_booth_error_is_still_reported_to_user(self):
        command_id = "b" * 32
        response = {
            "status": "error",
            "command": "print_image",
            "command_id": command_id,
            "message": "Принтер не готов",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        failed = {
            "outcome": "failed",
            "status": "failed",
            "command_id": command_id,
        }
        with patch.object(
            control_response_service.database,
            "mark_print_job_failed",
            AsyncMock(return_value=failed),
        ) as mark_failed, patch(
            "control_response_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            self.assertTrue(await control_response_service.handle(response))

        mark_failed.assert_awaited_once_with(
            command_id=command_id,
            last_error="Принтер не готов",
        )
        send_text.assert_awaited_once_with(
            ReplyTarget("telegram", 123),
            "❌ Принтер не готов",
        )


class ConfigDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_combined_config_as_plain_text_document(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return b'{"ok":true,"result":{"message_id":1}}'

        class Telegram:
            def post(self, *_args, **_kwargs):
                return Response()

        class ClientSession:
            async def __aenter__(self):
                return Telegram()

            async def __aexit__(self, *_args):
                return False

        form = MagicMock()
        with patch.object(telegram_api, "BOT_TOKEN", "token"), \
             patch("telegram_api.aiohttp.FormData", return_value=form), \
             patch("telegram_api.aiohttp.ClientSession",
                   return_value=ClientSession()):
            self.assertTrue(await telegram_api.send_document(
                123,
                b"combined configs",
                "photobooth_configs.txt",
                "text/plain; charset=utf-8",
            ))

        form.add_field.assert_any_call("chat_id", "123")
        form.add_field.assert_any_call(
            "document",
            b"combined configs",
            filename="photobooth_configs.txt",
            content_type="text/plain; charset=utf-8",
        )

    async def test_control_response_delivers_configs_and_cleans_export(self):
        response = {
            "status": "ok",
            "command": "get_config",
            "message": "Конфиги готовы",
            "artifact_path": "/control/configs/test.txt",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        export = b"===== config_app.json =====\n{}\n"
        vps_config = b'{"yadisk_folder":"event"}\n'
        with patch("control_response_service.runtime_config.read_bytes",
                   return_value=vps_config), \
             patch("control_response_service.yadisk_control.download_bytes",
                   AsyncMock(return_value=export)), \
             patch("control_response_service.messenger_delivery.send_documents",
                   AsyncMock(return_value=True)) as send, \
             patch("control_response_service.messenger_delivery.send_document",
                   new_callable=AsyncMock) as send_document, \
             patch("control_response_service.yadisk_control.delete_resource",
                   AsyncMock(return_value=True)) as delete:
            self.assertTrue(await control_response_service.handle(response))

        send.assert_awaited_once_with(
            ReplyTarget("telegram", 123),
            [
                (
                    export,
                    "photobooth_configs.txt",
                    "text/plain; charset=utf-8",
                ),
                (
                    vps_config,
                    "config_vps.json",
                    "application/json",
                ),
            ],
        )
        send_document.assert_not_awaited()
        delete.assert_awaited_once_with("/control/configs/test.txt")


if __name__ == "__main__":
    unittest.main()
