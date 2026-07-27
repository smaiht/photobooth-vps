import json
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import delivery_retry
import telegram_api
import telegram_session_delivery
import vk_api


class FakeResponse:
    def __init__(self, status: int, body: dict | list | bytes, headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self) -> bytes:
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class RetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_recognizes_transient_http_and_telegram_retry_after(self):
        self.assertTrue(delivery_retry.retryable_http_status(429))
        self.assertTrue(delivery_retry.retryable_http_status(503))
        self.assertFalse(delivery_retry.retryable_http_status(400))
        self.assertEqual(
            delivery_retry.retry_after_seconds(
                {},
                b'{"parameters":{"retry_after":7}}',
            ),
            7,
        )

    async def test_telegram_text_retries_500_then_succeeds(self):
        session = FakeSession([
            FakeResponse(500, {"ok": False}),
            FakeResponse(200, {"ok": True, "result": {"message_id": 1}}),
        ])
        with patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            delivered = await telegram_api.send_text(
                session,
                "https://telegram.test",
                123,
                "hello",
            )

        self.assertTrue(delivered)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(
            call[1]["timeout"] is telegram_api.DEFAULT_SEND_TIMEOUT
            for call in session.calls
        ))
        wait.assert_awaited_once_with(1, retry_after=None)

    async def test_telegram_permanent_400_is_not_retried(self):
        session = FakeSession([
            FakeResponse(400, {"ok": False, "description": "bad request"}),
        ])
        with patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            delivered = await telegram_api.send_text(
                session,
                "https://telegram.test",
                123,
                "hello",
            )

        self.assertFalse(delivered)
        self.assertEqual(len(session.calls), 1)
        wait.assert_not_awaited()

    async def test_telegram_edit_not_modified_counts_as_success(self):
        session = FakeSession([
            FakeResponse(400, {
                "ok": False,
                "description": "Bad Request: message is not modified",
            }),
        ])
        with patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            delivered = await telegram_api.edit_print_caption(
                session,
                "https://telegram.test",
                123,
                9,
                "caption",
            )

        self.assertTrue(delivered)
        self.assertEqual(len(session.calls), 1)
        wait.assert_not_awaited()

    async def test_telegram_stops_after_three_transient_failures(self):
        session = FakeSession([
            FakeResponse(503, {"ok": False}),
            FakeResponse(503, {"ok": False}),
            FakeResponse(503, {"ok": False}),
        ])
        with patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            delivered = await telegram_api.send_text(
                session,
                "https://telegram.test",
                123,
                "hello",
            )

        self.assertFalse(delivered)
        self.assertEqual(len(session.calls), delivery_retry.MAX_ATTEMPTS)
        self.assertEqual(wait.await_count, delivery_retry.MAX_ATTEMPTS - 1)

    async def test_telegram_connection_error_is_retried(self):
        session = FakeSession([
            aiohttp.ClientConnectionError("offline"),
            FakeResponse(200, {"ok": True, "result": {"message_id": 1}}),
        ])
        with patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            delivered = await telegram_api.send_text(
                session,
                "https://telegram.test",
                123,
                "hello",
            )

        self.assertTrue(delivered)
        self.assertEqual(len(session.calls), 2)
        wait.assert_awaited_once()

    async def test_telegram_poll_error_does_not_expose_token_url(self):
        session = MagicMock()
        session.get.side_effect = aiohttp.InvalidURL(
            "https://api.telegram.org/botTEST_SECRET/getUpdates"
        )

        try:
            await telegram_api.get_updates(
                session,
                "https://api.telegram.org/botTEST_SECRET",
                offset=0,
                allowed_updates=("message",),
            )
        except RuntimeError as exc:
            rendered = "".join(traceback.format_exception(exc))
        else:
            self.fail("get_updates should fail")

        self.assertNotIn("TEST_SECRET", rendered)

    async def test_telegram_get_file_error_does_not_expose_token_url(self):
        session = MagicMock()
        session.post.side_effect = aiohttp.InvalidURL(
            "https://api.telegram.org/botTEST_SECRET/getFile"
        )

        try:
            await telegram_api.download_file(
                session,
                "https://api.telegram.org/botTEST_SECRET",
                "file-id",
                max_size=1024,
            )
        except RuntimeError as exc:
            rendered = "".join(traceback.format_exception(exc))
        else:
            self.fail("download_file should fail")

        self.assertNotIn("TEST_SECRET", rendered)

    async def test_telegram_photo_rebuilds_multipart_for_retry(self):
        session = FakeSession([
            FakeResponse(503, {"ok": False}),
            FakeResponse(200, {"ok": True, "result": {"message_id": 9}}),
        ])
        first_form = MagicMock()
        second_form = MagicMock()
        with patch(
            "telegram_api.aiohttp.FormData",
            side_effect=(first_form, second_form),
        ) as form_factory, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ):
            message_id = await telegram_api.send_photo(
                session,
                "https://telegram.test",
                123,
                b"photo",
                "caption",
                None,
                None,
            )

        self.assertEqual(message_id, 9)
        self.assertEqual(form_factory.call_count, 2)
        self.assertIs(session.calls[0][1]["data"], first_form)
        self.assertIs(session.calls[1][1]["data"], second_form)

    async def test_telegram_archive_rebuilds_form_for_retry(self):
        session = FakeSession([
            FakeResponse(502, {"ok": False}),
            FakeResponse(200, {"ok": True, "result": []}),
        ])
        first_form = MagicMock()
        second_form = MagicMock()
        with patch(
            "telegram_session_delivery.aiohttp.ClientSession",
            return_value=FakeSessionContext(session),
        ), patch(
            "telegram_session_delivery._form",
            side_effect=(first_form, second_form),
        ) as form_factory, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ):
            delivered = await telegram_session_delivery._post(
                "sendMediaGroup",
                [(MagicMock(), "photo")],
            )

        self.assertTrue(delivered)
        self.assertEqual(form_factory.call_count, 2)
        self.assertIs(session.calls[0][1]["data"], first_form)
        self.assertIs(session.calls[1][1]["data"], second_form)

    async def test_telegram_archive_stops_after_three_failures(self):
        session = FakeSession([
            FakeResponse(503, {"ok": False}),
            FakeResponse(503, {"ok": False}),
            FakeResponse(503, {"ok": False}),
        ])
        forms = [MagicMock() for _ in range(delivery_retry.MAX_ATTEMPTS)]
        with patch(
            "telegram_session_delivery.aiohttp.ClientSession",
            return_value=FakeSessionContext(session),
        ), patch(
            "telegram_session_delivery._form",
            side_effect=forms,
        ) as form_factory, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            delivered = await telegram_session_delivery._post(
                "sendPhoto",
                [(MagicMock(), "photo")],
            )

        self.assertFalse(delivered)
        self.assertEqual(len(session.calls), delivery_retry.MAX_ATTEMPTS)
        self.assertEqual(form_factory.call_count, delivery_retry.MAX_ATTEMPTS)
        self.assertEqual(wait.await_count, delivery_retry.MAX_ATTEMPTS - 1)

    async def test_vk_message_retry_reuses_random_id(self):
        temporary = vk_api.VkApiError("temporary", retryable=True)
        with patch(
            "vk_api.secrets.randbelow",
            return_value=100,
        ), patch(
            "vk_api.api_call",
            AsyncMock(side_effect=(temporary, {"conversation_message_id": 8})),
        ) as api, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            message_id = await vk_api.send_text(object(), 123, "hello")

        self.assertEqual(message_id, 8)
        self.assertEqual(api.await_count, 2)
        self.assertEqual(
            [item.kwargs["random_id"] for item in api.await_args_list],
            [101, 101],
        )
        wait.assert_awaited_once_with(1, retry_after=None)

    async def test_vk_real_api_response_503_is_retried_safely(self):
        session = FakeSession([
            FakeResponse(
                503,
                b"temporary body containing token-that-must-not-be-logged",
                {"Content-Type": "text/plain"},
            ),
            FakeResponse(200, {"response": 12}),
        ])
        with patch.object(
            vk_api,
            "BOT_TOKEN",
            "secret-token",
        ), patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait, self.assertLogs(vk_api.log, level="WARNING") as logs:
            message_id = await vk_api.send_text(session, 123, "hello")

        self.assertEqual(message_id, 12)
        self.assertEqual(len(session.calls), 2)
        random_ids = [item[1]["data"]["random_id"] for item in session.calls]
        self.assertEqual(random_ids[0], random_ids[1])
        wait.assert_awaited_once()
        self.assertNotIn("secret-token", " ".join(logs.output))
        self.assertNotIn("token-that-must-not-be-logged", " ".join(logs.output))

    async def test_vk_permanent_error_is_not_retried(self):
        permanent = vk_api.VkApiError("permission denied")
        with patch(
            "vk_api.api_call",
            AsyncMock(side_effect=permanent),
        ) as api, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            with self.assertRaisesRegex(vk_api.VkApiError, "permission"):
                await vk_api.send_text(object(), 123, "hello")

        api.assert_awaited_once()
        wait.assert_not_awaited()

    async def test_vk_stops_after_three_transient_failures(self):
        failures = [
            vk_api.VkApiError("temporary", retryable=True)
            for _ in range(delivery_retry.MAX_ATTEMPTS)
        ]
        with patch(
            "vk_api.secrets.randbelow",
            return_value=100,
        ), patch(
            "vk_api.api_call",
            AsyncMock(side_effect=failures),
        ) as api, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait:
            with self.assertRaisesRegex(vk_api.VkApiError, "temporary"):
                await vk_api.send_text(object(), 123, "hello")

        self.assertEqual(api.await_count, delivery_retry.MAX_ATTEMPTS)
        self.assertEqual(wait.await_count, delivery_retry.MAX_ATTEMPTS - 1)
        self.assertEqual(
            [item.kwargs["random_id"] for item in api.await_args_list],
            [101] * delivery_retry.MAX_ATTEMPTS,
        )

    async def test_vk_photo_upload_gets_fresh_server_and_form(self):
        session = FakeSession([
            FakeResponse(200, {"error": "temporary"}),
            FakeResponse(200, {
                "server": 10,
                "photo": [{"sizes": []}],
                "hash": "saved-hash",
            }),
        ])
        first_form = MagicMock()
        second_form = MagicMock()
        with patch(
            "vk_api.api_call",
            AsyncMock(side_effect=(
                {"upload_url": "https://upload.vk.test/one"},
                {"upload_url": "https://upload.vk.test/two"},
                [{"owner_id": -1, "id": 2}],
            )),
        ) as api, patch(
            "vk_api.aiohttp.FormData",
            side_effect=(first_form, second_form),
        ) as form_factory, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ):
            attachment = await vk_api.upload_message_photo(
                session,
                123,
                b"photo",
            )

        self.assertEqual(attachment, "photo-1_2")
        self.assertEqual(form_factory.call_count, 2)
        self.assertEqual(
            [item[0] for item in session.calls],
            ["https://upload.vk.test/one", "https://upload.vk.test/two"],
        )
        self.assertEqual(api.await_count, 3)
        saved_photo = api.await_args_list[2].kwargs["photo"]
        self.assertIsInstance(saved_photo, str)
        self.assertEqual(json.loads(saved_photo), [{"sizes": []}])

    async def test_vk_photo_upload_recovers_from_logged_response_shapes(self):
        session = FakeSession([
            FakeResponse(
                200,
                b"invalid-json-containing-secret-upload-response",
                {"Content-Type": "text/html"},
            ),
            FakeResponse(200, {
                "server": None,
                "photo": "[]",
                "hash": "",
            }),
            FakeResponse(200, {
                "server": 10,
                "photo": "saved-photo-payload",
                "hash": "saved-hash",
            }),
        ])
        forms = [MagicMock() for _ in range(delivery_retry.MAX_ATTEMPTS)]
        with patch(
            "vk_api.api_call",
            AsyncMock(side_effect=(
                {"upload_url": "https://upload.vk.test/one"},
                {"upload_url": "https://upload.vk.test/two"},
                {"upload_url": "https://upload.vk.test/three"},
                [{"owner_id": -1, "id": 2}],
            )),
        ) as api, patch(
            "vk_api.aiohttp.FormData",
            side_effect=forms,
        ) as form_factory, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ) as wait, self.assertLogs(vk_api.log, level="WARNING") as logs:
            attachment = await vk_api.upload_message_photo(
                session,
                123,
                b"photo",
            )

        self.assertEqual(attachment, "photo-1_2")
        self.assertEqual(api.await_count, 4)
        self.assertEqual(form_factory.call_count, delivery_retry.MAX_ATTEMPTS)
        self.assertEqual(wait.await_count, delivery_retry.MAX_ATTEMPTS - 1)
        self.assertEqual(
            [item[0] for item in session.calls],
            [
                "https://upload.vk.test/one",
                "https://upload.vk.test/two",
                "https://upload.vk.test/three",
            ],
        )
        self.assertNotIn(
            "secret-upload-response",
            " ".join(logs.output),
        )

    async def test_vk_upload_error_does_not_expose_signed_url(self):
        signed_url = "https://upload.vk.test/photo?hash=TEST_SECRET"
        session = FakeSession([
            aiohttp.InvalidURL(signed_url)
            for _ in range(delivery_retry.MAX_ATTEMPTS)
        ])
        with patch(
            "vk_api.api_call",
            AsyncMock(return_value={"upload_url": signed_url}),
        ), patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ):
            try:
                await vk_api.upload_message_photo(session, 123, b"photo")
            except vk_api.VkApiError as exc:
                rendered = "".join(traceback.format_exception(exc))
            else:
                self.fail("upload_message_photo should fail")

        self.assertNotIn("TEST_SECRET", rendered)

    async def test_vk_long_poll_error_does_not_expose_key(self):
        session = MagicMock()
        server = "https://lp.vk.test/check?key=TEST_SECRET"
        session.get.side_effect = aiohttp.InvalidURL(server)

        try:
            await vk_api.poll_long_poll(
                session,
                server,
                "TEST_SECRET",
                "1",
            )
        except vk_api.VkApiError as exc:
            rendered = "".join(traceback.format_exception(exc))
        else:
            self.fail("poll_long_poll should fail")

        self.assertNotIn("TEST_SECRET", rendered)

    async def test_vk_document_upload_gets_fresh_server_after_503(self):
        session = FakeSession([
            FakeResponse(503, b"temporary", {"Content-Type": "text/plain"}),
            FakeResponse(200, {"file": "uploaded-file"}),
        ])
        with patch(
            "vk_api.api_call",
            AsyncMock(side_effect=(
                {"upload_url": "https://upload.vk.test/one"},
                {"upload_url": "https://upload.vk.test/two"},
                {"doc": {"owner_id": -1, "id": 3}},
            )),
        ), patch(
            "vk_api.aiohttp.FormData",
            side_effect=(MagicMock(), MagicMock()),
        ) as form_factory, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ):
            attachment = await vk_api.upload_message_document(
                session,
                123,
                b"document",
                filename="log.txt",
            )

        self.assertEqual(attachment, "doc-1_3")
        self.assertEqual(form_factory.call_count, 2)
        self.assertEqual(
            [item[0] for item in session.calls],
            ["https://upload.vk.test/one", "https://upload.vk.test/two"],
        )

    async def test_vk_message_retry_does_not_repeat_successful_photo_upload(self):
        temporary = vk_api.VkApiError("temporary", retryable=True)
        with patch(
            "vk_api.upload_message_photo",
            AsyncMock(return_value="photo-1_2"),
        ) as upload, patch(
            "vk_api.api_call",
            AsyncMock(side_effect=(temporary, 77)),
        ) as api, patch(
            "delivery_retry.wait_before_retry",
            new_callable=AsyncMock,
        ):
            message_id = await vk_api.send_photo(
                object(),
                123,
                b"photo",
                "caption",
            )

        self.assertEqual(message_id, 77)
        upload.assert_awaited_once()
        self.assertEqual(api.await_count, 2)
        random_ids = [item.kwargs["random_id"] for item in api.await_args_list]
        self.assertEqual(random_ids[0], random_ids[1])


if __name__ == "__main__":
    unittest.main()
