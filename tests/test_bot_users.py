import unittest
from unittest.mock import AsyncMock, patch

import telegram_bot
from messaging import ReplyTarget


class StartParameterTests(unittest.TestCase):
    def test_parses_start_with_and_without_parameter(self):
        self.assertEqual(telegram_bot.parse_start_command("/start"), (True, None))
        self.assertEqual(
            telegram_bot.parse_start_command("/start wedding_A1-b2"),
            (True, "wedding_A1-b2"),
        )
        self.assertEqual(
            telegram_bot.parse_start_command("/start@photobooth_bot cafe_key"),
            (True, "cafe_key"),
        )
        self.assertEqual(telegram_bot.parse_start_command("hello"), (False, None))


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
            "telegram_bot.database.record_bot_start", AsyncMock(return_value=1)
        ) as record:
            matched = await telegram_bot.record_start(update, update["message"])

        self.assertTrue(matched)
        record.assert_awaited_once_with(
            provider="telegram",
            provider_user_id=123,
            start_parameter="wedding_key",
            provider_update_id=456,
            username="guest",
            first_name="Иван",
            last_name="Иванов",
        )

    async def test_ignores_non_start_messages(self):
        with patch("telegram_bot.database.record_bot_start", AsyncMock()) as record:
            matched = await telegram_bot.record_start(
                {"update_id": 1},
                {"text": "photo", "from": {"id": 123}},
            )

        self.assertFalse(matched)
        record.assert_not_awaited()

    async def test_admin_message_uses_shared_command_service(self):
        update = {"update_id": 789}
        message = {
            "text": "/status",
            "from": {"id": 123},
            "chat": {"id": 456},
        }
        with patch(
            "telegram_bot.record_start",
            AsyncMock(return_value=False),
        ), patch(
            "telegram_bot.telegram_print.handle_message",
            AsyncMock(return_value=False),
        ), patch(
            "telegram_bot.telegram_api.is_admin",
            return_value=True,
        ), patch(
            "telegram_bot.admin_command_service.handle_message",
            new_callable=AsyncMock,
        ) as handle, patch(
            "telegram_bot.reply_to_plain_message",
            new_callable=AsyncMock,
        ) as plain_reply:
            await telegram_bot.route_message_update(
                object(),
                "https://telegram.test",
                update,
                message,
            )

        handle.assert_awaited_once_with(
            ReplyTarget("telegram", 456),
            "/status",
        )
        plain_reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
