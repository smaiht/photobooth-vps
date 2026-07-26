import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app
import yadisk_control
import yadisk_poll


class ResponseValidationTests(unittest.TestCase):
    def test_validates_response_and_artifact_path(self):
        command_id = "a" * 32
        response = yadisk_control.validate_response({
            "schema_version": 2,
            "message_type": "command_response",
            "command_id": command_id,
            "command": "send_logs",
            "status": "ok",
            "message": "done",
            "artifact_path": "/photobooth_system/control/logs/test.log",
        }, f"response_{command_id}.json")
        self.assertEqual(response["status"], "ok")

        with self.assertRaisesRegex(ValueError, "artifact"):
            yadisk_control.validate_response({
                **response,
                "artifact_path": "/control/../secret",
            })


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
                "set_event", {"name": "Свадьба"}, reply_chat_id=123)

        self.assertEqual(command["command_id"], "a" * 32)
        self.assertEqual(uploads[0][0]["message_type"], "command")
        self.assertEqual(uploads[0][0]["data"], {"name": "Свадьба"})
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

    def test_event_command_requires_iso_date_except_cafe(self):
        self.assertEqual(
            app._event_name_from_command(
                "/event   2026-08-17   Свадьба   Ивановых  "
            ),
            "2026-08-17 Свадьба Ивановых",
        )
        self.assertEqual(app._event_name_from_command("/event Кафе"), "Кафе")
        for command in (
            "/event Свадьба Ивановых",
            "/event 2026-02-31 Свадьба Ивановых",
            "/event 2026-08-17",
            "/event ../bad",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                app._event_name_from_command(command)

    def test_event_access_token_is_stable_url_safe_and_fixed_length(self):
        with patch.object(app, "EVENT_KEY", "test-event-key"):
            token = app._event_access_token(
                "  2026-08-17   СВАДЬБА Ивановых  ")
            self.assertEqual(
                token,
                app._event_access_token("2026-08-17 свадьба ивановых"),
            )

        self.assertEqual(len(token), 46)
        self.assertRegex(token, r"^ev_[A-Za-z0-9_-]{43}$")


class CafeUnblockCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_default_and_explicit_session_count(self):
        self.assertEqual(app._unblock_sessions_from_command("/unblock"), 1)
        self.assertEqual(app._unblock_sessions_from_command("/unblock 25"), 25)
        self.assertEqual(
            app._unblock_sessions_from_command("/unblock@photobooth_bot 1000"),
            1000,
        )
        self.assertIsNone(app._unblock_sessions_from_command("/status"))

    def test_rejects_invalid_session_count(self):
        for command in (
            "/unblock 0",
            "/unblock 1001",
            "/unblock -1",
            "/unblock 1.5",
            "/unblock many",
            "/unblock 2 extra",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                app._unblock_sessions_from_command(command)

    def test_is_registered_as_fixed_command_and_keyboard_button(self):
        self.assertEqual(app.TG_COMMANDS["/unblock"], "unblock")
        callbacks = [
            button["callback_data"]
            for row in app.TG_COMMAND_KEYBOARD["inline_keyboard"]
            for button in row
        ]
        self.assertIn("/unblock", callbacks)
        self.assertIsNone(app._camera_setting_from_command("/unblock 2"))

    async def test_forwards_session_count_to_booth(self):
        with patch(
            "app._send_disk_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/unblock 7")

        send.assert_awaited_once_with("unblock", 123, {"sessions": 7})
        self.assertIn("7", send_text.await_args.args[3])
        self.assertIn("подтверждение будки", send_text.await_args.args[3])

    async def test_without_count_forwards_one_session(self):
        with patch(
            "app._send_disk_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ):
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/unblock")

        send.assert_awaited_once_with("unblock", 123, {"sessions": 1})

    async def test_invalid_count_is_reported_without_disk_command(self):
        with patch(
            "app._send_disk_command",
            new_callable=AsyncMock,
        ) as send, patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/unblock 1001")

        send.assert_not_awaited()
        self.assertIn("от 1 до 1000", send_text.await_args.args[3])


class CameraSettingCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_dynamic_camera_setting(self):
        self.assertEqual(
            app._camera_setting_from_command("/iso 200"),
            ("iso", "200"),
        )
        self.assertEqual(
            app._camera_setting_from_command(
                "/white_balance@photobooth_bot AUTO"),
            ("white_balance", "AUTO"),
        )

    def test_does_not_intercept_reserved_or_plain_commands(self):
        self.assertIsNone(app._camera_setting_from_command("/status"))
        self.assertIsNone(app._camera_setting_from_command("/get_config"))
        self.assertIsNone(app._camera_setting_from_command("/event Wedding"))
        self.assertIsNone(app._camera_setting_from_command("plain text"))

    def test_requires_value_for_dynamic_camera_setting(self):
        with self.assertRaisesRegex(ValueError, "Использование"):
            app._camera_setting_from_command("/continuous_af")

    async def test_forwards_raw_value_to_booth(self):
        with patch(
            "app._send_disk_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/iso auto")

        send.assert_awaited_once_with(
            "set_camera_config",
            123,
            {"field": "iso", "value": "auto"},
        )
        self.assertIn("ожидаю подтверждение", send_text.await_args.args[3])

    async def test_missing_value_returns_usage_without_disk_command(self):
        with patch(
            "app._send_disk_command",
            new_callable=AsyncMock,
        ) as send, patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/continuous_af")

        send.assert_not_awaited()
        self.assertIn("Использование", send_text.await_args.args[3])

    async def test_get_config_is_forwarded_as_fixed_command(self):
        with patch(
            "app._send_disk_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/get_config")

        send.assert_awaited_once_with("get_config", 123)
        self.assertEqual(
            send_text.await_args.args[3],
            "⏳ Запрашиваю конфиги фотобудки...",
        )


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_does_not_restart_automatically(self):
        async def update(progress_callback):
            await progress_callback("retry notice")
            return "published"

        with patch("app._do_update", AsyncMock(side_effect=update)) as do_update, \
             patch("app._send_disk_command", AsyncMock(return_value="a" * 32)) as send, \
             patch("app._tg_send_text", AsyncMock(return_value=True)) as send_text:
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/update")

        do_update.assert_awaited_once()
        send.assert_not_awaited()
        self.assertEqual(
            [item.args[3] for item in send_text.await_args_list],
            ["⏳ Скачиваю полный релиз...", "retry notice", "published"],
        )


class LogDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_integer_chat_id_is_serialized_as_multipart_text(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Telegram:
            def post(self, *_args, **_kwargs):
                return Response()

        class ClientSession:
            async def __aenter__(self):
                return Telegram()

            async def __aexit__(self, *_args):
                return False

        form = MagicMock()
        with patch.object(app, "TG_TOKEN", "token"), \
             patch("app.aiohttp.FormData", return_value=form), \
             patch("app.aiohttp.ClientSession", return_value=ClientSession()):
            self.assertTrue(await app._tg_send_log(5683598562, b"log"))

        form.add_field.assert_any_call("chat_id", "5683598562")

    async def test_successful_document_is_not_retried_for_cleanup_or_text(self):
        response = {
            "status": "ok",
            "command": "send_logs",
            "message": "Лог загружен",
            "artifact_path": "/control/logs/test.log",
            "reply_chat_id": 5683598562,
        }
        with patch.object(app, "TG_TOKEN", "token"), \
             patch.object(app, "TG_ADMIN", "admin"), \
             patch("app.yadisk_control.download_bytes", AsyncMock(return_value=b"log")), \
             patch("app._tg_send_log", AsyncMock(return_value=True)) as send_log, \
             patch("app._tg_send_text", new_callable=AsyncMock) as send_text, \
             patch("app.yadisk_control.delete_resource", AsyncMock(
                 side_effect=RuntimeError("cleanup unavailable"))) as delete:
            self.assertTrue(await app._handle_control_response(response))

        send_log.assert_awaited_once_with(5683598562, b"log")
        send_text.assert_not_awaited()
        delete.assert_awaited_once_with("/control/logs/test.log")


class PrintCommandResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_updates_queue_without_duplicate_user_message(self):
        command_id = "a" * 32
        response = {
            "status": "ok",
            "command": "print_image",
            "command_id": command_id,
            "message": "Ваше фото добавлено в очередь",
            "reply_chat_id": 123,
        }
        queued = {
            "outcome": "queued",
            "status": "queued",
            "command_id": command_id,
        }
        with patch.object(
            app.database,
            "mark_print_job_queued",
            AsyncMock(return_value=queued),
        ) as mark_queued, patch(
            "app._tg_send_text",
            new_callable=AsyncMock,
        ) as send_text, patch(
            "app.aiohttp.ClientSession",
            side_effect=AssertionError("Telegram session must not be opened"),
        ):
            self.assertTrue(await app._handle_control_response(response))

        mark_queued.assert_awaited_once_with(command_id=command_id)
        send_text.assert_not_awaited()

    async def test_booth_error_is_still_reported_to_user(self):
        command_id = "b" * 32
        response = {
            "status": "error",
            "command": "print_image",
            "command_id": command_id,
            "message": "Принтер не готов",
            "reply_chat_id": 123,
        }
        failed = {
            "outcome": "failed",
            "status": "failed",
            "command_id": command_id,
        }
        telegram = object()

        class ClientSession:
            async def __aenter__(self):
                return telegram

            async def __aexit__(self, *_args):
                return False

        with patch.object(app, "TG_TOKEN", "token"), patch.object(
            app.database,
            "mark_print_job_failed",
            AsyncMock(return_value=failed),
        ) as mark_failed, patch(
            "app.aiohttp.ClientSession",
            return_value=ClientSession(),
        ), patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            self.assertTrue(await app._handle_control_response(response))

        mark_failed.assert_awaited_once_with(
            command_id=command_id,
            last_error="Принтер не готов",
        )
        send_text.assert_awaited_once_with(
            telegram,
            "https://api.telegram.org/bottoken",
            123,
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

        class Telegram:
            def post(self, *_args, **_kwargs):
                return Response()

        class ClientSession:
            async def __aenter__(self):
                return Telegram()

            async def __aexit__(self, *_args):
                return False

        form = MagicMock()
        with patch.object(app, "TG_TOKEN", "token"), \
             patch("app.aiohttp.FormData", return_value=form), \
             patch("app.aiohttp.ClientSession", return_value=ClientSession()):
            self.assertTrue(await app._tg_send_document(
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
            "reply_chat_id": 123,
        }
        export = b"===== config_app.json =====\n{}\n"
        vps_config = b'{"yadisk_folder":"event"}\n'
        with patch.object(app, "TG_TOKEN", "token"), \
             patch.object(app, "CONFIG_PATH") as config_path, \
             patch("app.yadisk_control.download_bytes",
                   AsyncMock(return_value=export)), \
             patch("app._tg_send_documents",
                   AsyncMock(return_value=True)) as send, \
             patch("app._tg_send_log", new_callable=AsyncMock) as send_log, \
             patch("app.yadisk_control.delete_resource",
                   AsyncMock(return_value=True)) as delete:
            config_path.read_bytes.return_value = vps_config
            self.assertTrue(await app._handle_control_response(response))

        send.assert_awaited_once_with(
            123,
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
        send_log.assert_not_awaited()
        delete.assert_awaited_once_with("/control/configs/test.txt")


if __name__ == "__main__":
    unittest.main()
