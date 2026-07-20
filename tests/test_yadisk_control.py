import json
import unittest
from unittest.mock import AsyncMock, patch

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


if __name__ == "__main__":
    unittest.main()
