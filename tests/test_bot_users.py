import unittest
from unittest.mock import AsyncMock, patch

import app


class StartParameterTests(unittest.TestCase):
    def test_parses_start_with_and_without_parameter(self):
        self.assertEqual(app._start_parameter_from_command("/start"), (True, None))
        self.assertEqual(
            app._start_parameter_from_command("/start wedding_A1-b2"),
            (True, "wedding_A1-b2"),
        )
        self.assertEqual(
            app._start_parameter_from_command("/start@photobooth_bot cafe_key"),
            (True, "cafe_key"),
        )
        self.assertEqual(app._start_parameter_from_command("hello"), (False, None))


class TelegramStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_common_user_fields_without_language_code(self):
        update = {
            "update_id": 456,
            "message": {
                "text": "/start wedding_key",
                "from": {
                    "id": 123,
                    "username": "guest",
                    "first_name": "Иван",
                    "last_name": "Иванов",
                    "language_code": "ru",
                },
            },
        }
        with patch(
            "app.database.record_bot_start", AsyncMock(return_value=1)
        ) as record:
            matched = await app._record_telegram_start(update, update["message"])

        self.assertTrue(matched)
        record.assert_awaited_once_with(
            provider="telegram",
            provider_user_id=123,
            start_parameter="wedding_key",
            provider_update_id=456,
            username="guest",
            first_name="Иван",
            last_name="Иванов",
            profile={},
        )

    async def test_ignores_non_start_messages(self):
        with patch("app.database.record_bot_start", AsyncMock()) as record:
            matched = await app._record_telegram_start(
                {"update_id": 1},
                {"text": "photo", "from": {"id": 123}},
            )

        self.assertFalse(matched)
        record.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
