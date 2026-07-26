import asyncio
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image, ImageChops

import app
import print_jobs


def image_payload(size: tuple[int, int], color="royalblue") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG", quality=95)
    return output.getvalue()


class PrintPreviewTests(unittest.TestCase):
    def test_exact_landscape_and_portrait_ratios_need_no_choice(self):
        landscape = print_jobs.build_choice_preview(image_payload((461, 310)))
        portrait = print_jobs.build_choice_preview(image_payload((310, 461)))

        self.assertTrue(landscape.exact_ratio)
        self.assertIsNone(landscape.payload)
        self.assertEqual(landscape.target_size, (3688, 2480))
        self.assertTrue(portrait.exact_ratio)
        self.assertIsNone(portrait.payload)
        self.assertEqual(portrait.target_size, (2480, 3688))

    def test_horizontal_crop_overflow_stacks_tiles(self):
        preview = print_jobs.build_choice_preview(image_payload((1200, 300)))

        self.assertFalse(preview.exact_ratio)
        self.assertEqual(preview.orientation, "landscape")
        self.assertEqual(preview.overflow_axis, "horizontal")
        with Image.open(io.BytesIO(preview.payload)) as collage:
            self.assertGreater(collage.height, collage.width)

    def test_vertical_crop_overflow_places_tiles_side_by_side(self):
        preview = print_jobs.build_choice_preview(image_payload((300, 1200)))

        self.assertFalse(preview.exact_ratio)
        self.assertEqual(preview.orientation, "portrait")
        self.assertEqual(preview.overflow_axis, "vertical")
        with Image.open(io.BytesIO(preview.payload)) as collage:
            self.assertGreater(collage.width, collage.height * 0.8)

    def test_tiles_have_white_paper_and_badge_outlines_at_the_bottom(self):
        paper_size = (200, 100)
        fit_source = Image.new("RGB", (100, 100), "royalblue")
        cover_source = Image.new("RGB", (300, 100), "royalblue")
        try:
            fit = print_jobs._contain(fit_source, paper_size)
            cover, axis = print_jobs._cover_view(cover_source, paper_size)
        finally:
            fit_source.close()
            cover_source.close()
        self.assertEqual(axis, "horizontal")

        visual_area_size = (
            max(fit.width, cover.width),
            max(fit.height, cover.height),
        )
        fit_tile = print_jobs._tile(
            fit,
            visual_area_size,
            paper_size,
            print_jobs._FIT_TITLE,
            print_jobs._FIT_DETAIL,
            "1",
        )
        cover_tile = print_jobs._tile(
            cover,
            visual_area_size,
            paper_size,
            print_jobs._FILL_TITLE,
            print_jobs._FILL_DETAIL,
            "2",
        )
        fit.close()
        cover.close()
        try:
            self.assertEqual(fit_tile.size, cover_tile.size)
            badge_center = (
                print_jobs._TILE_PADDING
                + (visual_area_size[0] - paper_size[0]) // 2
                + print_jobs._BADGE_RADIUS
                + print_jobs._BADGE_INSET,
                print_jobs._TILE_PADDING
                + (visual_area_size[1] - paper_size[1]) // 2
                + paper_size[1]
                - print_jobs._BADGE_RADIUS
                - print_jobs._BADGE_INSET,
            )
            badge_top = (
                badge_center[0],
                badge_center[1] - print_jobs._BADGE_RADIUS,
            )
            self.assertEqual(fit_tile.getpixel(badge_top), print_jobs._PAPER)
            self.assertEqual(cover_tile.getpixel(badge_top), print_jobs._PAPER)
            badge_inside = (
                badge_center[0],
                badge_center[1]
                - print_jobs._BADGE_RADIUS
                + print_jobs._BADGE_OUTLINE_WIDTH
                + 2,
            )
            self.assertEqual(
                fit_tile.getpixel(badge_inside),
                print_jobs._BADGE_FILL,
            )
            self.assertEqual(
                cover_tile.getpixel(badge_inside),
                print_jobs._BADGE_FILL,
            )

            paper_left = (
                print_jobs._TILE_PADDING
                + (visual_area_size[0] - paper_size[0]) // 2
            )
            paper_middle_y = (
                print_jobs._TILE_PADDING
                + (visual_area_size[1] - paper_size[1]) // 2
                + paper_size[1] // 2
            )
            self.assertEqual(
                fit_tile.getpixel((paper_left, paper_middle_y)),
                print_jobs._PAPER,
            )
            self.assertEqual(
                cover_tile.getpixel((paper_left, paper_middle_y)),
                print_jobs._PAPER,
            )
            frame_inside_x = paper_left + print_jobs._PAPER_OUTLINE_WIDTH + 1
            self.assertEqual(
                cover_tile.getpixel((frame_inside_x, paper_middle_y)),
                (65, 105, 225),
            )

            background = Image.new(
                "RGB",
                (fit_tile.width, print_jobs._LABEL_BOTTOM_MARGIN - 1),
                print_jobs._BACKGROUND,
            )
            try:
                whitespace = fit_tile.crop((
                    0,
                    fit_tile.height - background.height,
                    fit_tile.width,
                    fit_tile.height,
                ))
                try:
                    self.assertIsNone(
                        ImageChops.difference(whitespace, background).getbbox())
                finally:
                    whitespace.close()
            finally:
                background.close()
        finally:
            fit_tile.close()
            cover_tile.close()


class PendingPrintTests(unittest.TestCase):
    def test_persists_until_explicitly_deleted(self):
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)):
            job_id = "a" * 32
            print_jobs.save_pending(
                job_id,
                ".jpg",
                b"image",
                {"sender_id": 123},
            )

            payload, metadata = print_jobs.load_pending(job_id)
            self.assertEqual(payload, b"image")
            self.assertEqual(metadata["pending_status"], "awaiting_choice")
            updated = print_jobs.update_pending(job_id, print_mode="fill")
            self.assertEqual(updated["print_mode"], "fill")
            self.assertTrue((Path(tmpdir) / job_id).exists())
            print_jobs.delete_pending(job_id)
            self.assertFalse((Path(tmpdir) / job_id).exists())


class TelegramPrintChoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app._print_callbacks_in_progress.clear()
        app._print_background_tasks.clear()
        self.event_key_patch = patch.object(
            app.event_access,
            "EVENT_KEY",
            "test-event-key",
        )
        self.event_key_patch.start()
        self.addCleanup(self.event_key_patch.stop)

        self.ensure_user = AsyncMock(return_value=101)
        self.create_job = AsyncMock(return_value={
            "outcome": "created",
            "job_id": "job-id",
            "status": "processing",
        })
        self.awaiting_choice = AsyncMock(return_value={
            "outcome": "awaiting_choice",
            "status": "awaiting_choice",
        })
        self.claim_choice = AsyncMock(return_value={
            "outcome": "authorized",
            "status": "authorized",
            "authorization_kind": "allowlist",
        })
        self.cancel_job = AsyncMock(return_value={
            "outcome": "cancelled",
            "status": "cancelled",
        })
        self.fail_job = AsyncMock(return_value={
            "outcome": "failed",
            "status": "failed",
        })
        self.dispatch_job = AsyncMock(return_value={
            "outcome": "dispatching",
            "status": "dispatching",
        })
        database_mocks = {
            "ensure_bot_user": self.ensure_user,
            "create_print_job": self.create_job,
            "mark_print_job_awaiting_choice": self.awaiting_choice,
            "claim_print_job_choice": self.claim_choice,
            "cancel_print_job": self.cancel_job,
            "fail_print_job_before_dispatch": self.fail_job,
            "mark_print_job_dispatching": self.dispatch_job,
        }
        for name, mocked in database_mocks.items():
            started = patch.object(app.database, name, mocked)
            started.start()
            self.addCleanup(started.stop)

    async def test_mismatched_ratio_stays_local_until_user_selects(self):
        message = {
            "message_id": 11,
            "from": {"id": 6634566969, "first_name": "Print"},
            "chat": {"id": 6634566969},
            "photo": [{"file_id": "photo-id", "file_size": 100}],
        }
        payload = image_payload((1000, 1000))
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch("app._tg_download_file", AsyncMock(return_value=payload)), \
             patch("app._tg_send_photo", AsyncMock(return_value=501)) as send_photo, \
             patch("app._tg_send_text", AsyncMock(return_value=True)), \
             patch("app.yadisk_poll.current_event_folder", return_value="event"), \
             patch("app.yadisk_poll.store_print_job", new_callable=AsyncMock) as store, \
             patch("app._send_disk_command", new_callable=AsyncMock) as send_command, \
             patch("app.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = "c" * 32
            handled = await app._tg_handle_print_message(
                object(), "https://telegram.test", message)

        self.assertTrue(handled)
        send_photo.assert_awaited_once()
        sent_caption = send_photo.await_args.args[4]
        sent_keyboard = send_photo.await_args.args[5]["inline_keyboard"]
        self.assertIn("<b>Фото не совпадает с форматом 10×15.</b>", sent_caption)
        self.assertIn("1 — <b>как есть</b> — будут белые поля.", sent_caption)
        self.assertIn(
            "2 — <b>увеличить под размер</b> — обрежутся затемнённые края.",
            sent_caption,
        )
        self.assertNotIn("(", sent_caption)
        self.assertEqual(
            [button["text"] for button in sent_keyboard[0]],
            ["1️⃣ Как есть", "2️⃣ Увеличить"],
        )
        self.assertEqual(
            [button["text"] for button in sent_keyboard[1]],
            ["❌ Отмена"],
        )
        store.assert_not_awaited()
        send_command.assert_not_awaited()

    async def test_exact_ratio_is_submitted_as_fit_without_buttons(self):
        sender_id = 6634566969
        chat_id = -100987654321
        message = {
            "message_id": 12,
            "from": {"id": sender_id, "first_name": "Print"},
            "chat": {"id": chat_id, "type": "group"},
            "photo": [{"file_id": "photo-id", "file_size": 100}],
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 987654321, "first_name": "Original"},
            },
        }
        payload = image_payload((461, 310))
        with patch("app._tg_download_file", AsyncMock(return_value=payload)), \
             patch("app._tg_send_photo", new_callable=AsyncMock) as send_photo, \
             patch("app._tg_send_text", AsyncMock(return_value=True)) as send_text, \
             patch("app.yadisk_poll.current_event_folder", return_value="event"), \
             patch("app._submit_print_job", AsyncMock(return_value="e" * 32)) as submit, \
             patch("app.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = "f" * 32
            handled = await app._tg_handle_print_message(
                object(), "https://telegram.test", message)

        self.assertTrue(handled)
        send_photo.assert_not_awaited()
        self.assertEqual(submit.await_args.args[4]["print_mode"], "fit")
        self.assertEqual(submit.await_args.args[1], sender_id)
        self.assertEqual(submit.await_args.args[5], chat_id)
        self.assertEqual(submit.await_args.args[4]["sender_id"], sender_id)
        self.assertTrue(
            all(call.args[2] == chat_id for call in send_text.await_args_list)
        )

    async def test_dispatched_print_is_also_sent_to_group(self):
        job_id = "8" * 32
        command_id = "e" * 32
        payload = image_payload((461, 310))
        metadata = {
            "sender_id": 123,
            "sender_name": "Иван Иванов",
            "username": "ivan",
            "source_filename": "photo.jpg",
            "event_folder": "2026-12-31 Тест ивент",
            "print_mode": "fit",
        }
        calls = []

        async def store(*_args, **_kwargs):
            calls.append("store")
            return {
                "artifact_path": "/event/photo.jpg",
                "info_path": "/event/photo.txt",
            }

        async def send_command(*_args, **_kwargs):
            calls.append("command")
            return command_id

        async def send_group(*_args, **_kwargs):
            calls.append("group")
            return True

        with patch.object(app, "TG_CHAT", "-100123456789"), patch(
            "app.yadisk_poll.store_print_job",
            side_effect=store,
        ), patch(
            "app._send_disk_command",
            side_effect=send_command,
        ), patch(
            "app._send_print_to_group",
            side_effect=send_group,
        ) as send_print, patch("app.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = command_id
            result = await app._submit_print_job(
                job_id,
                123,
                ".jpg",
                payload,
                metadata,
                123,
                object(),
                "https://telegram.test",
            )

        self.assertEqual(result, command_id)
        self.assertEqual(calls, ["store", "command", "group"])
        send_print.assert_awaited_once()
        self.assertEqual(send_print.await_args.kwargs["job_id"], job_id)
        self.assertEqual(send_print.await_args.kwargs["payload"], payload)

    async def test_group_delivery_failure_does_not_cancel_dispatched_print(self):
        job_id = "1" * 32
        command_id = "2" * 32
        metadata = {
            "sender_id": 123,
            "event_folder": "event",
            "print_mode": "fit",
        }
        with patch.object(app, "TG_CHAT", "-100123456789"), patch(
            "app.yadisk_poll.store_print_job",
            AsyncMock(return_value={
                "artifact_path": "/event/photo.jpg",
                "info_path": "/event/photo.txt",
            }),
        ), patch(
            "app._send_disk_command",
            AsyncMock(return_value=command_id),
        ), patch(
            "app._send_print_to_group",
            AsyncMock(return_value=False),
        ), patch("app.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = command_id
            result = await app._submit_print_job(
                job_id,
                123,
                ".jpg",
                b"image",
                metadata,
                123,
                object(),
                "https://telegram.test",
            )

        self.assertEqual(result, command_id)

    async def test_group_print_post_contains_sender_event_and_mode(self):
        job_id = "3" * 32
        payload = image_payload((461, 310))
        metadata = {
            "sender_id": 123,
            "sender_name": "Иван <Иванов>",
            "username": "ivan",
            "source_filename": "photo.jpg",
            "event_folder": "2026-12-31 Тест ивент",
            "print_mode": "fill",
            "telegram_chat": {"id": 123},
        }
        with patch.object(app, "TG_CHAT", "-100123456789"), patch(
            "app.telegram_helpers.photo_jpeg_preview",
            return_value=b"preview",
        ), patch(
            "app._tg_send_photo",
            AsyncMock(return_value=700),
        ) as send_photo:
            delivered = await app._send_print_to_group(
                object(),
                "https://telegram.test",
                job_id=job_id,
                payload=payload,
                metadata=metadata,
            )

        self.assertTrue(delivered)
        self.assertEqual(send_photo.await_args.args[2], "-100123456789")
        self.assertEqual(send_photo.await_args.args[3], b"preview")
        caption = send_photo.await_args.args[4]
        self.assertIn("Фото отправлено на печать", caption)
        self.assertIn("2026-12-31 Тест ивент", caption)
        self.assertIn("Иван &lt;Иванов&gt; (@ivan)", caption)
        self.assertIn("увеличить под размер", caption)
        self.assertEqual(
            send_photo.await_args.kwargs["filename"],
            f"print_{job_id}.jpg",
        )

    async def test_only_sender_can_choose_and_second_click_does_not_resubmit(self):
        owner_id = 6634566969
        other_allowed_id = 5683598562
        job_id = "d" * 32
        callback = {
            "id": "callback-1",
            "data": f"print:fill:{job_id}",
            "from": {"id": owner_id},
            "message": {"message_id": 44, "chat": {"id": owner_id}},
        }
        self.claim_choice.side_effect = [
            {"outcome": "not_owner"},
            {
                "outcome": "authorized",
                "status": "authorized",
                "authorization_kind": "allowlist",
            },
            {
                "outcome": "already_claimed",
                "status": "dispatching",
                "print_mode": "fill",
            },
        ]
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch("app.yadisk_poll.current_event_folder", return_value="event"), \
             patch("app._tg_answer_callback", new_callable=AsyncMock) as answer, \
             patch("app._tg_edit_print_caption", AsyncMock(return_value=True)), \
             patch("app._tg_send_text", AsyncMock(return_value=True)), \
             patch("app._submit_print_job", AsyncMock(return_value="e" * 32)) as submit:
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {"sender_id": owner_id, "event_folder": "event"},
            )

            unauthorized = {
                **callback,
                "id": "callback-other",
                "from": {"id": other_allowed_id},
            }
            self.assertTrue(await app._tg_handle_print_callback(
                object(), "https://telegram.test", unauthorized))
            submit.assert_not_awaited()
            self.assertIn("только отправитель", answer.await_args.args[3])

            self.assertTrue(await app._tg_handle_print_callback(
                object(), "https://telegram.test", callback))
            submit.assert_awaited_once()
            submitted_metadata = submit.await_args.args[4]
            self.assertEqual(submitted_metadata["print_mode"], "fill")

            self.assertTrue(await app._tg_handle_print_callback(
                object(), "https://telegram.test", callback))
            submit.assert_awaited_once()

    async def test_cafe_choice_notifies_user_before_sending_photo_to_admin(self):
        owner_id = 111222333
        job_id = "6" * 32
        callback = {
            "id": "callback-cafe-fill",
            "data": f"print:fill:{job_id}",
            "from": {"id": owner_id},
            "message": {"message_id": 47, "chat": {"id": owner_id}},
        }
        self.claim_choice.return_value = {
            "outcome": "awaiting_authorization",
            "status": "awaiting_authorization",
            "print_mode": "fill",
        }
        calls = []

        async def answer(*_args, **_kwargs):
            calls.append("answer_user")
            return True

        async def edit(*_args, **_kwargs):
            calls.append("edit_user")
            return True

        async def send_payment(*_args, **_kwargs):
            calls.append("send_payment")
            return True

        async def send_admin(*_args, **_kwargs):
            calls.append("send_admin")
            return 501

        telegram = object()
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch("app.yadisk_poll.current_event_folder", return_value="Кафе"), \
             patch("app._tg_answer_callback", side_effect=answer) as answer_mock, \
             patch("app._tg_edit_print_caption", side_effect=edit) as edit_mock, \
             patch("app._tg_send_text", side_effect=send_payment) as send_text, \
             patch("app._send_admin_print_request", side_effect=send_admin):
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {"sender_id": owner_id, "event_folder": "Кафе"},
            )

            self.assertTrue(await app._tg_handle_print_callback(
                telegram, "https://telegram.test", callback))

        self.assertEqual(
            calls,
            ["answer_user", "edit_user", "send_payment", "send_admin"],
        )
        self.assertEqual(answer_mock.await_args.args[3], "Вариант сохранён")
        self.assertEqual(
            edit_mock.await_args.args[4],
            "✅ Выбрано: увеличить под размер, края обрежутся.",
        )
        send_text.assert_awaited_once_with(
            telegram,
            "https://telegram.test",
            owner_id,
            "💳 Оплатите печать администратору.\n"
            "После подтверждения оплаты фото будет добавлено в очередь.",
        )

    async def test_admin_approval_notifies_user_once_before_dispatch(self):
        admin_id = 999
        user_chat_id = 111222333
        owner_id = 444555666
        job_id = "5" * 32
        original_caption = (
            "Новая печать в «Кафе»\n"
            f"Job: {job_id}\n"
            "Выбор: увеличить под размер, края обрежутся\n"
            "Пользователь: Иван (@ivan)\n"
            f"Telegram ID: {owner_id}\n"
            "Файл: telegram_photo.jpg"
        )
        caption_entities = [{"type": "bold", "offset": 0, "length": 22}]
        callback = {
            "id": "callback-admin-approve",
            "data": f"print_admin:approve:{job_id}",
            "from": {"id": admin_id},
            "message": {
                "message_id": 88,
                "chat": {"id": admin_id},
                "caption": original_caption,
                "caption_entities": caption_entities,
            },
        }
        approval = {
            "outcome": "authorized",
            "conversation_id": user_chat_id,
            "choice_message_id": 47,
            "print_mode": "fill",
            "provider_user_id": owner_id,
        }
        calls = []

        async def send_text(*args, **_kwargs):
            calls.append(("send_text", args[2], args[3]))
            return True

        async def edit_caption(*args, **kwargs):
            calls.append(("edit", args[4], kwargs.get("caption_entities")))
            return True

        submit_started = asyncio.Event()
        release_submit = asyncio.Event()

        async def submit(*_args, **_kwargs):
            calls.append(("submit",))
            submit_started.set()
            await release_submit.wait()
            return "e" * 32

        telegram = object()
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch.object(app, "TG_ADMIN", str(admin_id)), \
             patch("app.yadisk_poll.current_event_folder", return_value="Кафе"), \
             patch.object(
                 app.database,
                 "authorize_print_job_by_admin",
                 AsyncMock(return_value=approval),
             ), \
             patch("app._tg_answer_callback", AsyncMock(return_value=True)), \
             patch("app._tg_send_text", side_effect=send_text) as send_text_mock, \
             patch("app._tg_edit_print_caption", side_effect=edit_caption) as edit, \
             patch("app._submit_print_job", side_effect=submit):
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {
                    "sender_id": owner_id,
                    "event_folder": "Кафе",
                    "print_mode": "fill",
                },
            )

            self.assertTrue(await app._tg_handle_print_admin_callback(
                telegram, "https://telegram.test", callback))
            background_tasks = tuple(app._print_background_tasks)
            self.assertEqual(len(background_tasks), 1)
            await asyncio.wait_for(submit_started.wait(), timeout=1)
            self.assertFalse(background_tasks[0].done())
            release_submit.set()
            await asyncio.wait_for(
                asyncio.gather(*background_tasks),
                timeout=1,
            )

        expected_text = (
            "✅ Оплата подтверждена. Ваше фото добавлено в очередь "
            "и скоро будет распечатано."
        )
        self.assertEqual(
            calls,
            [
                (
                    "edit",
                    f"{original_caption}\n\n"
                    "⏳ Печать разрешена. Добавляю фото в очередь…",
                    caption_entities,
                ),
                ("send_text", user_chat_id, expected_text),
                ("submit",),
                (
                    "edit",
                    f"{original_caption}\n\n"
                    "✅ Печать разрешена и передана на будку.",
                    caption_entities,
                ),
            ],
        )
        send_text_mock.assert_awaited_once()
        self.assertEqual(edit.await_count, 2)

    async def test_admin_rejection_updates_card_before_local_cleanup(self):
        admin_id = 999
        user_chat_id = 111222333
        job_id = "4" * 32
        original_caption = "Новая печать в «Кафе»\nПользователь: Иван"
        callback = {
            "id": "callback-admin-reject",
            "data": f"print_admin:reject:{job_id}",
            "from": {"id": admin_id},
            "message": {
                "message_id": 89,
                "chat": {"id": admin_id},
                "caption": original_caption,
            },
        }
        rejected = {
            "outcome": "cancelled",
            "conversation_id": user_chat_id,
        }
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def controlled_to_thread(function, *args, **kwargs):
            if function is print_jobs.delete_pending:
                cleanup_started.set()
                await release_cleanup.wait()
                return None
            return function(*args, **kwargs)

        telegram = object()
        with patch.object(app, "TG_ADMIN", str(admin_id)), patch(
            "app.yadisk_poll.current_event_folder",
            return_value="Кафе",
        ), patch.object(
            app.database,
            "reject_print_job_by_admin",
            AsyncMock(return_value=rejected),
        ), patch(
            "app._tg_answer_callback",
            AsyncMock(return_value=True),
        ) as answer, patch(
            "app._tg_edit_print_caption",
            AsyncMock(return_value=True),
        ) as edit, patch(
            "app._tg_send_text",
            AsyncMock(return_value=True),
        ), patch(
            "app.asyncio.to_thread",
            side_effect=controlled_to_thread,
        ):
            handler = asyncio.create_task(app._tg_handle_print_admin_callback(
                telegram,
                "https://telegram.test",
                callback,
            ))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            self.assertFalse(handler.done())
            answer.assert_awaited_once_with(
                telegram,
                "https://telegram.test",
                "callback-admin-reject",
                "Печать отклонена",
            )
            edit.assert_awaited_once_with(
                telegram,
                "https://telegram.test",
                admin_id,
                89,
                f"{original_caption}\n\n🚫 Печать отклонена администратором.",
                caption_entities=None,
            )
            release_cleanup.set()
            self.assertTrue(await asyncio.wait_for(handler, timeout=1))

    async def test_cancel_removes_pending_without_submitting(self):
        owner_id = 6634566969
        job_id = "b" * 32
        callback = {
            "id": "callback-cancel",
            "data": f"print:cancel:{job_id}",
            "from": {"id": owner_id},
            "message": {"message_id": 45, "chat": {"id": owner_id}},
        }
        telegram = object()
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch("app._tg_answer_callback", new_callable=AsyncMock) as answer, \
             patch("app._tg_edit_print_caption", AsyncMock(return_value=True)) as edit, \
             patch("app._submit_print_job", new_callable=AsyncMock) as submit:
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {"sender_id": owner_id, "event_folder": "event"},
            )

            self.assertTrue(await app._tg_handle_print_callback(
                telegram, "https://telegram.test", callback))

            self.assertFalse((Path(tmpdir) / job_id).exists())

        submit.assert_not_awaited()
        self.assertEqual(answer.await_args.args[3], "Печать отменена")
        edit.assert_awaited_once_with(
            telegram,
            "https://telegram.test",
            owner_id,
            45,
            "🚫 Печать отменена.",
        )

    async def test_cancel_and_fill_callbacks_cannot_run_concurrently(self):
        owner_id = 6634566969
        job_id = "7" * 32
        cancel_callback = {
            "id": "callback-cancel-race",
            "data": f"print:cancel:{job_id}",
            "from": {"id": owner_id},
            "message": {"message_id": 46, "chat": {"id": owner_id}},
        }
        fill_callback = {
            **cancel_callback,
            "id": "callback-fill-race",
            "data": f"print:fill:{job_id}",
        }
        load_started = asyncio.Event()
        release_load = asyncio.Event()

        async def controlled_cancel(**_kwargs):
            load_started.set()
            await release_load.wait()
            return {"outcome": "cancelled", "status": "cancelled"}

        self.cancel_job.side_effect = controlled_cancel

        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch("app._tg_answer_callback", new_callable=AsyncMock) as answer, \
             patch("app._tg_edit_print_caption", AsyncMock(return_value=True)), \
             patch("app._submit_print_job", new_callable=AsyncMock) as submit:
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {"sender_id": owner_id, "event_folder": "event"},
            )

            cancel_task = asyncio.create_task(app._tg_handle_print_callback(
                object(), "https://telegram.test", cancel_callback))
            await asyncio.wait_for(load_started.wait(), timeout=1)
            try:
                self.assertTrue(await app._tg_handle_print_callback(
                    object(), "https://telegram.test", fill_callback))
            finally:
                release_load.set()
            self.assertTrue(await asyncio.wait_for(cancel_task, timeout=1))
            self.assertFalse((Path(tmpdir) / job_id).exists())

        submit.assert_not_awaited()
        answers = [call.args[3] for call in answer.await_args_list]
        self.assertIn("Задание уже обрабатывается", answers)
        self.assertIn("Печать отменена", answers)
        self.assertNotIn(job_id, app._print_callbacks_in_progress)

    async def test_forwarded_photo_replies_to_forwarder_not_original_author(self):
        sender_id = 6634566969
        chat_id = -100123456789
        original_author_id = 987654321
        job_id = "9" * 32
        message = {
            "message_id": 77,
            "from": {"id": sender_id, "first_name": "Forwarder"},
            "chat": {"id": chat_id, "type": "group"},
            "photo": [{"file_id": "forwarded-photo", "file_size": 100}],
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": original_author_id, "first_name": "Original"},
            },
            "forward_from": {"id": original_author_id, "first_name": "Original"},
        }
        payload = image_payload((1000, 1000))
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch("app._tg_download_file", AsyncMock(return_value=payload)), \
             patch("app._tg_send_photo", AsyncMock(return_value=502)) as send_photo, \
             patch("app._tg_send_text", AsyncMock(return_value=True)) as send_text, \
             patch("app.yadisk_poll.current_event_folder", return_value="event"), \
             patch("app.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = job_id
            self.assertTrue(await app._tg_handle_print_message(
                object(), "https://telegram.test", message))
            _stored_payload, metadata = print_jobs.load_pending(job_id)

        self.assertEqual(send_photo.await_args.args[2], chat_id)
        self.assertEqual(send_photo.await_args.args[6], message["message_id"])
        self.assertEqual(metadata["sender_id"], sender_id)
        self.assertEqual(metadata["telegram_user"]["id"], sender_id)
        self.assertNotEqual(metadata["sender_id"], original_author_id)
        self.assertTrue(send_text.await_args_list)
        self.assertTrue(
            all(call.args[2] == chat_id for call in send_text.await_args_list)
        )

    async def test_forward_origin_does_not_replace_requesting_user(self):
        message = {
            "message_id": 78,
            "from": {"id": 111111111},
            "chat": {"id": 222222222},
            "photo": [{"file_id": "forwarded-photo", "file_size": 100}],
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 6634566969, "first_name": "Allowed"},
            },
        }
        payload = image_payload((1000, 1000))
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
             patch("app._tg_download_file", AsyncMock(return_value=payload)), \
             patch("app._tg_send_photo", AsyncMock(return_value=503)), \
             patch("app._tg_send_text", AsyncMock(return_value=True)), \
             patch("app.yadisk_poll.current_event_folder", return_value="event"):
            self.assertTrue(await app._tg_handle_print_message(
                object(), "https://telegram.test", message))

        self.ensure_user.assert_awaited_once()
        self.assertEqual(
            self.ensure_user.await_args.kwargs["provider_user_id"],
            message["from"]["id"],
        )
        self.assertNotEqual(
            self.ensure_user.await_args.kwargs["provider_user_id"],
            message["forward_origin"]["sender_user"]["id"],
        )


class TelegramPhotoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_caption_uses_html_parse_mode(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {"ok": True, "result": {"message_id": 504}}

        class Telegram:
            def post(self, *_args, **_kwargs):
                return Response()

        form = MagicMock()
        with patch("app.aiohttp.FormData", return_value=form):
            self.assertEqual(504, await app._tg_send_photo(
                Telegram(),
                "https://telegram.test",
                123,
                b"preview",
                "<b>caption</b>",
                {"inline_keyboard": []},
                77,
            ))

        form.add_field.assert_any_call("parse_mode", "HTML")

    async def test_caption_edit_preserves_existing_entities(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Telegram:
            payload = None

            def post(self, *_args, **kwargs):
                self.payload = kwargs["json"]
                return Response()

        telegram = Telegram()
        entities = [{"type": "bold", "offset": 0, "length": 12}]
        self.assertTrue(await app._tg_edit_print_caption(
            telegram,
            "https://telegram.test",
            123,
            456,
            "Исходный текст\n\n✅ Готово",
            caption_entities=entities,
        ))

        self.assertEqual(telegram.payload["caption_entities"], entities)
        self.assertEqual(telegram.payload["reply_markup"], {
            "inline_keyboard": [],
        })


if __name__ == "__main__":
    unittest.main()
