import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import ai_flow
import print_flow
import print_jobs
import telegram_print
import vk_print


def image_bytes(size=(640, 480)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, "JPEG")
    return output.getvalue()


def truncated_png_bytes(size=(640, 480)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, "PNG")
    payload = output.getvalue()
    return payload[: len(payload) // 2]


class AiCaptionTests(unittest.TestCase):
    def test_only_explicit_ai_captions_enable_the_flow(self):
        for caption in ("AI", " ai ", "ИИ", "ии", "изменить", "ИЗМЕНИТЬ"):
            with self.subTest(caption=caption):
                self.assertTrue(ai_flow.is_ai_caption(caption))
        for caption in (None, "", "/ai", "AI: poster", "измени фото", "print"):
            with self.subTest(caption=caption):
                self.assertFalse(ai_flow.is_ai_caption(caption))

    def test_ai_result_has_an_exact_print_size(self):
        with Image.open(io.BytesIO(ai_flow._print_ready_jpeg(image_bytes()))) as result:
            self.assertEqual(result.size, print_jobs.LANDSCAPE_PRINT_SIZE)

    def test_output_size_uses_portrait_only_for_vertical_sources(self):
        for size, expected in (
            ((640, 480), "1536x1024"),
            ((640, 640), "1536x1024"),
            ((480, 640), "1024x1536"),
        ):
            with self.subTest(size=size):
                source = image_bytes(size)
                prepared, suffix, output_size = ai_flow._prepare_input(
                    source,
                    ".jpg",
                )
                self.assertEqual(prepared, source)
                self.assertEqual(suffix, ".jpg")
                self.assertEqual(output_size, expected)


class AiUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_cafe_guest_falls_back_to_normal_print_before_download(self):
        upload = ai_flow.AiUpload(
            user=print_flow.PrintUser(
                provider="telegram",
                provider_user_id=123,
                conversation_id=123,
            ),
            suffix=".jpg",
            download=AsyncMock(return_value=image_bytes()),
        )
        ui = SimpleNamespace(send_text=AsyncMock(return_value=True))
        with patch(
            "ai_flow.event_access.current_event",
            return_value=("cafe", None, True),
        ), patch(
            "ai_flow.runtime_config.ai_image_edit_settings",
        ) as settings:
            handled = await ai_flow.handle_upload(upload, ui)

        self.assertFalse(handled)
        upload.download.assert_not_awaited()
        settings.assert_not_called()

    async def test_cafe_allowlisted_user_keeps_ai_route(self):
        upload = ai_flow.AiUpload(
            user=print_flow.PrintUser(
                provider="telegram",
                provider_user_id=123,
                conversation_id=123,
                allowlisted=True,
            ),
            suffix=".jpg",
            download=AsyncMock(return_value=image_bytes()),
        )
        ui = SimpleNamespace(send_text=AsyncMock(return_value=True))
        with patch(
            "ai_flow.event_access.current_event",
            return_value=("cafe", None, True),
        ), patch(
            "ai_flow.runtime_config.ai_image_edit_settings",
            return_value={"enabled": False},
        ):
            handled = await ai_flow.handle_upload(upload, ui)

        self.assertTrue(handled)
        ui.send_text.assert_awaited_once_with(
            upload.user,
            "❌ AI-обработка сейчас отключена.",
        )

    async def test_menu_receives_every_template_from_config(self):
        templates = tuple(
            {
                "id": f"effect_{index}",
                "button": f"Effect {index}",
                "prompt": f"Prompt {index}",
            }
            for index in range(6)
        )
        user = print_flow.PrintUser(
            provider="telegram",
            provider_user_id=123,
            conversation_id=123,
        )
        upload = ai_flow.AiUpload(
            user=user,
            suffix=".jpg",
            download=AsyncMock(return_value=image_bytes()),
        )
        ui = SimpleNamespace(
            send_text=AsyncMock(return_value=True),
            send_ai_choice=AsyncMock(return_value=77),
        )
        with TemporaryDirectory() as tmpdir, patch.object(
            ai_flow,
            "AI_JOBS_ROOT",
            Path(tmpdir),
        ), patch(
            "ai_flow.runtime_config.ai_image_edit_settings",
            return_value={
                "enabled": True,
                "templates": templates,
            },
        ), patch(
            "ai_flow.event_access.current_event",
            return_value=("event", "token", False),
        ), patch(
            "ai_flow.print_flow.ensure_user",
            AsyncMock(return_value=1),
        ), patch(
            "ai_flow.print_flow.user_has_access",
            AsyncMock(return_value=True),
        ), patch(
            "ai_flow.database.create_ai_image_job",
            AsyncMock(return_value={
                "outcome": "created",
                "stale_job_ids": [],
            }),
        ), patch(
            "ai_flow.database.mark_ai_image_job_awaiting_template",
            AsyncMock(return_value={"outcome": "awaiting_template"}),
        ):
            self.assertTrue(await ai_flow.handle_upload(upload, ui))

        self.assertEqual(ui.send_ai_choice.await_args.args[3], templates)


class AiOwnPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_becomes_prompt_only_after_own_prompt_button(self):
        user = print_flow.PrintUser(
            provider="telegram",
            provider_user_id=123,
            conversation_id=123,
        )
        job_id = "f" * 32
        ui = SimpleNamespace(
            send_text=AsyncMock(return_value=True),
            acknowledge=AsyncMock(),
        )
        with patch(
            "ai_flow.runtime_config.ai_image_edit_settings",
            return_value={"enabled": True, "templates": ()},
        ), patch(
            "ai_flow.event_access.current_event",
            return_value=("event", "token", False),
        ), patch(
            "ai_flow.print_flow.user_has_access",
            AsyncMock(return_value=True),
        ), patch(
            "ai_flow.print_flow.ensure_user",
            AsyncMock(return_value=1),
        ), patch(
            "ai_flow.database.queue_ai_image_job",
            AsyncMock(return_value={"outcome": "queued"}),
        ) as queue:
            self.assertFalse(await ai_flow.handle_text(user, "спасибо", ui))
            await ai_flow.handle_action(
                ai_flow.AiAction(
                    user=user,
                    action="template",
                    template_id=ai_flow.CUSTOM_TEMPLATE_ID,
                    job_id=job_id,
                ),
                ui,
            )
            queue.assert_not_awaited()
            self.assertTrue(
                await ai_flow.handle_text(user, "сделай нас космонавтами", ui)
            )
            self.assertFalse(await ai_flow.handle_text(user, "ещё текст", ui))

        queue.assert_awaited_once()
        self.assertEqual(queue.await_args.kwargs["job_id"], job_id)
        self.assertEqual(
            queue.await_args.kwargs["prompt"],
            "сделай нас космонавтами",
        )


class AiWorkerTests(unittest.IsolatedAsyncioTestCase):
    def job(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "event_name": "event",
            "conversation_id": "123",
            "source_suffix": ".jpg",
            "result_suffix": None,
            "template_id": "effect",
            "template_label": "Effect",
            "prompt": "Prompt",
            "provider": "telegram",
            "provider_user_id": "123",
        }

    async def test_generated_image_is_delivered_print_ready(self):
        job_id = "c" * 32
        with TemporaryDirectory() as tmpdir, patch.object(
            ai_flow,
            "AI_JOBS_ROOT",
            Path(tmpdir),
        ), patch(
            "ai_flow.event_access.current_event",
            return_value=("event", "token", False),
        ), patch(
            "ai_flow.image_api.edit_image",
            AsyncMock(return_value=image_bytes((1536, 1024))),
        ) as edit_image, patch(
            "ai_flow.database.mark_ai_image_job_ready",
            AsyncMock(return_value={"outcome": "ready"}),
        ), patch(
            "ai_flow.database.mark_ai_image_job_delivered",
            AsyncMock(),
        ), patch(
            "ai_flow.messenger_delivery.send_photo",
            AsyncMock(return_value=True),
        ) as send_photo, patch(
            "ai_flow._fail_worker_job",
            AsyncMock(),
        ) as failed:
            ai_flow._save_source(job_id, ".jpg", image_bytes())
            await ai_flow._process_job(self.job(job_id))
            result = ai_flow._load_result(job_id, ".jpg")

        self.assertEqual(edit_image.await_args.kwargs["prompt"], "Prompt")
        self.assertEqual(edit_image.await_args.kwargs["size"], "1536x1024")
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, print_jobs.LANDSCAPE_PRINT_SIZE)
        send_photo.assert_awaited_once()
        failed.assert_not_awaited()

    async def test_broken_generated_image_fails_the_job(self):
        job_id = "d" * 32
        with TemporaryDirectory() as tmpdir, patch.object(
            ai_flow,
            "AI_JOBS_ROOT",
            Path(tmpdir),
        ), patch(
            "ai_flow.event_access.current_event",
            return_value=("event", "token", False),
        ), patch(
            "ai_flow.image_api.edit_image",
            AsyncMock(return_value=truncated_png_bytes()),
        ), patch(
            "ai_flow.database.mark_ai_image_job_ready",
            AsyncMock(),
        ) as mark_ready, patch(
            "ai_flow._fail_worker_job",
            AsyncMock(),
        ) as failed:
            ai_flow._save_source(job_id, ".jpg", image_bytes())
            await ai_flow._process_job(self.job(job_id))

        mark_ready.assert_not_awaited()
        failed.assert_awaited_once()


class AiAdapterRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_ai_caption_bypasses_normal_print(self):
        message = {
            "message_id": 7,
            "from": {"id": 123},
            "chat": {"id": 123},
            "caption": "ИИ",
            "photo": [{"file_id": "photo", "file_size": 100}],
        }
        with patch(
            "telegram_print.ai_flow.handle_upload",
            AsyncMock(return_value=True),
        ) as ai_upload, patch(
            "telegram_print.print_flow.handle_upload",
            AsyncMock(return_value=True),
        ) as print_upload:
            self.assertTrue(await telegram_print.handle_message(
                object(),
                "https://telegram.test",
                message,
            ))

        ai_upload.assert_awaited_once()
        print_upload.assert_not_awaited()

    async def test_vk_ai_caption_bypasses_normal_print(self):
        message = {
            "from_id": 123,
            "peer_id": 123,
            "text": "изменить",
            "attachments": [{
                "type": "photo",
                "photo": {
                    "id": 1,
                    "owner_id": 2,
                    "sizes": [{
                        "width": 640,
                        "height": 480,
                        "url": "https://vk.test/photo.jpg",
                    }],
                },
            }],
        }
        with patch(
            "vk_print.ai_flow.handle_upload",
            AsyncMock(return_value=True),
        ) as ai_upload, patch(
            "vk_print.print_flow.handle_upload",
            AsyncMock(return_value=True),
        ) as print_upload:
            self.assertTrue(await vk_print.handle_message(object(), message))

        ai_upload.assert_awaited_once()
        print_upload.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
