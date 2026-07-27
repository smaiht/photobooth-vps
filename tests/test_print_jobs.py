import asyncio
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image, ImageChops

import telegram_api
import print_jobs
from messaging import ReplyTarget


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




class TelegramPhotoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_caption_uses_html_parse_mode(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return json.dumps({
                    "ok": True,
                    "result": {"message_id": 504},
                }).encode()

        class Telegram:
            def post(self, *_args, **_kwargs):
                return Response()

        form = MagicMock()
        with patch("telegram_api.aiohttp.FormData", return_value=form):
            self.assertEqual(504, await telegram_api.send_photo(
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

            async def read(self):
                return b'{"ok":true,"result":true}'

        class Telegram:
            payload = None

            def post(self, *_args, **kwargs):
                self.payload = kwargs["json"]
                return Response()

        telegram = Telegram()
        entities = [{"type": "bold", "offset": 0, "length": 12}]
        self.assertTrue(await telegram_api.edit_print_caption(
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
