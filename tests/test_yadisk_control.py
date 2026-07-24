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

    def test_parses_event_command_with_spaces(self):
        self.assertEqual(
            app._event_name_from_command("/event Свадьба Ивановых 2026"),
            "Свадьба Ивановых 2026",
        )
        with self.assertRaises(ValueError):
            app._event_name_from_command("/event ../bad")


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
        with patch.object(app, "TG_TOKEN", "token"), \
             patch("app.yadisk_control.download_bytes",
                   AsyncMock(return_value=export)), \
             patch("app._tg_send_document",
                   AsyncMock(return_value=True)) as send, \
             patch("app._tg_send_log", new_callable=AsyncMock) as send_log, \
             patch("app.yadisk_control.delete_resource",
                   AsyncMock(return_value=True)) as delete:
            self.assertTrue(await app._handle_control_response(response))

        send.assert_awaited_once_with(
            123,
            export,
            "photobooth_configs.txt",
            "text/plain; charset=utf-8",
        )
        send_log.assert_not_awaited()
        delete.assert_awaited_once_with("/control/configs/test.txt")


if __name__ == "__main__":
    unittest.main()
