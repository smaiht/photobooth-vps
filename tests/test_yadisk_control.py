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
    def test_validates_embedded_response_document(self):
        command_id = "a" * 32
        response = yadisk_control.validate_response({
            "schema_version": 3,
            "message_type": "command_response",
            "command_id": command_id,
            "command": "send_logs",
            "status": "ok",
            "message": "done",
            "document": "log contents",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": 123,
            },
        }, f"response_{command_id}.json")
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["document"], "log contents")
        self.assertEqual(
            response["reply_target"],
            ReplyTarget("telegram", 123),
        )

        with self.assertRaisesRegex(ValueError, "document"):
            yadisk_control.validate_response({
                **response,
                "document": "x" * (yadisk_control.MAX_RESPONSE_DOCUMENT_SIZE + 1),
            })

        with self.assertRaisesRegex(ValueError, "unexpected response document"):
            yadisk_control.validate_response({
                **response,
                "command": "status",
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


class BoothNoticeValidationTests(unittest.TestCase):
    """Unsolicited booth notices carry no command_id and no reply_target."""

    def _valid(self, **overrides) -> dict:
        notice = {
            "schema_version": 3,
            "message_type": "booth_notice",
            "notice_id": "a" * 32,
            "kind": "camera_config",
            "title": "Конфигурация камеры",
            "text": "ISO=100, Av=16",
            "created_at": "2026-08-09T11:00:00+00:00",
        }
        notice.update(overrides)
        return notice

    def _filename(self, notice_id: str = "a" * 32) -> str:
        return f"notice_20260809T110000Z_{notice_id}.json"

    def test_valid_notice_is_normalized(self):
        notice = yadisk_control.validate_notice(self._valid(), self._filename())
        self.assertEqual(notice["message_type"], "booth_notice")
        self.assertEqual(notice["kind"], "camera_config")
        self.assertEqual(notice["text"], "ISO=100, Av=16")
        self.assertEqual(notice["title"], "Конфигурация камеры")
        # A notice is not a reply, so no target may be inherited from it.
        self.assertNotIn("reply_target", notice)
        self.assertNotIn("command_id", notice)

    def test_missing_title_is_allowed(self):
        notice = yadisk_control.validate_notice(
            self._valid(title=None), self._filename())
        self.assertEqual(notice["title"], "")

    def test_rejects_wrong_schema_and_message_type(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            yadisk_control.validate_notice(self._valid(schema_version=2))
        with self.assertRaisesRegex(ValueError, "message_type"):
            yadisk_control.validate_notice(
                self._valid(message_type="command_response"))

    def test_rejects_invalid_identifier_kind_and_text(self):
        for overrides, message in (
            ({"notice_id": "zz"}, "notice_id"),
            ({"notice_id": 5}, "notice_id"),
            ({"kind": "Camera Config"}, "kind"),
            ({"kind": ""}, "kind"),
            ({"text": ""}, "text"),
            ({"text": "   "}, "text"),
            ({"text": None}, "text"),
            ({"title": 5}, "title"),
        ):
            with self.subTest(overrides=overrides), \
                    self.assertRaisesRegex(ValueError, message):
                yadisk_control.validate_notice(self._valid(**overrides))

    def test_rejects_filename_that_does_not_match_the_notice(self):
        with self.assertRaisesRegex(ValueError, "filename"):
            yadisk_control.validate_notice(
                self._valid(), self._filename("b" * 32))
        with self.assertRaisesRegex(ValueError, "filename"):
            yadisk_control.validate_notice(self._valid(), "notice.json")
        with self.assertRaisesRegex(ValueError, "filename"):
            yadisk_control.validate_notice(
                self._valid(), f"response_{'a' * 32}.json")

    def test_long_text_is_truncated_to_the_documented_limit(self):
        notice = yadisk_control.validate_notice(
            self._valid(text="x" * 9000), self._filename())
        self.assertEqual(len(notice["text"]), yadisk_control.MAX_NOTICE_TEXT)


class BoothNoticeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _notice(self, **overrides) -> dict:
        notice = {
            "notice_id": "a" * 32,
            "kind": "booth_status",
            "title": "Фотобудка готова",
            "text": "ISO=100",
        }
        notice.update(overrides)
        return notice

    async def test_notice_is_broadcast_to_all_admin_channels(self):
        delivery = admin_notifications.AdminBroadcastDelivery(
            delivered_targets=(
                ReplyTarget("telegram", "1"), ReplyTarget("vk", "2")),
            failed_targets=(),
        )
        with patch("control_response_service.runtime_config.yadisk_folder",
                   return_value="VPS event"), \
             patch("control_response_service.admin_notifications.send_admin_text",
                   AsyncMock(return_value=delivery)) as send:
            handled = await control_response_service.handle_notice(self._notice())

        self.assertTrue(handled)
        text = send.await_args.args[0]
        self.assertIn("Фотобудка готова", text)
        self.assertIn("ISO=100", text)
        self.assertIn("Event (VPS config_vps.json): VPS event", text)

    async def test_notice_without_title_still_delivers_its_text(self):
        delivery = admin_notifications.AdminBroadcastDelivery(
            delivered_targets=(ReplyTarget("telegram", "1"),),
            failed_targets=(),
        )
        with patch("control_response_service.admin_notifications.send_admin_text",
                   AsyncMock(return_value=delivery)) as send:
            self.assertTrue(
                await control_response_service.handle_notice(
                    self._notice(title="")))
        self.assertIn("ISO=100", send.await_args.args[0])

    async def test_total_failure_keeps_the_message_for_a_retry(self):
        delivery = admin_notifications.AdminBroadcastDelivery(
            delivered_targets=(),
            failed_targets=(ReplyTarget("telegram", "1"),),
        )
        with patch("control_response_service.admin_notifications.send_admin_text",
                   AsyncMock(return_value=delivery)):
            handled = await control_response_service.handle_notice(self._notice())

        self.assertFalse(handled)

    async def test_partial_failure_is_accepted_to_avoid_duplicates(self):
        delivery = admin_notifications.AdminBroadcastDelivery(
            delivered_targets=(ReplyTarget("telegram", "1"),),
            failed_targets=(ReplyTarget("vk", "2"),),
        )
        with patch("control_response_service.admin_notifications.send_admin_text",
                   AsyncMock(return_value=delivery)):
            handled = await control_response_service.handle_notice(self._notice())

        self.assertTrue(handled)


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


class ControlConnectionSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_session_uses_desktop_client_user_agent(self):
        api_session = MagicMock(closed=False)
        transfer_session = MagicMock(closed=False)
        with patch.object(yadisk_control, "_configured", True), \
             patch.object(yadisk_control, "_token", "secret"), \
             patch.object(yadisk_control, "_root", "/control"), \
             patch.object(yadisk_control, "_session", None), \
             patch.object(yadisk_control, "_transfer_session", None), \
             patch("yadisk_control.aiohttp.ClientSession", side_effect=[
                 api_session,
                 transfer_session,
             ]) as client_session, \
             patch("yadisk_control._ensure_directory", AsyncMock()) as ensure:
            self.assertTrue(await yadisk_control._connect())

        headers = client_session.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["Authorization"], "OAuth secret")
        self.assertEqual(
            headers["User-Agent"],
            yadisk_control.YADISK_API_USER_AGENT,
        )
        self.assertEqual(
            [call.args[0] for call in ensure.await_args_list],
            ["/control", "/control/to_booth", "/control/to_vps"],
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


class PrintQueueAdminCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_clear_without_arguments(self):
        self.assertEqual(
            admin_commands.parse("/clear_print_queue@photobooth_bot"),
            ("clear_print_queue", None),
        )

    def test_rejects_clear_arguments(self):
        with self.assertRaisesRegex(ValueError, "Использование"):
            admin_commands.parse("/clear_print_queue strips")

    def test_clear_command_is_listed_in_admin_help(self):
        self.assertIn("/clear_print_queue", admin_commands.HELP_MESSAGE)

    async def test_clear_command_is_forwarded_to_the_booth(self):
        target = ReplyTarget("telegram", 123)
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value={"command_id": "a" * 32}),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as reply:
            await admin_command_service.handle_message(
                target,
                "/clear_print_queue",
            )

        send.assert_awaited_once_with(
            "clear_print_queue",
            target,
            None,
        )
        self.assertIn("Очищаю очереди", reply.await_args.args[1])


class RuntimeDirectoryAdminCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_both_cleanup_commands_without_arguments(self):
        self.assertEqual(
            admin_commands.parse("/clear_photos"),
            ("clear_photos", None),
        )
        self.assertEqual(
            admin_commands.parse("/clear_print_jobs@photobooth_bot"),
            ("clear_print_jobs", None),
        )

    def test_rejects_cleanup_arguments_and_lists_commands_in_help(self):
        for command in ("/clear_photos", "/clear_print_jobs"):
            with self.subTest(command=command):
                self.assertIn(command, admin_commands.HELP_MESSAGE)
                with self.assertRaisesRegex(ValueError, "Использование"):
                    admin_commands.parse(f"{command} anything")

    async def test_cleanup_command_is_forwarded_to_the_booth(self):
        target = ReplyTarget("vk", 456)
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value={"command_id": "a" * 32}),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as reply:
            await admin_command_service.handle_message(
                target,
                "/clear_print_jobs",
            )

        send.assert_awaited_once_with("clear_print_jobs", target, None)
        self.assertIn("photos_print_jobs", reply.await_args.args[1])


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

    def test_help_lists_every_supported_camera_setting(self):
        expected = {
            "image_quality", "ae_mode", "shutter_type", "av", "tv", "iso",
            "white_balance", "color_temperature", "picture_style",
            "evf_af_mode", "af_mode", "subject_tracking", "evf_view_type",
            "continuous_af", "eye_detection_af", "focus_before_capture",
            "focus_delay", "disable_auto_power_off", "min_free_disk_gib",
            "evf_keep_camera_screen", "drive_mode", "color_space",
            "lock_camera_ui", "lock_mode_dial",
        }
        self.assertEqual(set(admin_commands.CAMERA_SETTING_FIELDS), expected)
        for field in admin_commands.CAMERA_SETTING_FIELDS:
            with self.subTest(field=field):
                self.assertIn(
                    f"/{field} <значение>",
                    admin_commands.HELP_MESSAGE,
                )

    def test_non_camera_field_is_forwarded_for_booth_allowlist_check(self):
        self.assertEqual(
            admin_commands.parse("/photo_choice_default_with_frame true"),
            (
                "set_app_config",
                {
                    "field": "photo_choice_default_with_frame",
                    "value": "true",
                },
            ),
        )
        self.assertIn("_admin_editable_fields", admin_commands.HELP_MESSAGE)

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

    async def test_booth_rejection_is_delivered_once_and_not_retried(self):
        """A rejected value is a final verdict, not a delivery failure."""
        response = {
            "schema_version": 3,
            "message_type": "command_response",
            "command_id": "b" * 32,
            "command": "set_camera_config",
            "status": "error",
            "message": "iso: значение 101 недопустимо",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        item = {
            "name": f"response_{'b' * 32}.json",
            "path": f"disk:/bus/to_vps/response_{'b' * 32}.json",
        }
        yadisk_poll._state = {"handled_messages": []}
        yadisk_poll._retry_after.clear()
        yadisk_poll._failures.clear()

        with patch(
            "control_response_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text, patch(
            "yadisk_poll._delete_inbox_message", AsyncMock(return_value=True),
        ) as delete:
            handled = await yadisk_poll._process_response(
                item, response, control_response_service.handle,
            )

        self.assertTrue(handled)
        self.assertIn("101", send_text.await_args.args[1])
        self.assertTrue(send_text.await_args.args[1].startswith("❌"))
        # The message leaves the inbox, so the poller never sees it again.
        delete.assert_awaited_once()
        self.assertNotIn(item["name"], yadisk_poll._retry_after)

    async def test_rejection_is_retried_only_when_delivery_fails(self):
        response = {
            "schema_version": 3,
            "message_type": "command_response",
            "command_id": "c" * 32,
            "command": "set_camera_config",
            "status": "error",
            "message": "iso: значение 101 недопустимо",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        item = {
            "name": f"response_{'c' * 32}.json",
            "path": f"disk:/bus/to_vps/response_{'c' * 32}.json",
        }
        yadisk_poll._state = {"handled_messages": []}

        with patch(
            "control_response_service.messenger_delivery.send_text",
            AsyncMock(return_value=False),
        ), patch(
            "yadisk_poll._delete_inbox_message", AsyncMock(return_value=True),
        ) as delete:
            handled = await yadisk_poll._process_response(
                item, response, control_response_service.handle,
            )

        self.assertFalse(handled)
        delete.assert_not_awaited()
        self.assertEqual(yadisk_poll._state["handled_messages"], [])

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


class AppConfigCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_direct_field_command_to_booth(self):
        target = ReplyTarget("telegram", 123)
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                target,
                "/photo_choice_default_with_frame true",
            )

        send.assert_awaited_once_with(
            "set_app_config",
            target,
            {
                "field": "photo_choice_default_with_frame",
                "value": "true",
            },
        )
        self.assertIn("ожидаю подтверждение", send_text.await_args.args[1])


class TemplatePackCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_pack_name_and_lists_command_in_help(self):
        self.assertEqual(
            admin_commands.parse("/template birthday"),
            ("set_template_pack", {"name": "birthday"}),
        )
        self.assertEqual(
            admin_commands.parse(
                "/template@photobooth_bot PARK_UNIVERSAL"
            ),
            ("set_template_pack", {"name": "park_universal"}),
        )
        self.assertIn("/template <pack>", admin_commands.HELP_MESSAGE)

    def test_rejects_missing_or_unsafe_pack_name(self):
        for text in (
            "/template",
            "/template two names",
            "/template ../birthday",
            "/template _hidden",
            "/template день-рождения",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                admin_commands.parse(text)

    async def test_forwards_pack_name_to_booth(self):
        target = ReplyTarget("telegram", 123)
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                target,
                "/template birthday",
            )

        send.assert_awaited_once_with(
            "set_template_pack",
            target,
            {"name": "birthday"},
        )
        self.assertIn("birthday", send_text.await_args.args[1])
        self.assertIn("подтверждение будки", send_text.await_args.args[1])


class CameraPresetCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_light_preset_and_bot_suffix(self):
        self.assertEqual(
            admin_commands.parse("/light evening"),
            ("set_camera_preset", {"name": "evening"}),
        )
        self.assertEqual(
            admin_commands.parse("/light@photobooth_bot INDOOR_DARK"),
            ("set_camera_preset", {"name": "indoor_dark"}),
        )

    def test_rejects_missing_or_malformed_preset_name(self):
        for text in (
            "/light",
            "/light two names",
            "/light bad-name",
            "/light _hidden",
            "/light солнце",
            "/light missing",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                admin_commands.parse(text)

    def test_help_lists_every_preset_as_a_ready_command(self):
        self.assertEqual(
            dict(admin_commands.LIGHT_PRESETS),
            {
                "sun": "Яркое солнце",
                "cloudy": "Улица, пасмурно",
                "evening": "Улица, тёмный вечер",
                "indoor": "Помещение со светом",
                "indoor_dark": "Помещение, темно",
            },
        )
        for name, label in admin_commands.LIGHT_PRESETS:
            with self.subTest(name=name):
                self.assertIn(
                    f"/light {name} — {label}",
                    admin_commands.HELP_MESSAGE,
                )
        self.assertNotIn("/light <имя>", admin_commands.HELP_MESSAGE)
        self.assertLessEqual(len(admin_commands.HELP_MESSAGE), 4096)

    async def test_forwards_preset_name_to_booth(self):
        target = ReplyTarget("telegram", 123)
        with patch(
            "admin_command_service.yadisk_control.send_command",
            AsyncMock(return_value="a" * 32),
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                target,
                "/light cloudy",
            )

        send.assert_awaited_once_with(
            "set_camera_preset",
            target,
            {"name": "cloudy"},
        )
        self.assertIn("Пресет света: cloudy", send_text.await_args.args[1])
        self.assertIn("подтверждение будки", send_text.await_args.args[1])

    async def test_missing_name_returns_usage_without_disk_command(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            new_callable=AsyncMock,
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/light",
            )

        send.assert_not_awaited()
        for name, _label in admin_commands.LIGHT_PRESETS:
            self.assertIn(f"/light {name}", send_text.await_args.args[1])


class RemovedUpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_update_is_not_a_named_or_camera_command(self):
        self.assertNotIn("/update", admin_commands.KNOWN_COMMANDS)
        self.assertNotIn("/update", admin_commands.HELP_MESSAGE)
        self.assertIsNone(admin_commands.parse("/update"))

    async def test_update_returns_help_without_touching_yandex_disk(self):
        with patch(
            "admin_command_service.yadisk_control.send_command",
            new_callable=AsyncMock,
        ) as send, patch(
            "admin_command_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send_text:
            await admin_command_service.handle_message(
                ReplyTarget("telegram", 123),
                "/update",
            )

        send.assert_not_awaited()
        send_text.assert_awaited_once_with(
            ReplyTarget("telegram", 123),
            admin_commands.HELP_MESSAGE,
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

    async def test_status_response_includes_the_vps_event(self):
        response = {
            "status": "ok",
            "command": "status",
            "message": "Event (booth): Booth event",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        with patch(
            "control_response_service.runtime_config.yadisk_folder",
            return_value="VPS event",
        ), patch(
            "control_response_service.messenger_delivery.send_text",
            AsyncMock(return_value=True),
        ) as send:
            self.assertTrue(await control_response_service.handle(response))

        text = send.await_args.args[1]
        self.assertIn("Event (booth): Booth event", text)
        self.assertIn("Event (VPS config_vps.json): VPS event", text)


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

    async def test_embedded_document_is_delivered_without_extra_text(self):
        response = {
            "status": "ok",
            "command": "send_logs",
            "message": "Лог готов",
            "document": "log",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "5683598562",
            },
        }
        with patch("control_response_service.messenger_delivery.send_document",
                   AsyncMock(return_value=True)) as send_document, \
             patch("control_response_service.messenger_delivery.send_text",
                   new_callable=AsyncMock) as send_text:
            self.assertTrue(await control_response_service.handle(response))

        send_document.assert_awaited_once_with(
            ReplyTarget("telegram", 5683598562),
            b"log",
            "photobooth.log",
            "text/plain",
        )
        send_text.assert_not_awaited()

    async def test_log_response_can_be_delivered_to_vk(self):
        response = {
            "status": "ok",
            "command": "send_logs",
            "message": "Лог готов",
            "document": "log",
            "reply_target": {
                "provider": "vk",
                "conversation_id": "556972284",
            },
        }
        with patch(
            "control_response_service.messenger_delivery.send_document",
            AsyncMock(return_value=True),
        ) as send:
            self.assertTrue(await control_response_service.handle(response))

        send.assert_awaited_once_with(
            ReplyTarget("vk", 556972284),
            b"log",
            "photobooth.log",
            "text/plain",
        )


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

    async def test_control_response_delivers_embedded_booth_and_vps_configs(self):
        response = {
            "status": "ok",
            "command": "get_config",
            "message": "Конфиги готовы",
            "document": "===== config_app.json =====\n{}\n",
            "reply_target": {
                "provider": "telegram",
                "conversation_id": "123",
            },
        }
        export = b"===== config_app.json =====\n{}\n"
        vps_config = b'{"yadisk_folder":"event"}\n'
        with patch("control_response_service.runtime_config.read_bytes",
                   return_value=vps_config), \
             patch("control_response_service.messenger_delivery.send_documents",
                   AsyncMock(return_value=True)) as send, \
             patch("control_response_service.messenger_delivery.send_document",
                   new_callable=AsyncMock) as send_document:
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


if __name__ == "__main__":
    unittest.main()
