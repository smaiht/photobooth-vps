import asyncio
import io
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, call, patch

from PIL import Image

import print_flow
import print_jobs
import yadisk_poll
from messaging import ReplyTarget


def image_payload(size: tuple[int, int], color="royalblue") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG", quality=95)
    return output.getvalue()


class FakeUI:
    def __init__(self):
        self.send_text = AsyncMock(return_value=True)
        self.send_choice = AsyncMock(return_value=501)
        self.acknowledge = AsyncMock()
        self.update_choice = AsyncMock()
        self.update_admin = AsyncMock()


def user(
    provider="telegram",
    user_id=123,
    *,
    allowlisted=True,
    is_admin=False,
) -> print_flow.PrintUser:
    return print_flow.PrintUser(
        provider=provider,
        provider_user_id=user_id,
        conversation_id=user_id,
        source_message_id=77,
        username="guest",
        first_name="Иван",
        last_name="Иванов",
        allowlisted=allowlisted,
        is_admin=is_admin,
        metadata={"source_filename": f"{provider}_photo.jpg"},
    )


def upload(
    owner: print_flow.PrintUser,
    size=(1000, 1000),
) -> tuple[print_flow.PrintUpload, AsyncMock]:
    download = AsyncMock(return_value=image_payload(size))
    return (
        print_flow.PrintUpload(
            user=owner,
            suffix=".jpg",
            declared_size=100,
            download=download,
        ),
        download,
    )


class PrintJobStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_stores_image_and_metadata_inside_one_job_folder(self):
        job_id = "a" * 32
        ensure = AsyncMock(return_value=True)
        upload_file = AsyncMock()
        with patch.object(
            yadisk_poll, "_folder", "/event",
        ), patch.object(
            yadisk_poll, "_connect", AsyncMock(return_value=True),
        ), patch.object(
            yadisk_poll, "_ensure_directory", ensure,
        ), patch.object(
            yadisk_poll, "_upload_print_file", upload_file,
        ):
            result = await yadisk_poll.store_print_job(
                job_id,
                123,
                ".jpg",
                b"image",
                {"provider": "telegram"},
            )

        job_folder = result["job_folder"]
        basename = job_folder.rsplit("/", 1)[-1]
        self.assertEqual(
            job_folder.rsplit("/", 1)[0],
            "/event_by_sessions/0000_print_jobs",
        )
        self.assertEqual(result["artifact_path"], f"{job_folder}/{basename}.jpg")
        self.assertEqual(result["info_path"], f"{job_folder}/{basename}.txt")
        self.assertIn(call(job_folder), ensure.await_args_list)
        self.assertEqual(upload_file.await_args_list[0].args[0], result["artifact_path"])
        self.assertEqual(upload_file.await_args_list[1].args[0], result["info_path"])


class SharedPrintFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        print_flow._actions_in_progress.clear()
        print_flow._background_tasks.clear()

        self.ensure_user = AsyncMock(return_value=101)
        self.has_access = AsyncMock(return_value=True)
        self.create_job = AsyncMock(return_value={
            "outcome": "created",
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
        mocks = {
            "ensure_bot_user": self.ensure_user,
            "user_has_current_start_parameter": self.has_access,
            "create_print_job": self.create_job,
            "mark_print_job_awaiting_choice": self.awaiting_choice,
            "claim_print_job_choice": self.claim_choice,
            "cancel_print_job": self.cancel_job,
            "fail_print_job_before_dispatch": self.fail_job,
            "mark_print_job_dispatching": self.dispatch_job,
        }
        for name, mocked in mocks.items():
            started = patch.object(print_flow.database, name, mocked)
            started.start()
            self.addCleanup(started.stop)
        event_patch = patch.object(
            print_flow.event_access,
            "current_event",
            return_value=("2026-08-17 Свадьба", "event-token", False),
        )
        event_patch.start()
        self.addCleanup(event_patch.stop)
        self.send_approval = AsyncMock(return_value=(
            print_flow.admin_notifications.AdminBroadcastDelivery(
                delivered_targets=(ReplyTarget("telegram", 1),),
                failed_targets=(),
            )
        ))
        approval_patch = patch.object(
            print_flow.admin_notifications,
            "send_print_approval",
            self.send_approval,
        )
        approval_patch.start()
        self.addCleanup(approval_patch.stop)
        self.send_admin_status = AsyncMock(return_value=(
            print_flow.admin_notifications.AdminBroadcastDelivery(
                delivered_targets=(),
                failed_targets=(),
            )
        ))
        admin_status_patch = patch.object(
            print_flow.admin_notifications,
            "send_admin_text",
            self.send_admin_status,
        )
        admin_status_patch.start()
        self.addCleanup(admin_status_patch.stop)

    async def test_mismatched_image_stays_local_and_asks_for_choice(self):
        owner = user("telegram")
        incoming, download = upload(owner)
        ui = FakeUI()

        async def download_after_status():
            ui.send_text.assert_awaited_once_with(
                owner,
                "⏳ Ваше фото обрабатывается, подождите немного…",
            )
            return image_payload((1000, 1000))

        download.side_effect = download_after_status
        job_id = "a" * 32
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(
            print_flow.uuid,
            "uuid4",
        ) as uuid4, patch.object(
            print_flow,
            "submit_print_job",
            new_callable=AsyncMock,
        ) as submit:
            uuid4.return_value.hex = job_id
            handled = await print_flow.handle_upload(incoming, ui)
            stored_payload, metadata = print_jobs.load_pending(job_id)

        self.assertTrue(handled)
        download.assert_awaited_once()
        submit.assert_not_awaited()
        ui.send_choice.assert_awaited_once()
        ui.send_text.assert_awaited_once_with(
            owner,
            "⏳ Ваше фото обрабатывается, подождите немного…",
        )
        self.awaiting_choice.assert_awaited_once_with(
            job_id=job_id,
            choice_message_id=501,
        )
        self.assertEqual(stored_payload, image_payload((1000, 1000)))
        self.assertEqual(metadata["provider"], "telegram")
        self.assertEqual(metadata["reply_target"], owner.target.to_dict())

    async def test_preview_transport_failure_is_not_called_an_invalid_photo(self):
        owner = user("vk", 124)
        incoming, _download = upload(owner, (1000, 1000))
        ui = FakeUI()
        ui.send_choice.side_effect = RuntimeError("VK upload unavailable")
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(
            print_flow.uuid,
            "uuid4",
        ) as uuid4, patch.object(
            print_flow.log,
            "exception",
        ):
            uuid4.return_value.hex = "d" * 32
            self.assertTrue(await print_flow.handle_upload(incoming, ui))

        self.fail_job.assert_awaited_once()
        user_message = ui.send_text.await_args_list[-1].args[1]
        self.assertIn("Не удалось отправить превью", user_message)
        self.assertNotIn("Фото не принято", user_message)

    async def test_exact_vk_image_uses_same_auto_dispatch_path(self):
        owner = user("vk", 321)
        incoming, _download = upload(owner, (461, 310))
        ui = FakeUI()
        job_id = "b" * 32
        with patch.object(print_flow.uuid, "uuid4") as uuid4, patch.object(
            print_flow,
            "submit_print_job",
            AsyncMock(return_value="e" * 32),
        ) as submit:
            uuid4.return_value.hex = job_id
            self.assertTrue(await print_flow.handle_upload(incoming, ui))

        ui.send_choice.assert_not_awaited()
        submit.assert_awaited_once()
        kwargs = submit.await_args.kwargs
        self.assertEqual(kwargs["reply_target"], ReplyTarget("vk", 321))
        self.assertEqual(kwargs["metadata"]["provider"], "vk")
        self.assertEqual(kwargs["metadata"]["print_mode"], "fit")
        self.assertEqual(
            ui.send_text.await_args_list,
            [
                call(
                    owner,
                    "⏳ Ваше фото обрабатывается, подождите немного…",
                ),
                call(
                    owner,
                    "✅ Ваше фото добавлено в очередь и скоро будет распечатано.",
                ),
            ],
        )

    async def test_vk_guest_access_uses_vk_identity_and_event_token(self):
        owner = user("vk", 456, allowlisted=False)
        incoming, _download = upload(owner)
        ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(print_flow.uuid, "uuid4") as uuid4:
            uuid4.return_value.hex = "c" * 32
            await print_flow.handle_upload(incoming, ui)

        self.has_access.assert_awaited_once_with(
            provider="vk",
            provider_user_id=456,
            start_parameter="event-token",
        )

    async def test_authorized_vk_choice_dispatches_with_vk_reply_target(self):
        owner = user("vk", 654)
        job_id = "d" * 32
        action = print_flow.PrintAction(
            user=owner,
            action="fill",
            job_id=job_id,
        )
        ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(
            print_flow,
            "submit_print_job",
            AsyncMock(return_value="f" * 32),
        ) as submit:
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {
                    "provider": "vk",
                    "sender_id": 654,
                    "event_folder": "2026-08-17 Свадьба",
                },
            )
            self.assertTrue(await print_flow.handle_choice(action, ui))
            self.assertFalse((Path(tmpdir) / job_id).exists())

        submit.assert_awaited_once()
        self.assertEqual(
            submit.await_args.kwargs["reply_target"],
            ReplyTarget("vk", 654),
        )
        self.assertEqual(
            submit.await_args.kwargs["metadata"]["print_choice"],
            "vk_button",
        )
        ui.update_choice.assert_awaited_once_with(
            action,
            "✅ Выбрано: увеличить под размер, края обрежутся.",
        )
        ui.send_text.assert_awaited_once_with(
            owner,
            "✅ Ваше фото добавлено в очередь и скоро будет распечатано.",
        )

    async def test_choice_is_rejected_for_another_user(self):
        self.claim_choice.return_value = {"outcome": "not_owner"}
        action = print_flow.PrintAction(
            user=user("telegram", 999),
            action="fit",
            job_id="e" * 32,
        )
        ui = FakeUI()
        with patch.object(
            print_flow,
            "submit_print_job",
            new_callable=AsyncMock,
        ) as submit:
            self.assertTrue(await print_flow.handle_choice(action, ui))

        submit.assert_not_awaited()
        self.assertIn("только отправитель", ui.acknowledge.await_args.args[1])

    async def test_cafe_choice_uses_cross_provider_admin_delivery(self):
        print_flow.event_access.current_event.return_value = (
            "Кафе",
            "event-token",
            True,
        )
        self.claim_choice.return_value = {
            "outcome": "awaiting_authorization",
            "status": "awaiting_authorization",
            "print_mode": "fill",
        }
        owner = user("vk", 777, allowlisted=False)
        job_id = "f" * 32
        action = print_flow.PrintAction(
            user=owner,
            action="fill",
            job_id=job_id,
        )
        ui = FakeUI()
        self.send_approval.return_value = (
            print_flow.admin_notifications.AdminBroadcastDelivery(
                delivered_targets=(ReplyTarget("telegram", 1),),
                failed_targets=(ReplyTarget("vk", 2),),
            )
        )
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ):
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {"provider": "vk", "sender_id": 777, "event_folder": "Кафе"},
            )
            self.assertTrue(await print_flow.handle_choice(action, ui))
            _payload, metadata = print_jobs.load_pending(job_id)

        self.send_approval.assert_awaited_once()
        self.assertEqual(self.send_approval.await_args.kwargs["job_id"], job_id)
        self.assertIn(
            "увеличить под размер",
            self.send_approval.await_args.kwargs["caption"],
        )
        self.assertIn(
            "<b>Новая печать в «Кафе»</b>",
            self.send_approval.await_args.kwargs["telegram_caption"],
        )
        self.assertIn(
            "VK ID: <code>777</code>",
            self.send_approval.await_args.kwargs["telegram_caption"],
        )
        self.assertNotIn(
            "Файл:",
            self.send_approval.await_args.kwargs["telegram_caption"],
        )
        self.assertEqual(metadata["pending_status"], "awaiting_authorization")
        ui.update_choice.assert_awaited_once_with(
            action,
            "✅ Выбрано: увеличить под размер, края обрежутся.",
        )
        ui.send_text.assert_awaited_once()
        self.assertIn("Оплатите", ui.send_text.await_args.args[1])
        self.fail_job.assert_not_awaited()

    async def test_cafe_job_fails_when_no_admin_received_the_request(self):
        print_flow.event_access.current_event.return_value = (
            "Кафе",
            None,
            True,
        )
        self.claim_choice.return_value = {
            "outcome": "awaiting_authorization",
            "status": "awaiting_authorization",
        }
        self.send_approval.return_value = (
            print_flow.admin_notifications.AdminBroadcastDelivery(
                delivered_targets=(),
                failed_targets=(
                    ReplyTarget("telegram", 1),
                    ReplyTarget("vk", 2),
                ),
            )
        )
        owner = user("telegram", 778, allowlisted=False)
        job_id = "0" * 32
        action = print_flow.PrintAction(owner, "fit", job_id)
        ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ):
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {
                    "provider": "telegram",
                    "sender_id": 778,
                    "event_folder": "Кафе",
                },
            )
            self.assertTrue(await print_flow.handle_choice(action, ui))
            self.assertFalse((Path(tmpdir) / job_id).exists())

        self.fail_job.assert_awaited_once()
        self.assertIn("администраторам", ui.send_text.await_args.args[1])

    async def test_post_dispatch_status_failure_does_not_reject_job(self):
        owner = user("vk", 333)
        incoming, _download = upload(owner, (461, 310))
        ui = FakeUI()
        ui.send_text.side_effect = (True, RuntimeError("VK unavailable"))
        with patch.object(print_flow.uuid, "uuid4") as uuid4, patch.object(
            print_flow,
            "submit_print_job",
            AsyncMock(return_value="a" * 32),
        ) as submit, patch.object(print_flow.log, "exception"):
            uuid4.return_value.hex = "b" * 32
            self.assertTrue(await print_flow.handle_upload(incoming, ui))

        submit.assert_awaited_once()
        self.fail_job.assert_not_awaited()
        self.assertNotIn(
            "Фото не принято",
            " ".join(str(item.args[1]) for item in ui.send_text.await_args_list),
        )

    async def test_exact_cafe_status_failure_does_not_block_admin_request(self):
        print_flow.event_access.current_event.return_value = (
            "Кафе",
            None,
            True,
        )
        self.claim_choice.return_value = {
            "outcome": "awaiting_authorization",
            "status": "awaiting_authorization",
        }
        owner = user("telegram", 334, allowlisted=False)
        incoming, _download = upload(owner, (461, 310))
        ui = FakeUI()
        ui.send_text.side_effect = (True, RuntimeError("TG unavailable"))
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(print_flow.uuid, "uuid4") as uuid4, patch.object(
            print_flow.log,
            "exception",
        ):
            uuid4.return_value.hex = "c" * 32
            self.assertTrue(await print_flow.handle_upload(incoming, ui))

        self.send_approval.assert_awaited_once()
        self.fail_job.assert_not_awaited()

    async def test_cancel_deletes_pending_without_dispatch(self):
        owner = user("telegram", 888)
        job_id = "1" * 32
        action = print_flow.PrintAction(
            user=owner,
            action="cancel",
            job_id=job_id,
        )
        ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(
            print_flow,
            "submit_print_job",
            new_callable=AsyncMock,
        ) as submit:
            print_jobs.save_pending(job_id, ".jpg", b"image", {"sender_id": 888})
            self.assertTrue(await print_flow.handle_choice(action, ui))
            self.assertFalse((Path(tmpdir) / job_id).exists())

        submit.assert_not_awaited()
        self.assertEqual(ui.acknowledge.await_args.args[1], "Печать отменена")

    async def test_simultaneous_actions_are_serialized_per_job(self):
        job_id = "2" * 32
        started = asyncio.Event()
        release = asyncio.Event()

        async def cancel(**_kwargs):
            started.set()
            await release.wait()
            return {"outcome": "cancelled"}

        self.cancel_job.side_effect = cancel
        owner = user("vk", 222)
        cancel_action = print_flow.PrintAction(owner, "cancel", job_id)
        fill_action = print_flow.PrintAction(owner, "fill", job_id)
        first_ui = FakeUI()
        second_ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ):
            print_jobs.save_pending(job_id, ".jpg", b"image", {"sender_id": 222})
            first = asyncio.create_task(
                print_flow.handle_choice(cancel_action, first_ui)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            try:
                self.assertTrue(
                    await print_flow.handle_choice(fill_action, second_ui)
                )
            finally:
                release.set()
            await asyncio.wait_for(first, timeout=1)

        self.assertIn(
            "уже обрабатывается",
            second_ui.acknowledge.await_args.args[1],
        )
        self.assertNotIn(job_id, print_flow._actions_in_progress)

    async def test_admin_rejection_notifies_original_vk_user(self):
        print_flow.event_access.current_event.return_value = ("Кафе", None, True)
        job_id = "3" * 32
        result = {
            "outcome": "cancelled",
            "job_id": job_id,
            "provider": "vk",
            "conversation_id": "321",
        }
        reject = AsyncMock(return_value=result)
        started = patch.object(
            print_flow.database,
            "reject_print_job_by_admin",
            reject,
        )
        started.start()
        self.addCleanup(started.stop)
        action = print_flow.PrintAction(
            user=user("vk", 556972284, is_admin=True),
            action="reject",
            job_id=job_id,
        )
        ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(
            print_flow.messenger_delivery,
            "send_text",
            AsyncMock(return_value=True),
        ) as notify, patch.object(
            print_flow,
            "submit_print_job",
            new_callable=AsyncMock,
        ) as submit:
            print_jobs.save_pending(
                job_id,
                ".jpg",
                b"image",
                {
                    "provider": "vk",
                    "sender_id": 321,
                    "sender_name": "Алёна Ёлкина",
                    "username": "foto_guest",
                    "event_folder": "Кафе",
                },
            )
            self.assertTrue(await print_flow.handle_admin_action(action, ui))
            self.assertFalse((Path(tmpdir) / job_id).exists())

        submit.assert_not_awaited()
        notify.assert_awaited_once_with(
            ReplyTarget("vk", 321),
            "❌ Печать фотографии отклонена администратором.",
        )
        ui.update_admin.assert_awaited_once_with(
            action,
            "🚫 Решение администратора: печать отклонена.",
        )
        self.send_admin_status.assert_awaited_once()
        final_status = self.send_admin_status.await_args.args[0]
        self.assertIn("Мероприятие: «Кафе»", final_status)
        self.assertIn(f"Job: {job_id}", final_status)
        self.assertIn("Пользователь: Алёна Ёлкина (@foto_guest)", final_status)
        self.assertIn("VK ID: 321", final_status)
        self.assertNotIn("Файл:", final_status)

    async def test_admin_approval_dispatches_in_background_to_original_provider(self):
        print_flow.event_access.current_event.return_value = ("Кафе", None, True)
        job_id = "4" * 32
        database_job_id = str(uuid.UUID(hex=job_id))
        result = {
            "outcome": "authorized",
            "job_id": database_job_id,
            "provider": "vk",
            "conversation_id": "4321",
            "provider_user_id": "4321",
            "print_mode": "fit",
            "event_name": "Кафе",
            "username": "vk_guest",
            "first_name": "Иван",
            "last_name": "Иванов",
        }
        authorize = AsyncMock(return_value=result)
        started_patch = patch.object(
            print_flow.database,
            "authorize_print_job_by_admin",
            authorize,
        )
        started_patch.start()
        self.addCleanup(started_patch.stop)
        submit_started = asyncio.Event()
        release_submit = asyncio.Event()

        async def submit(**_kwargs):
            submit_started.set()
            await release_submit.wait()
            return "5" * 32

        action = print_flow.PrintAction(
            user=user("telegram", 999, is_admin=True),
            action="approve",
            job_id=job_id,
        )
        ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(
            print_flow.messenger_delivery,
            "send_text",
            AsyncMock(return_value=True),
        ) as notify, patch.object(
            print_flow,
            "submit_print_job",
            side_effect=submit,
        ) as dispatch:
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((461, 310)),
                {
                    "sender_id": 4321,
                    "event_folder": "Кафе",
                    "print_mode": "fit",
                },
            )
            self.assertTrue(await print_flow.handle_admin_action(action, ui))
            await asyncio.wait_for(submit_started.wait(), timeout=1)
            ui.update_admin.assert_awaited_once_with(
                action,
                "✅ Решение администратора: печать разрешена.",
            )
            notify.assert_not_awaited()
            self.send_admin_status.assert_not_awaited()
            tasks = tuple(print_flow._background_tasks)
            self.assertEqual(len(tasks), 1)
            release_submit.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
            self.assertFalse((Path(tmpdir) / job_id).exists())

        self.assertEqual(
            dispatch.await_args.kwargs["reply_target"],
            ReplyTarget("vk", 4321),
        )
        self.assertEqual(dispatch.await_args.kwargs["job_id"], job_id)
        notify.assert_awaited_once_with(
            ReplyTarget("vk", 4321),
            "✅ Оплата подтверждена. Ваше фото добавлено в очередь "
            "и скоро будет распечатано.",
        )
        self.send_admin_status.assert_awaited_once()
        final_status = self.send_admin_status.await_args.args[0]
        self.assertIn("✅ Фото отправлено на печать.", final_status)
        self.assertIn("Мероприятие: «Кафе»", final_status)
        self.assertIn(f"Job: {job_id}", final_status)
        self.assertIn("Пользователь: Иван Иванов (@vk_guest)", final_status)
        self.assertIn("VK ID: 4321", final_status)
        self.assertNotIn("Файл:", final_status)

    async def test_admin_dispatch_error_sends_one_final_status_to_both(self):
        print_flow.event_access.current_event.return_value = ("Кафе", None, True)
        job_id = "5" * 32
        database_job_id = str(uuid.UUID(hex=job_id))
        result = {
            "outcome": "authorized",
            "job_id": database_job_id,
            "provider": "telegram",
            "conversation_id": "123",
            "provider_user_id": "123",
            "print_mode": "fill",
            "event_name": "Кафе",
            "first_name": "Анна",
            "last_name": "Петрова",
        }
        action = print_flow.PrintAction(
            user=user("vk", 556972284, is_admin=True),
            action="approve",
            job_id=job_id,
        )
        ui = FakeUI()
        with TemporaryDirectory() as tmpdir, patch.object(
            print_jobs,
            "PENDING_ROOT",
            Path(tmpdir),
        ), patch.object(
            print_flow.database,
            "authorize_print_job_by_admin",
            AsyncMock(return_value=result),
        ), patch.object(
            print_flow,
            "submit_print_job",
            AsyncMock(side_effect=RuntimeError("Диск временно недоступен")),
        ), patch.object(
            print_flow.messenger_delivery,
            "send_text",
            AsyncMock(return_value=True),
        ) as notify, patch.object(print_flow.log, "exception"):
            print_jobs.save_pending(
                job_id,
                ".jpg",
                image_payload((1000, 1000)),
                {
                    "provider": "telegram",
                    "sender_id": 123,
                    "event_folder": "Кафе",
                    "print_mode": "fill",
                },
            )
            self.assertTrue(await print_flow.handle_admin_action(action, ui))
            tasks = tuple(print_flow._background_tasks)
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

        ui.update_admin.assert_awaited_once_with(
            action,
            "✅ Решение администратора: печать разрешена.",
        )
        notify.assert_awaited_once_with(
            ReplyTarget("telegram", 123),
            "❌ Не удалось передать фото на печать. "
            "Обратитесь к администратору.",
        )
        self.send_admin_status.assert_awaited_once()
        final_status = self.send_admin_status.await_args.args[0]
        self.assertIn("❌ Ошибка передачи на печать", final_status)
        self.assertIn("Диск временно недоступен", final_status)
        self.assertIn(f"Job: {job_id}", final_status)
        self.assertIn("Пользователь: Анна Петрова", final_status)


class PrintSubmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        print_flow._background_tasks.clear()

    async def test_publishes_then_archives_without_blocking_statuses(self):
        command_id = "6" * 32
        target = ReplyTarget("vk", 123)
        metadata = {"event_folder": "event", "print_mode": "fit"}
        archive_started = asyncio.Event()
        release_archive = asyncio.Event()

        async def archive_copy(**_kwargs):
            archive_started.set()
            await release_archive.wait()

        with patch.object(
            print_flow.yadisk_poll,
            "store_print_job",
            AsyncMock(return_value={
                "artifact_path": "/event/photo.jpg",
                "info_path": "/event/photo.txt",
            }),
        ) as store, patch.object(
            print_flow.database,
            "mark_print_job_dispatching",
            AsyncMock(return_value={"outcome": "dispatching"}),
        ), patch.object(
            print_flow.yadisk_control,
            "send_command",
            AsyncMock(return_value={"command_id": command_id}),
        ) as send, patch.object(
            print_flow.print_archive,
            "send",
            AsyncMock(side_effect=archive_copy),
        ) as archive, patch.object(print_flow.uuid, "uuid4") as uuid4:
            uuid4.return_value.hex = command_id
            result = await print_flow.submit_print_job(
                job_id="7" * 32,
                external_user_id=123,
                suffix=".jpg",
                payload=b"image",
                metadata=metadata,
                reply_target=target,
            )
            await asyncio.wait_for(archive_started.wait(), timeout=1)
            tasks = tuple(print_flow._background_tasks)
            self.assertEqual(len(tasks), 1)
            self.assertFalse(tasks[0].done())
            release_archive.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

        self.assertEqual(result, command_id)
        store.assert_awaited_once()
        send.assert_awaited_once_with(
            "print_image",
            target,
            metadata,
            command_id=command_id,
        )
        archive.assert_awaited_once()

    async def test_archive_metadata_cannot_fail_an_already_sent_command(self):
        command_id = "8" * 32
        metadata = {"event_folder": "event", "print_mode": "legacy-value"}
        with patch.object(
            print_flow.yadisk_poll,
            "store_print_job",
            AsyncMock(return_value={}),
        ), patch.object(
            print_flow.database,
            "mark_print_job_dispatching",
            AsyncMock(return_value={"outcome": "dispatching"}),
        ), patch.object(
            print_flow.yadisk_control,
            "send_command",
            AsyncMock(return_value={"command_id": command_id}),
        ), patch.object(
            print_flow.print_archive,
            "send",
            new_callable=AsyncMock,
        ) as archive, patch.object(
            print_flow.log,
            "exception",
        ) as logged, patch.object(print_flow.uuid, "uuid4") as uuid4:
            uuid4.return_value.hex = command_id
            result = await print_flow.submit_print_job(
                job_id="9" * 32,
                external_user_id=123,
                suffix=".jpg",
                payload=b"image",
                metadata=metadata,
                reply_target=ReplyTarget("vk", 123),
            )

        self.assertEqual(result, command_id)
        archive.assert_not_awaited()
        logged.assert_called_once()


class PrintArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_archive_does_not_prepare_or_send_preview(self):
        with patch.object(
            print_flow.print_archive.runtime_config,
            "archive_delivery_providers",
            return_value=(),
        ), patch.object(
            print_flow.print_archive.print_media,
            "jpeg_preview",
        ) as preview, patch.object(
            print_flow.print_archive.messenger_delivery,
            "send_photo",
            new_callable=AsyncMock,
        ) as send:
            await print_flow.print_archive.send(
                job_id="0" * 32,
                payload=b"image",
                metadata={},
                source_target=ReplyTarget("telegram", 1),
                mode_label="как есть",
            )

        preview.assert_not_called()
        send.assert_not_awaited()

    async def test_archive_sends_telegram_html_and_vk_plain_text(self):
        target = ReplyTarget("vk", 123)
        metadata = {
            "event_folder": "Кафе & тест",
            "provider": "vk",
            "sender_id": 123,
            "sender_name": "Иван <Иванов>",
            "source_filename": "photo & copy.jpg",
        }
        with patch.object(
            print_flow.print_archive.runtime_config,
            "archive_delivery_providers",
            return_value=("telegram", "vk"),
        ), patch.object(
            print_flow.print_archive.telegram_api,
            "ARCHIVE_CHAT_ID",
            "999",
        ), patch.object(
            print_flow.print_archive.vk_api,
            "ARCHIVE_CHAT_ID",
            "888",
        ), patch.object(
            print_flow.print_archive.print_media,
            "jpeg_preview",
            return_value=b"preview",
        ), patch.object(
            print_flow.print_archive.messenger_delivery,
            "send_photo",
            AsyncMock(return_value=True),
        ) as send:
            await print_flow.print_archive.send(
                job_id="a" * 32,
                payload=b"image",
                metadata=metadata,
                source_target=target,
                mode_label="как есть & без обрезки",
            )

        self.assertEqual(send.await_count, 2)
        telegram_call, vk_call = send.await_args_list
        self.assertEqual(
            telegram_call.args[0],
            ReplyTarget("telegram", 999),
        )
        telegram_caption = telegram_call.args[2]
        self.assertIn("<b>Фото отправлено на печать</b>", telegram_caption)
        self.assertIn("Кафе &amp; тест", telegram_caption)
        self.assertIn("Иван &lt;Иванов&gt;", telegram_caption)
        self.assertNotIn("Файл:", telegram_caption)
        self.assertNotIn("photo &amp; copy.jpg", telegram_caption)
        self.assertEqual(telegram_call.kwargs["parse_mode"], "HTML")

        self.assertEqual(vk_call.args[0], ReplyTarget("vk", 888))
        vk_caption = vk_call.args[2]
        self.assertIn("Фото отправлено на печать", vk_caption)
        self.assertIn("Кафе & тест", vk_caption)
        self.assertIn("Иван <Иванов>", vk_caption)
        self.assertNotIn("<b>", vk_caption)
        self.assertIsNone(vk_call.kwargs["parse_mode"])

    async def test_one_archive_failure_does_not_suppress_the_other(self):
        with patch.object(
            print_flow.print_archive.runtime_config,
            "archive_delivery_providers",
            return_value=("telegram", "vk"),
        ), patch.object(
            print_flow.print_archive.telegram_api,
            "ARCHIVE_CHAT_ID",
            "999",
        ), patch.object(
            print_flow.print_archive.vk_api,
            "ARCHIVE_CHAT_ID",
            "888",
        ), patch.object(
            print_flow.print_archive.print_media,
            "jpeg_preview",
            return_value=b"preview",
        ), patch.object(
            print_flow.print_archive.messenger_delivery,
            "send_photo",
            AsyncMock(side_effect=(RuntimeError("telegram down"), True)),
        ) as send, patch.object(
            print_flow.print_archive.log,
            "exception",
        ) as logged:
            await print_flow.print_archive.send(
                job_id="1" * 32,
                payload=b"image",
                metadata={},
                source_target=ReplyTarget("vk", 123),
                mode_label="как есть",
            )

        self.assertEqual(send.await_count, 2)
        self.assertEqual(
            [call.args[0].provider for call in send.await_args_list],
            ["telegram", "vk"],
        )
        logged.assert_called_once()

    async def test_archive_skips_matching_source_but_sends_other_provider(self):
        archive_target = ReplyTarget("telegram", 999)
        with patch.object(
            print_flow.print_archive.runtime_config,
            "archive_delivery_providers",
            return_value=("telegram", "vk"),
        ), patch.object(
            print_flow.print_archive.telegram_api,
            "ARCHIVE_CHAT_ID",
            "999",
        ), patch.object(
            print_flow.print_archive.vk_api,
            "ARCHIVE_CHAT_ID",
            "888",
        ), patch.object(
            print_flow.print_archive.print_media,
            "jpeg_preview",
            return_value=b"preview",
        ), patch.object(
            print_flow.print_archive.messenger_delivery,
            "send_photo",
            AsyncMock(return_value=True),
        ) as send:
            await print_flow.print_archive.send(
                job_id="b" * 32,
                payload=b"image",
                metadata={},
                source_target=archive_target,
                mode_label="как есть",
            )

        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], ReplyTarget("vk", 888))


if __name__ == "__main__":
    unittest.main()
