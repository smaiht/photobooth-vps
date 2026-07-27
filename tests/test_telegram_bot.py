import unittest
from unittest.mock import AsyncMock, patch

import telegram_bot
import telegram_print
import print_flow


class TelegramPollingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_message_update(self):
        session = object()
        update = {"update_id": 1, "message": {"text": "hello"}}
        message_handler = AsyncMock()
        callback_handler = AsyncMock()

        await telegram_bot.dispatch_update(
            session,
            "https://telegram.test",
            update,
            message_handler,
            callback_handler,
        )

        message_handler.assert_awaited_once_with(
            session,
            "https://telegram.test",
            update,
            update["message"],
        )
        callback_handler.assert_not_awaited()

    async def test_dispatches_callback_update(self):
        session = object()
        callback = {"id": "callback-1"}
        message_handler = AsyncMock()
        callback_handler = AsyncMock()

        await telegram_bot.dispatch_update(
            session,
            "https://telegram.test",
            {"update_id": 2, "callback_query": callback},
            message_handler,
            callback_handler,
        )

        callback_handler.assert_awaited_once_with(
            session,
            "https://telegram.test",
            callback,
        )
        message_handler.assert_not_awaited()

    async def test_polling_stays_disabled_without_token(self):
        with patch.object(
            telegram_bot.telegram_api,
            "BOT_TOKEN",
            "",
        ), patch(
            "telegram_bot.aiohttp.ClientSession",
        ) as client_session:
            await telegram_bot.poll_updates(AsyncMock(), AsyncMock())

        client_session.assert_not_called()


class TelegramPrintAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_choice_caption_keeps_telegram_html_emphasis(self):
        owner = print_flow.PrintUser(
            provider="telegram",
            provider_user_id=123,
            conversation_id=123,
        )
        upload = print_flow.PrintUpload(
            user=owner,
            suffix=".jpg",
            download=AsyncMock(),
        )
        with patch(
            "telegram_print.telegram_api.send_photo",
            AsyncMock(return_value=42),
        ) as send:
            await telegram_print.TelegramPrintUI(
                object(),
                "https://telegram.test",
            ).send_choice(upload, b"preview", "a" * 32)

        caption = send.await_args.args[4]
        self.assertIn("<b>Фото не совпадает", caption)
        self.assertIn("<b>как есть</b>", caption)

    def test_forwarded_photo_belongs_to_the_forwarding_user(self):
        message = {
            "message_id": 77,
            "from": {"id": 123, "first_name": "Forwarder"},
            "chat": {"id": -100500, "type": "group"},
            "photo": [{"file_id": "photo", "file_size": 100}],
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 999, "first_name": "Original"},
            },
        }

        owner = telegram_print.user_from_message(message)

        self.assertEqual(owner.provider_user_id, 123)
        self.assertEqual(owner.conversation_id, "-100500")
        self.assertEqual(owner.metadata["telegram_user"]["id"], 123)

    async def test_callback_is_normalized_for_shared_print_flow(self):
        job_id = "c" * 32
        callback = {
            "id": "callback-1",
            "data": f"print:fill:{job_id}",
            "from": {"id": 123},
            "message": {"message_id": 4, "chat": {"id": 123}},
        }
        with patch(
            "telegram_print.print_flow.handle_choice",
            AsyncMock(return_value=True),
        ) as handle:
            self.assertTrue(await telegram_print.handle_callback(
                object(),
                "https://telegram.test",
                callback,
            ))

        action = handle.await_args.args[0]
        self.assertEqual(action.action, "fill")
        self.assertEqual(action.job_id, job_id)
        self.assertEqual(action.user.provider, "telegram")

    async def test_photo_is_routed_before_admin_command(self):
        update = {"update_id": 1}
        message = {
            "message_id": 2,
            "from": {"id": 123},
            "chat": {"id": 123},
            "photo": [{"file_id": "photo"}],
        }
        with patch(
            "telegram_bot.record_start",
            AsyncMock(return_value=False),
        ), patch(
            "telegram_bot.telegram_print.handle_message",
            AsyncMock(return_value=True),
        ) as photo, patch(
            "telegram_bot.telegram_api.is_admin",
            return_value=True,
        ), patch(
            "telegram_bot.admin_command_service.handle_message",
            new_callable=AsyncMock,
        ) as admin:
            await telegram_bot.route_message_update(
                object(),
                "https://telegram.test",
                update,
                message,
            )

        photo.assert_awaited_once()
        admin.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
