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


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_does_not_restart_automatically(self):
        with patch("app._do_update", AsyncMock(return_value="published")), \
             patch("app._send_disk_command", AsyncMock(return_value="a" * 32)) as send, \
             patch("app._tg_send_text", AsyncMock(return_value=True)):
            await app._tg_handle_admin_command(
                object(), "https://telegram.test", 123, "/update")

        send.assert_not_awaited()


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


if __name__ == "__main__":
    unittest.main()
