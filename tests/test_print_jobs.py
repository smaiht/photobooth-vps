import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from PIL import Image

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
             patch("app._tg_send_photo", AsyncMock(return_value=True)) as send_photo, \
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
        store.assert_not_awaited()
        send_command.assert_not_awaited()

    async def test_exact_ratio_is_submitted_as_fit_without_buttons(self):
        message = {
            "message_id": 12,
            "from": {"id": 6634566969, "first_name": "Print"},
            "chat": {"id": 6634566969},
            "photo": [{"file_id": "photo-id", "file_size": 100}],
        }
        payload = image_payload((461, 310))
        with patch("app._tg_download_file", AsyncMock(return_value=payload)), \
             patch("app._tg_send_photo", new_callable=AsyncMock) as send_photo, \
             patch("app._tg_send_text", AsyncMock(return_value=True)), \
             patch("app.yadisk_poll.current_event_folder", return_value="event"), \
             patch("app._submit_print_job", AsyncMock(return_value="e" * 32)) as submit, \
             patch("app.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = "f" * 32
            handled = await app._tg_handle_print_message(
                object(), "https://telegram.test", message)

        self.assertTrue(handled)
        send_photo.assert_not_awaited()
        self.assertEqual(submit.await_args.args[4]["print_mode"], "fit")

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
        with TemporaryDirectory() as tmpdir, \
             patch.object(print_jobs, "PENDING_ROOT", Path(tmpdir)), \
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


if __name__ == "__main__":
    unittest.main()
