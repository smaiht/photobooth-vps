import io
import json
import traceback
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import aiohttp
from PIL import Image

import admin_notifications
import database
import event_access
import print_flow
import vk_api
import vk_bot
import vk_print
from messaging import ReplyTarget


class VkDeepLinkTests(unittest.TestCase):
    def test_builds_vk_link_with_same_event_token(self):
        with patch.object(event_access, "EVENT_KEY", "test-event-key"), \
             patch.object(vk_api, "GROUP_USERNAME", "fotobudka_vu"):
            link = event_access.vk_start_link("2026-08-17 Свадьба")
            expected_token = event_access.access_token("2026-08-17 Свадьба")

        parsed = urlsplit(link)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "vk.me")
        self.assertEqual(parsed.path, "/fotobudka_vu")
        self.assertEqual(
            parse_qs(parsed.query),
            {"ref": [expected_token]},
        )

    def test_guest_qr_sheet_contains_two_large_codes(self):
        links = {
            "telegram": "https://t.me/example_bot?start=abcdefghijkl",
            "vk": "https://vk.me/example?ref=abcdefghijkl",
        }
        payload = event_access.guest_qr_sheet_png(links)

        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, image.height)
            self.assertGreaterEqual(image.width, 900)
            self.assertGreaterEqual(image.height, 400)

    def test_database_provider_set_includes_vk(self):
        self.assertIn("vk", database.PROVIDERS)

    def test_builds_photo_attachment_with_access_key(self):
        self.assertEqual(
            vk_api.photo_attachment([
                {"owner_id": -123, "id": 456, "access_key": "photo-key"}
            ]),
            "photo-123_456_photo-key",
        )


class VkMessageShapeTests(unittest.TestCase):
    def test_accepts_only_incoming_private_messages(self):
        update = {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 123,
                    "peer_id": 123,
                    "out": 0,
                    "text": "hello",
                }
            },
        }
        self.assertEqual(
            vk_bot.incoming_private_message(update)["text"],
            "hello",
        )

        outgoing = {
            **update,
            "object": {"message": {**update["object"]["message"], "out": 1}},
        }
        chat = {
            **update,
            "object": {
                "message": {
                    **update["object"]["message"],
                    "peer_id": 2_000_000_001,
                }
            },
        }
        self.assertIsNone(vk_bot.incoming_private_message(outgoing))
        self.assertIsNone(vk_bot.incoming_private_message(chat))

    def test_accepts_only_callback_events_from_private_conversations(self):
        update = {
            "type": "message_event",
            "object": {
                "event_id": "callback-event",
                "user_id": 123,
                "peer_id": 123,
                "conversation_message_id": 77,
                "payload": {"type": "print_choice"},
            },
        }

        self.assertEqual(
            vk_bot.incoming_private_event(update)["event_id"],
            "callback-event",
        )

        group_chat = {
            **update,
            "object": {**update["object"], "peer_id": 2_000_000_001},
        }
        missing_event_id = {
            **update,
            "object": {**update["object"], "event_id": ""},
        }
        self.assertIsNone(vk_bot.incoming_private_event(group_chat))
        self.assertIsNone(vk_bot.incoming_private_event(missing_event_id))


class VkUserProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        vk_bot._user_profile_cache.clear()

    def tearDown(self):
        vk_bot._user_profile_cache.clear()

    def test_normalizes_cyrillic_profile_and_screen_name(self):
        profile = vk_api.extract_user_profile(
            [{
                "id": 123,
                "screen_name": "  foto_guest  ",
                "first_name": " Алёна ",
                "last_name": " Ёлкина ",
            }],
            123,
        )

        self.assertEqual(profile, {
            "username": "foto_guest",
            "first_name": "Алёна",
            "last_name": "Ёлкина",
        })

    async def test_users_get_requests_screen_name(self):
        response = [{
            "id": 123,
            "screen_name": "foto_guest",
            "first_name": "Иван",
            "last_name": "Иванов",
        }]
        session = object()
        with patch(
            "vk_api.api_call",
            AsyncMock(return_value=response),
        ) as api:
            profile = await vk_api.get_user_profile(session, 123)

        self.assertEqual(profile["username"], "foto_guest")
        api.assert_awaited_once_with(
            session,
            "users.get",
            user_ids="123",
            fields="screen_name",
        )

    async def test_profile_is_cached_but_lookup_failure_is_not(self):
        profile = {
            "username": "foto_guest",
            "first_name": "Иван",
            "last_name": "Иванов",
        }
        lookup = AsyncMock(side_effect=(RuntimeError("temporary"), profile))
        with patch("vk_bot.vk_api.get_user_profile", lookup), self.assertLogs(
            vk_bot.log,
            level="WARNING",
        ):
            first = await vk_bot.cached_user_profile(object(), 123)
            second = await vk_bot.cached_user_profile(object(), 123)
            third = await vk_bot.cached_user_profile(object(), 123)

        self.assertEqual(first, {})
        self.assertEqual(second, profile)
        self.assertEqual(third, profile)
        self.assertEqual(lookup.await_count, 2)

    def test_print_user_receives_vk_profile(self):
        user = vk_print.user_from_message(
            {"from_id": 123, "peer_id": 123},
            profile={
                "username": "foto_guest",
                "first_name": "Иван",
                "last_name": "Иванов",
            },
        )

        self.assertEqual(user.username, "foto_guest")
        self.assertEqual(user.first_name, "Иван")
        self.assertEqual(user.last_name, "Иванов")


class VkBatchReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_retried_batch_skips_updates_that_already_completed(self):
        updates = [
            {
                "event_id": "event-1",
                "type": "message_new",
                "object": {"message": {
                    "from_id": 1,
                    "peer_id": 1,
                    "text": "/status",
                }},
            },
            {
                "event_id": "event-2",
                "type": "message_new",
                "object": {"message": {
                    "from_id": 2,
                    "peer_id": 2,
                    "text": "photo",
                }},
            },
        ]
        calls = []

        async def route(_session, update, _message):
            event_id = update["event_id"]
            calls.append(event_id)
            if event_id == "event-2" and calls.count(event_id) == 1:
                raise RuntimeError("temporary failure")

        completed = set()
        with patch("vk_bot.route_message_update", side_effect=route):
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                await vk_bot.process_update_batch(object(), updates, completed)
            await vk_bot.process_update_batch(object(), updates, completed)

        self.assertEqual(calls, ["event-1", "event-2", "event-2"])
        self.assertEqual(completed, {"event-1", "event-2"})

    async def test_callback_event_is_routed_once_without_profile_api_delay(self):
        event = {
            "event_id": "button-event",
            "user_id": 123,
            "peer_id": 123,
            "conversation_message_id": 77,
            "payload": {
                "type": "print_choice",
                "action": "fit",
                "job_id": "a" * 32,
            },
        }
        update = {"type": "message_event", "object": event}
        completed = set()
        vk_bot._user_profile_cache.clear()
        with patch(
            "vk_bot.vk_print.handle_event",
            AsyncMock(return_value=True),
        ) as handle, patch(
            "vk_bot.cached_user_profile",
            new_callable=AsyncMock,
        ) as lookup:
            await vk_bot.process_update_batch(object(), [update, update], completed)

        handle.assert_awaited_once_with(ANY, event, profile={})
        lookup.assert_not_awaited()
        self.assertEqual(completed, {"message_event:button-event"})

    async def test_unknown_callback_is_answered_without_chat_message(self):
        event = {
            "event_id": "unknown-event",
            "user_id": 123,
            "peer_id": 123,
            "conversation_message_id": 77,
            "payload": {"type": "unknown"},
        }
        with patch(
            "vk_bot.vk_print.handle_event",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.vk_api.answer_message_event",
            AsyncMock(),
        ) as answer, self.assertLogs(vk_bot.log, level="WARNING"):
            await vk_bot.route_message_event(object(), event)

        answer.assert_awaited_once_with(
            ANY,
            event_id="unknown-event",
            user_id=123,
            peer_id=123,
            text="Кнопка устарела или больше неактивна",
        )


class VkPrintAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_largest_photo_without_persisting_signed_url(self):
        message = {
            "attachments": [{
                "type": "photo",
                "photo": {
                    "owner_id": 1,
                    "id": 2,
                    "sizes": [
                        {
                            "url": "https://cdn.example/small.jpg?signature=secret",
                            "width": 100,
                            "height": 100,
                        },
                        {
                            "url": "https://cdn.example/large.jpg?signature=secret",
                            "width": 1200,
                            "height": 800,
                        },
                    ],
                },
            }],
        }

        image = vk_print.extract_image(message)

        self.assertEqual(
            image.url,
            "https://cdn.example/large.jpg?signature=secret",
        )
        self.assertEqual(image.suffix, ".jpg")
        self.assertNotIn("url", image.metadata)
        self.assertNotIn("signature", repr(image.metadata))

    async def test_download_error_does_not_expose_signed_url(self):
        signed_url = "https://cdn.example/photo.jpg?signature=TEST_SECRET"
        session = MagicMock()
        session.get.side_effect = aiohttp.InvalidURL(signed_url)
        image = vk_print.VkImage(
            url=signed_url,
            suffix=".jpg",
            declared_size=None,
            metadata={},
        )

        try:
            await vk_print.download_image(session, image)
        except RuntimeError as exc:
            rendered = "".join(traceback.format_exception(exc))
        else:
            self.fail("download_image should fail")

        self.assertNotIn("TEST_SECRET", rendered)

    def test_extracts_supported_image_document(self):
        image = vk_print.extract_image({
            "attachments": [{
                "type": "doc",
                "doc": {
                    "owner_id": 1,
                    "id": 2,
                    "title": "portrait.png",
                    "ext": "png",
                    "size": 1234,
                    "url": "https://cdn.example/portrait.png",
                },
            }],
        })

        self.assertEqual(image.suffix, ".png")
        self.assertEqual(image.declared_size, 1234)
        self.assertEqual(image.metadata["source_filename"], "portrait.png")

    def test_rejects_multiple_printable_attachments(self):
        photo = {
            "type": "photo",
            "photo": {
                "sizes": [{
                    "url": "https://cdn.example/photo.jpg",
                    "width": 100,
                    "height": 100,
                }],
            },
        }
        with self.assertRaisesRegex(ValueError, "альбомы"):
            vk_print.extract_image({"attachments": [photo, photo]})

    def test_parses_vk_keyboard_payload_into_shared_action(self):
        job_id = "a" * 32
        message = {
            "from_id": 123,
            "peer_id": 123,
            "conversation_message_id": 77,
            "payload": json.dumps({
                "type": "print_choice",
                "action": "fill",
                "job_id": job_id,
            }),
        }

        kind, action = vk_print.parse_action(message)

        self.assertEqual(kind, "print_choice")
        self.assertEqual(action.action, "fill")
        self.assertEqual(action.job_id, job_id)
        self.assertEqual(action.user.target, ReplyTarget("vk", 123))

    def test_parses_vk_callback_event_into_shared_action(self):
        job_id = "b" * 32
        event = {
            "event_id": "callback-event",
            "user_id": 123,
            "peer_id": 123,
            "conversation_message_id": 77,
            "payload": {
                "type": "print_choice",
                "action": "fill",
                "job_id": job_id,
            },
        }

        kind, action = vk_print.parse_event_action(event)

        self.assertEqual(kind, "print_choice")
        self.assertEqual(action.action, "fill")
        self.assertEqual(action.job_id, job_id)
        self.assertEqual(action.action_id, "callback-event")
        self.assertEqual(action.context["vk_message_event"], event)

    async def test_choice_card_contains_callback_button_payloads(self):
        owner = print_flow.PrintUser(
            provider="vk",
            provider_user_id=123,
            conversation_id=123,
            source_message_id=5,
        )
        upload = print_flow.PrintUpload(
            user=owner,
            suffix=".jpg",
            download=AsyncMock(),
        )
        with patch(
            "vk_print.vk_api.send_photo",
            AsyncMock(return_value=42),
        ) as send:
            message_id = await vk_print.VkPrintUI(object()).send_choice(
                upload,
                b"preview",
                "b" * 32,
            )

        self.assertEqual(message_id, 42)
        caption = send.await_args.args[3]
        self.assertNotIn("<b>", caption)
        self.assertIn("КАК ЕСТЬ", caption)
        keyboard = send.await_args.kwargs["keyboard"]
        actions = [
            button["action"]
            for row in keyboard["buttons"]
            for button in row
        ]
        self.assertTrue(keyboard["inline"])
        self.assertEqual(
            {json.loads(action["payload"])["action"] for action in actions},
            {"fit", "fill", "cancel"},
        )
        self.assertTrue(all(action["type"] == "callback" for action in actions))

    async def test_callback_ack_uses_snackbar_without_new_chat_message(self):
        event = {
            "event_id": "callback-event",
            "user_id": 556972284,
            "peer_id": 556972284,
            "conversation_message_id": 77,
        }
        action = print_flow.PrintAction(
            user=print_flow.PrintUser(
                provider="vk",
                provider_user_id=556972284,
                conversation_id=556972284,
                is_admin=True,
            ),
            action="approve",
            job_id="c" * 32,
            context={"vk_message_event": event},
        )
        with patch(
            "vk_print.vk_api.answer_message_event",
            AsyncMock(),
        ) as answer, patch(
            "vk_print.vk_api.send_text",
            new_callable=AsyncMock,
        ) as send:
            await vk_print.VkPrintUI(object()).acknowledge(
                action,
                "Печать разрешена",
            )

        answer.assert_awaited_once_with(
            ANY,
            event_id="callback-event",
            user_id=556972284,
            peer_id=556972284,
            text="Печать разрешена",
        )
        send.assert_not_awaited()

    async def test_admin_card_edit_preserves_caption_and_photo(self):
        event = {
            "event_id": "callback-event",
            "user_id": 556972284,
            "peer_id": 556972284,
            "conversation_message_id": 77,
        }
        action = print_flow.PrintAction(
            user=print_flow.PrintUser(
                provider="vk",
                provider_user_id=556972284,
                conversation_id=556972284,
                is_admin=True,
            ),
            action="approve",
            job_id="c" * 32,
            context={"vk_message_event": event},
        )
        message = {
            "conversation_message_id": 77,
            "text": "Мероприятие: «Кафе»\nJob: ccc",
            "attachments": [{
                "type": "photo",
                "photo": {
                    "owner_id": -123,
                    "id": 456,
                    "access_key": "photo-key",
                },
            }],
        }
        with patch(
            "vk_print.vk_api.get_message_by_cmid",
            AsyncMock(return_value=message),
        ) as get_message, patch(
            "vk_print.vk_api.edit_message",
            AsyncMock(),
        ) as edit:
            await vk_print.VkPrintUI(object()).update_admin(
                action,
                "✅ Решение администратора: печать разрешена.",
            )

        get_message.assert_awaited_once_with(ANY, 556972284, 77)
        edit.assert_awaited_once_with(
            ANY,
            556972284,
            77,
            "Мероприятие: «Кафе»\nJob: ccc\n\n"
            "✅ Решение администратора: печать разрешена.",
            attachment="photo-123_456_photo-key",
        )

    async def test_photo_is_routed_before_admin_command(self):
        update = {"event_id": "event-photo", "type": "message_new"}
        message = {
            "from_id": 556972284,
            "peer_id": 556972284,
            "attachments": [{"type": "photo", "photo": {}}],
        }
        with patch(
            "vk_bot.cached_user_profile",
            AsyncMock(return_value={}),
        ), patch(
            "vk_bot.record_start",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.vk_print.handle_action",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.vk_print.handle_message",
            AsyncMock(return_value=True),
        ) as photo, patch(
            "vk_bot.vk_api.is_admin",
            return_value=True,
        ), patch(
            "vk_bot.admin_command_service.handle_message",
            new_callable=AsyncMock,
        ) as admin:
            await vk_bot.route_message_update(object(), update, message)

        photo.assert_awaited_once_with(ANY, message, profile={})
        admin.assert_not_awaited()

    async def test_vk_api_serializes_keyboard_and_returns_message_id(self):
        keyboard = {"inline": True, "buttons": []}
        with patch(
            "vk_api.api_call",
            AsyncMock(return_value={"conversation_message_id": 88}),
        ) as api:
            message_id = await vk_api.send_text(
                object(),
                123,
                "choose",
                keyboard=keyboard,
            )

        self.assertEqual(message_id, 88)
        self.assertEqual(json.loads(api.await_args.kwargs["keyboard"]), keyboard)


class VkCallbackApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_edits_card_by_cmid_and_removes_keyboard(self):
        with patch(
            "vk_api.api_call",
            AsyncMock(return_value=1),
        ) as api:
            await vk_api.edit_message(
                object(),
                123,
                77,
                "Исходная подпись\n\n✅ Выбрано",
                attachment="photo-1_2_key",
            )

        api.assert_awaited_once()
        self.assertEqual(api.await_args.args[1], "messages.edit")
        self.assertEqual(api.await_args.kwargs["peer_id"], 123)
        self.assertEqual(api.await_args.kwargs["cmid"], 77)
        self.assertEqual(api.await_args.kwargs["attachment"], "photo-1_2_key")
        self.assertEqual(
            json.loads(api.await_args.kwargs["keyboard"]),
            {"buttons": []},
        )

    async def test_answers_callback_with_cyrillic_snackbar(self):
        with patch(
            "vk_api.api_call",
            AsyncMock(return_value=1),
        ) as api:
            await vk_api.answer_message_event(
                object(),
                event_id="callback-event",
                user_id=123,
                peer_id=123,
                text="Вариант сохранён",
            )

        api.assert_awaited_once()
        self.assertEqual(
            api.await_args.args[1],
            "messages.sendMessageEventAnswer",
        )
        self.assertEqual(
            json.loads(api.await_args.kwargs["event_data"]),
            {"type": "show_snackbar", "text": "Вариант сохранён"},
        )

    async def test_fetches_original_card_by_conversation_message_id(self):
        response = {
            "items": [{
                "conversation_message_id": 77,
                "text": "caption",
            }],
        }
        with patch(
            "vk_api.api_call",
            AsyncMock(return_value=response),
        ) as api:
            message = await vk_api.get_message_by_cmid(object(), 123, 77)

        self.assertEqual(message["text"], "caption")
        api.assert_awaited_once_with(
            ANY,
            "messages.getByConversationMessageId",
            peer_id=123,
            conversation_message_ids="77",
        )

    async def test_long_poll_requires_message_event_subscription(self):
        settings = {
            "is_enabled": 1,
            "events": {"message_new": 1, "message_event": 0},
        }
        with patch(
            "vk_api.api_call",
            AsyncMock(return_value=settings),
        ):
            with self.assertRaisesRegex(vk_api.VkApiError, "message_event"):
                await vk_api.validate_long_poll(object(), 123)


class VkStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_ref_as_vk_start_parameter(self):
        update = {
            "event_id": "event-123",
            "type": "message_new",
        }
        message = {
            "from_id": 556972284,
            "peer_id": 556972284,
            "ref": "event_token",
        }
        with patch(
            "vk_bot.database.record_bot_start",
            AsyncMock(return_value=1),
        ) as record:
            matched = await vk_bot.record_start(
                update,
                message,
                profile={
                    "username": "foto_guest",
                    "first_name": "Иван",
                    "last_name": "Иванов",
                },
            )

        self.assertTrue(matched)
        record.assert_awaited_once_with(
            provider="vk",
            provider_user_id=556972284,
            start_parameter="event_token",
            provider_update_id="event-123",
            username="foto_guest",
            first_name="Иван",
            last_name="Иванов",
        )

    async def test_message_without_ref_is_not_a_start(self):
        with patch("vk_bot.database.record_bot_start", AsyncMock()) as record:
            matched = await vk_bot.record_start(
                {"event_id": "event-1"},
                {"from_id": 1, "peer_id": 1, "text": "hello"},
            )

        self.assertFalse(matched)
        record.assert_not_awaited()

    async def test_valid_ref_gets_connected_reply(self):
        update = {"event_id": "event-123", "type": "message_new"}
        message = {
            "from_id": 123,
            "peer_id": 123,
            "ref": "current-token",
        }
        with patch(
            "vk_bot.cached_user_profile",
            AsyncMock(return_value={}),
        ), patch(
            "vk_bot.database.record_bot_start",
            AsyncMock(return_value=1),
        ), patch(
            "vk_bot.event_access.current_event",
            return_value=("2026-08-17 Свадьба", "current-token", False),
        ), patch(
            "vk_bot.database.user_has_current_start_parameter",
            AsyncMock(return_value=True),
        ), patch(
            "vk_bot.vk_api.send_text",
            AsyncMock(),
        ) as send:
            await vk_bot.route_message_update(object(), update, message)

        self.assertIn("подключены", send.await_args.args[2])
        self.assertIn("2026-08-17 Свадьба", send.await_args.args[2])

    async def test_ref_does_not_swallow_photo_from_the_same_message(self):
        update = {"event_id": "event-photo-ref", "type": "message_new"}
        message = {
            "from_id": 123,
            "peer_id": 123,
            "ref": "current-token",
            "attachments": [{"type": "photo", "photo": {}}],
        }
        with patch(
            "vk_bot.cached_user_profile",
            AsyncMock(return_value={}),
        ), patch(
            "vk_bot.record_start",
            AsyncMock(return_value=True),
        ), patch(
            "vk_bot.reply_for_current_event",
            AsyncMock(),
        ) as reply, patch(
            "vk_bot.vk_print.handle_action",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.vk_print.handle_message",
            AsyncMock(return_value=True),
        ) as photo:
            await vk_bot.route_message_update(object(), update, message)

        reply.assert_awaited_once()
        photo.assert_awaited_once_with(ANY, message, profile={})

    async def test_ref_does_not_swallow_admin_command(self):
        update = {"event_id": "event-admin-ref", "type": "message_new"}
        message = {
            "from_id": 556972284,
            "peer_id": 556972284,
            "ref": "current-token",
            "text": "/status",
        }
        with patch(
            "vk_bot.cached_user_profile",
            AsyncMock(return_value={}),
        ), patch(
            "vk_bot.record_start",
            AsyncMock(return_value=True),
        ), patch(
            "vk_bot.reply_for_current_event",
            AsyncMock(),
        ), patch(
            "vk_bot.vk_print.handle_action",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.vk_print.handle_message",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.vk_api.is_admin",
            return_value=True,
        ), patch(
            "vk_bot.admin_command_service.handle_message",
            AsyncMock(),
        ) as command:
            await vk_bot.route_message_update(object(), update, message)

        command.assert_awaited_once_with(
            ReplyTarget("vk", 556972284),
            "/status",
        )

    async def test_plain_message_is_saved_and_receives_qr_prompt(self):
        update = {"event_id": "event-456", "type": "message_new"}
        message = {"from_id": 321, "peer_id": 321, "text": "hello"}
        with patch(
            "vk_bot.cached_user_profile",
            AsyncMock(return_value={
                "username": "foto_guest",
                "first_name": "Алёна",
                "last_name": "Ёлкина",
            }),
        ), patch(
            "vk_bot.database.ensure_bot_user",
            AsyncMock(return_value=1),
        ) as ensure, patch(
            "vk_bot.event_access.current_event",
            return_value=("2026-08-17 Свадьба", "current-token", False),
        ), patch(
            "vk_bot.database.user_has_current_start_parameter",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.vk_api.is_admin",
            return_value=False,
        ), patch(
            "vk_bot.vk_api.send_text",
            AsyncMock(),
        ) as send:
            await vk_bot.route_message_update(object(), update, message)

        ensure.assert_awaited_once_with(
            provider="vk",
            provider_user_id=321,
            username="foto_guest",
            first_name="Алёна",
            last_name="Ёлкина",
        )
        self.assertIn("QR-код", send.await_args.args[2])

    async def test_admin_message_uses_shared_command_service(self):
        update = {"event_id": "event-admin", "type": "message_new"}
        message = {
            "from_id": 556972284,
            "peer_id": 556972284,
            "text": "/status",
        }
        with patch(
            "vk_bot.cached_user_profile",
            AsyncMock(return_value={}),
        ), patch(
            "vk_bot.record_start",
            AsyncMock(return_value=False),
        ), patch(
            "vk_bot.database.ensure_bot_user",
            AsyncMock(return_value=1),
        ) as ensure, patch(
            "vk_bot.vk_api.is_admin",
            return_value=True,
        ), patch(
            "vk_bot.admin_command_service.handle_message",
            new_callable=AsyncMock,
        ) as handle, patch(
            "vk_bot.reply_for_current_event",
            new_callable=AsyncMock,
        ) as reply:
            await vk_bot.route_message_update(
                object(),
                update,
                message,
            )

        ensure.assert_not_awaited()
        handle.assert_awaited_once_with(
            ReplyTarget("vk", 556972284),
            "/status",
        )
        reply.assert_not_awaited()


class AdminNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_configured_admin_targets_use_symmetric_provider_settings(self):
        with patch.object(
            admin_notifications.telegram_api,
            "BOT_TOKEN",
            "telegram-token",
        ), patch.object(
            admin_notifications.telegram_api,
            "ADMIN_ID",
            "123",
        ), patch.object(
            admin_notifications.vk_api,
            "BOT_TOKEN",
            "vk-token",
        ), patch.object(
            admin_notifications.vk_api,
            "ADMIN_ID",
            "456",
        ):
            targets = admin_notifications.configured_admin_targets()

        self.assertEqual(
            targets,
            (ReplyTarget("telegram", 123), ReplyTarget("vk", 456)),
        )

    async def test_event_card_is_sent_to_telegram_and_vk_admins(self):
        telegram_admin = ReplyTarget("telegram", 123)
        vk_admin = ReplyTarget("vk", 556972284)
        with patch(
            "admin_notifications.configured_admin_targets",
            return_value=(telegram_admin, vk_admin),
        ), patch(
            "admin_notifications.messenger_delivery.send_photo",
            AsyncMock(return_value=True),
        ) as send:
            result = await admin_notifications.send_event_update(
                telegram_admin,
                b"png",
                "event ready",
            )

        self.assertTrue(result.primary_delivered)
        self.assertEqual(
            result.delivered_targets,
            (telegram_admin, vk_admin),
        )
        self.assertEqual(result.failed_targets, ())
        self.assertEqual(
            send.await_args_list,
            [
                call(
                    telegram_admin,
                    b"png",
                    "event ready",
                    filename="event_access_telegram_vk_qr.png",
                    content_type="image/png",
                    keyboard=None,
                ),
                call(
                    vk_admin,
                    b"png",
                    "event ready",
                    filename="event_access_telegram_vk_qr.png",
                    content_type="image/png",
                    keyboard=None,
                ),
            ],
        )

    async def test_admin_copy_failure_does_not_fail_primary_delivery(self):
        primary = ReplyTarget("telegram", 123)
        vk_admin = ReplyTarget("vk", 556972284)
        with patch(
            "admin_notifications.configured_admin_targets",
            return_value=(vk_admin,),
        ), patch(
            "admin_notifications.messenger_delivery.send_photo",
            AsyncMock(side_effect=(True, False)),
        ):
            result = await admin_notifications.send_event_update(
                primary,
                b"png",
                "event ready",
            )

        self.assertTrue(result.primary_delivered)
        self.assertEqual(result.delivered_targets, (primary,))
        self.assertEqual(result.failed_targets, (vk_admin,))

    async def test_event_without_qr_is_also_sent_to_both_admins(self):
        telegram_admin = ReplyTarget("telegram", 123)
        vk_admin = ReplyTarget("vk", 556972284)
        with patch(
            "admin_notifications.configured_admin_targets",
            return_value=(telegram_admin, vk_admin),
        ), patch(
            "admin_notifications.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send:
            result = await admin_notifications.send_event_update(
                telegram_admin,
                None,
                "Кафе включено",
            )

        self.assertTrue(result.primary_delivered)
        self.assertEqual(result.delivered_targets, (telegram_admin, vk_admin))
        self.assertEqual(
            send.await_args_list,
            [
                call(telegram_admin, "Кафе включено"),
                call(vk_admin, "Кафе включено"),
            ],
        )

    async def test_print_approval_is_sent_with_buttons_to_both_admins(self):
        telegram_admin = ReplyTarget("telegram", 123)
        vk_admin = ReplyTarget("vk", 556972284)
        job_id = "a" * 32
        with patch(
            "admin_notifications.configured_admin_targets",
            return_value=(telegram_admin, vk_admin),
        ), patch(
            "admin_notifications.messenger_delivery.send_photo",
            AsyncMock(return_value=True),
        ) as send:
            result = await admin_notifications.send_print_approval(
                job_id=job_id,
                preview=b"jpg",
                caption="approval",
                telegram_caption="<b>approval</b>",
            )

        self.assertEqual(
            result.delivered_targets,
            (telegram_admin, vk_admin),
        )
        telegram_keyboard = send.await_args_list[0].kwargs["keyboard"]
        vk_keyboard = send.await_args_list[1].kwargs["keyboard"]
        self.assertEqual(send.await_args_list[0].args[2], "<b>approval</b>")
        self.assertEqual(send.await_args_list[0].kwargs["parse_mode"], "HTML")
        self.assertEqual(send.await_args_list[1].args[2], "approval")
        self.assertNotIn("parse_mode", send.await_args_list[1].kwargs)
        self.assertTrue(all(
            job_id in button["callback_data"]
            for button in telegram_keyboard["inline_keyboard"][0]
        ))
        self.assertEqual(
            {
                json.loads(button["action"]["payload"])["job_id"]
                for button in vk_keyboard["buttons"][0]
            },
            {job_id},
        )
        self.assertTrue(all(
            button["action"]["type"] == "callback"
            for button in vk_keyboard["buttons"][0]
        ))

    async def test_vk_send_photo_passes_uploaded_attachment(self):
        with patch(
            "vk_api.upload_message_photo",
            AsyncMock(return_value="photo-123_456"),
        ), patch(
            "vk_api.api_call",
            AsyncMock(return_value=1),
        ) as call:
            await vk_api.send_photo(object(), 123, b"png", "caption")

        self.assertEqual(call.await_args.args[1], "messages.send")
        self.assertEqual(call.await_args.kwargs["peer_id"], 123)
        self.assertEqual(
            call.await_args.kwargs["attachment"],
            "photo-123_456",
        )

    def test_builds_document_attachment_with_access_key(self):
        self.assertEqual(
            vk_api.document_attachment({
                "type": "doc",
                "doc": {
                    "owner_id": -123,
                    "id": 456,
                    "access_key": "doc-key",
                },
            }),
            "doc-123_456_doc-key",
        )

    async def test_vk_send_documents_passes_uploaded_attachments(self):
        documents = [
            (b"log", "photobooth.log", "text/plain"),
            (b"{}", "config.json", "application/json"),
        ]
        with patch(
            "vk_api.upload_message_document",
            AsyncMock(side_effect=("doc-1_10", "doc-1_11")),
        ) as upload, patch(
            "vk_api.api_call",
            AsyncMock(return_value=1),
        ) as call:
            await vk_api.send_documents(object(), 123, documents)

        self.assertEqual(upload.await_count, 2)
        self.assertEqual(call.await_args.args[1], "messages.send")
        self.assertEqual(call.await_args.kwargs["peer_id"], 123)
        self.assertEqual(
            call.await_args.kwargs["attachment"],
            "doc-1_10,doc-1_11",
        )

    async def test_vk_document_upload_uses_messages_server_and_docs_save(self):
        class UploadResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return b'{"file":"uploaded-file-token"}'

        class Session:
            def post(self, *_args, **_kwargs):
                return UploadResponse()

        session = Session()
        form = MagicMock()
        with patch(
            "vk_api.api_call",
            AsyncMock(side_effect=(
                {"upload_url": "https://upload.vk.test/document"},
                {
                    "type": "doc",
                    "doc": {"owner_id": -123, "id": 456},
                },
            )),
        ) as api, patch(
            "vk_api.aiohttp.FormData",
            return_value=form,
        ):
            attachment = await vk_api.upload_message_document(
                session,
                123,
                b"log",
                filename="photobooth.log",
                content_type="text/plain",
            )

        self.assertEqual(attachment, "doc-123_456")
        form.add_field.assert_called_once_with(
            "file",
            b"log",
            filename="photobooth.log",
            content_type="text/plain",
        )
        self.assertEqual(
            api.await_args_list,
            [
                call(
                    session,
                    "docs.getMessagesUploadServer",
                    peer_id=123,
                    type="doc",
                ),
                call(
                    session,
                    "docs.save",
                    file="uploaded-file-token",
                    title="photobooth.log",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
