import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import messenger_delivery
from messaging import ReplyTarget


def session_context():
    session = object()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    return session, context


class ReplyTargetTests(unittest.TestCase):
    def test_normalizes_provider_and_conversation_id(self):
        target = ReplyTarget(" VK ", 556972284)

        self.assertEqual(target.provider, "vk")
        self.assertEqual(target.conversation_id, "556972284")
        self.assertEqual(
            target.to_dict(),
            {
                "provider": "vk",
                "conversation_id": "556972284",
            },
        )

    def test_rejects_unsupported_provider_and_invalid_conversation_id(self):
        for provider, conversation_id in (
            ("max", 123),
            ("telegram", ""),
            ("telegram", None),
            ("vk", True),
        ):
            with self.subTest(
                provider=provider,
                conversation_id=conversation_id,
            ), self.assertRaises(ValueError):
                ReplyTarget(provider, conversation_id)


class MessengerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_text_to_telegram(self):
        session, context = session_context()
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ), patch(
            "messenger_delivery.telegram_api.send_text",
            AsyncMock(return_value=True),
        ) as send:
            delivered = await messenger_delivery.send_text(
                ReplyTarget("telegram", 123),
                "hello",
            )

        self.assertTrue(delivered)
        send.assert_awaited_once_with(
            session,
            messenger_delivery.telegram_api.BOT_API_BASE,
            "123",
            "hello",
        )

    async def test_routes_text_to_vk_with_integer_peer_id(self):
        session, context = session_context()
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ), patch(
            "messenger_delivery.vk_api.send_text",
            AsyncMock(return_value=1),
        ) as send:
            delivered = await messenger_delivery.send_text(
                ReplyTarget("vk", "556972284"),
                "hello",
            )

        self.assertTrue(delivered)
        send.assert_awaited_once_with(session, 556972284, "hello")

    async def test_routes_html_text_only_to_telegram(self):
        session, context = session_context()
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ), patch(
            "messenger_delivery.telegram_api.send_text",
            AsyncMock(return_value=True),
        ) as send:
            delivered = await messenger_delivery.send_text(
                ReplyTarget("telegram", 123),
                "<b>Кафе</b>",
                parse_mode="HTML",
            )

        self.assertTrue(delivered)
        send.assert_awaited_once_with(
            session,
            messenger_delivery.telegram_api.BOT_API_BASE,
            "123",
            "<b>Кафе</b>",
            parse_mode="HTML",
        )

    async def test_rejects_telegram_parse_mode_for_vk(self):
        _session, context = session_context()
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ):
            with self.assertRaisesRegex(ValueError, "only for Telegram"):
                await messenger_delivery.send_text(
                    ReplyTarget("vk", 556972284),
                    "<b>Кафе</b>",
                    parse_mode="HTML",
                )

    async def test_routes_photo_to_selected_provider(self):
        session, context = session_context()
        keyboard = {"inline": True, "buttons": []}
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ), patch(
            "messenger_delivery.vk_api.send_photo",
            AsyncMock(return_value=1),
        ) as send:
            delivered = await messenger_delivery.send_photo(
                ReplyTarget("vk", 556972284),
                b"png",
                "caption",
                filename="card.png",
                content_type="image/png",
                keyboard=keyboard,
            )

        self.assertTrue(delivered)
        send.assert_awaited_once_with(
            session,
            556972284,
            b"png",
            "caption",
            filename="card.png",
            content_type="image/png",
            keyboard=keyboard,
        )

    async def test_telegram_photo_forwards_parse_mode_and_requires_message_id(self):
        session, context = session_context()
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ), patch(
            "messenger_delivery.telegram_api.send_photo",
            AsyncMock(return_value=None),
        ) as send:
            delivered = await messenger_delivery.send_photo(
                ReplyTarget("telegram", 123),
                b"png",
                "<b>caption</b>",
                parse_mode="HTML",
            )

        self.assertFalse(delivered)
        send.assert_awaited_once_with(
            session,
            messenger_delivery.telegram_api.BOT_API_BASE,
            "123",
            b"png",
            "<b>caption</b>",
            None,
            None,
            filename="image.png",
            content_type="image/png",
            parse_mode="HTML",
        )

    async def test_routes_document_group_to_vk(self):
        session, context = session_context()
        documents = [
            (b"log", "photobooth.log", "text/plain"),
            (b"{}", "config.json", "application/json"),
        ]
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ), patch(
            "messenger_delivery.vk_api.send_documents",
            AsyncMock(return_value=1),
        ) as send:
            delivered = await messenger_delivery.send_documents(
                ReplyTarget("vk", 556972284),
                documents,
            )

        self.assertTrue(delivered)
        send.assert_awaited_once_with(session, 556972284, documents)

    async def test_vk_delivery_requires_a_valid_message_id(self):
        _session, context = session_context()
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ), patch(
            "messenger_delivery.vk_api.send_text",
            AsyncMock(return_value=None),
        ):
            delivered = await messenger_delivery.send_text(
                ReplyTarget("vk", 556972284),
                "hello",
            )

        self.assertFalse(delivered)

    async def test_routes_single_document_to_telegram(self):
        with patch(
            "messenger_delivery.telegram_api.send_document",
            AsyncMock(return_value=True),
        ) as send:
            delivered = await messenger_delivery.send_document(
                ReplyTarget("telegram", 123),
                b"log",
                "photobooth.log",
                "text/plain",
            )

        self.assertTrue(delivered)
        send.assert_awaited_once_with(
            "123",
            b"log",
            "photobooth.log",
            "text/plain",
        )

    async def test_rejects_non_positive_vk_peer_id(self):
        _session, context = session_context()
        with patch(
            "messenger_delivery.aiohttp.ClientSession",
            return_value=context,
        ):
            with self.assertRaisesRegex(ValueError, "positive"):
                await messenger_delivery.send_text(
                    ReplyTarget("vk", 0),
                    "hello",
                )


if __name__ == "__main__":
    unittest.main()
