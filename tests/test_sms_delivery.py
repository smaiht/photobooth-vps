import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import admin_notifications
import control_response_service
from messaging import ReplyTarget
import yadisk_control
import yadisk_poll


class SmsDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sms_uses_notice_policy_and_deduplicates_reupload_after_restart(self):
        notice_id = "a" * 32
        item = {"name": f"notice_{notice_id}.json"}
        notice = {
            "schema_version": yadisk_control.SCHEMA_VERSION,
            "message_type": "booth_notice", "kind": "sms_received",
            "notice_id": notice_id, "title": "СМС на модем фотобудки",
            "text": "От: Test sender\n\n<код> & кириллица 📩",
        }
        targets = (ReplyTarget("telegram", "11"), ReplyTarget("vk", "22"))
        successful = set()

        async def send(target, text):
            self.assertIn(notice["text"], text)
            return target.provider in successful

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(yadisk_poll, "STATE_FILE", Path(tmpdir) / "state.json"), \
             patch.object(yadisk_poll, "_state", {"handled_messages": [], "session_deliveries": {}}), \
             patch.object(admin_notifications, "configured_admin_targets", return_value=targets), \
             patch.object(admin_notifications.messenger_delivery, "send_text", AsyncMock(side_effect=send)) as deliver, \
             patch.object(yadisk_poll, "_delete_inbox_message", AsyncMock(return_value=True)) as delete:
            self.assertFalse(await yadisk_poll._process_notice(item, notice, control_response_service.handle_notice))
            delete.assert_not_awaited()
            self.assertEqual([call.args[0] for call in deliver.await_args_list], list(targets))
            deliver.reset_mock()
            successful.add("telegram")
            self.assertTrue(await yadisk_poll._process_notice(item, notice, control_response_service.handle_notice))
            # Like every notice, SMS attempts both admins but one success is enough.
            self.assertEqual([call.args[0] for call in deliver.await_args_list], list(targets))
            delete.assert_awaited_once()
            saved = json.loads(yadisk_poll.STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual(saved["handled_messages"], [item["name"]])
            self.assertEqual(saved["session_deliveries"], {})

            # A booth retry after an ambiguous upload/read-mark response must
            # not notify either provider again, even after a VPS restart.
            yadisk_poll._state_load()
            deliver.reset_mock()
            self.assertTrue(await yadisk_poll._process_notice(item, notice, control_response_service.handle_notice))
            deliver.assert_not_awaited()

    async def test_missing_administrators_keep_sms_on_disk(self):
        with patch.object(admin_notifications, "configured_admin_targets", return_value=()):
            self.assertFalse(await control_response_service.handle_notice({
                "kind": "sms_received", "notice_id": "b" * 32, "text": "test",
            }))
