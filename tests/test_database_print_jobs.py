import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database


JOB_ID = "11111111-1111-4111-8111-111111111111"
COMMAND_ID = "22222222-2222-4222-8222-222222222222"


class FakeCursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.row


class FakeConnection:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.executed = []

    async def execute(self, sql, parameters=()):
        self.executed.append((sql, parameters))
        if not self.rows:
            raise AssertionError(f"unexpected SQL: {sql}")
        return FakeCursor(self.rows.pop(0))

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class MigrationTests(unittest.TestCase):
    def test_print_jobs_schema_has_durable_states_and_open_job_guard(self):
        sql = (
            Path(__file__).parents[1] / "migrations" / "0002_print_jobs.sql"
        ).read_text()

        self.assertIn("user_id bigint NOT NULL REFERENCES bot_users(id)", sql)
        self.assertIn("event_name text NOT NULL", sql)
        self.assertIn("conversation_id text NOT NULL", sql)
        self.assertIn("source_message_id text", sql)
        self.assertIn("choice_message_id text", sql)
        self.assertIn("command_id uuid UNIQUE", sql)
        self.assertNotIn("dispatching_at", sql)
        for status in (
            "processing",
            "awaiting_choice",
            "awaiting_authorization",
            "authorized",
            "dispatching",
            "queued",
            "failed",
            "cancelled",
        ):
            self.assertIn(f"'{status}'", sql)
        self.assertIn("print_jobs_one_open_per_user_uidx", sql)
        self.assertNotIn("print_jobs_source_identity_uidx", sql)
        self.assertNotIn("UPDATE bot_users", sql)
        self.assertIn("WHERE status IN", sql)


class BotUserDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_start_does_not_replace_current_and_first_uses_nonempty(self):
        connection = FakeConnection((7,), None)
        with patch("database._connect", AsyncMock(return_value=connection)):
            user_id = await database.record_bot_start(
                provider="telegram",
                provider_user_id=123,
                start_parameter="   ",
                provider_update_id=456,
            )

        self.assertEqual(user_id, 7)
        upsert_sql, upsert_parameters = connection.executed[0]
        self.assertIn("first_start_parameter = COALESCE", upsert_sql)
        self.assertIn("current_start_parameter = COALESCE", upsert_sql)
        self.assertEqual(upsert_parameters[-2:], (None, None))
        self.assertEqual(connection.executed[1][1], (7, None, "456"))

    async def test_ensure_bot_user_does_not_write_start_event_or_parameters(self):
        connection = FakeConnection((8,))
        with patch("database._connect", AsyncMock(return_value=connection)):
            user_id = await database.ensure_bot_user(
                provider="max",
                provider_user_id="42",
                first_name="Гость",
            )

        self.assertEqual(user_id, 8)
        self.assertEqual(len(connection.executed), 1)
        sql = connection.executed[0][0]
        self.assertNotIn("bot_start_events", sql)
        self.assertNotIn("start_parameter", sql)

    async def test_current_start_parameter_check_uses_messenger_identity(self):
        connection = FakeConnection((True,))
        with patch("database._connect", AsyncMock(return_value=connection)):
            allowed = await database.user_has_current_start_parameter(
                provider="telegram",
                provider_user_id=123,
                start_parameter="ev_token",
            )

        self.assertTrue(allowed)
        sql, parameters = connection.executed[0]
        self.assertIn("current_start_parameter = %s", sql)
        self.assertEqual(parameters, ("telegram", "123", "ev_token"))


class PrintJobDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_job_starts_processing_with_text_message_ids(self):
        connection = FakeConnection(None, (JOB_ID, "processing"))
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.create_print_job(
                job_id=JOB_ID,
                user_id=9,
                event_name="2026-08-17 Свадьба Ивановых",
                conversation_id=-100123,
                source_message_id=77,
            )

        self.assertEqual(result["outcome"], "created")
        self.assertIn("event_name <> %s", connection.executed[0][0])
        parameters = connection.executed[1][1]
        self.assertEqual(parameters[3:], ("-100123", "77"))

    async def test_create_job_returns_existing_open_job_on_unique_conflict(self):
        connection = FakeConnection(None, None, (JOB_ID, "awaiting_choice"))
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.create_print_job(
                user_id=9,
                event_name="2026-08-17 Свадьба Ивановых",
                conversation_id="123",
            )

        self.assertEqual(result, {
            "outcome": "already_open",
            "job_id": JOB_ID,
            "status": "awaiting_choice",
        })
        self.assertIn("ON CONFLICT DO NOTHING", connection.executed[1][0])
        self.assertIn("status IN", connection.executed[2][0])
        self.assertEqual(connection.executed[2][1], (9,))

    async def test_event_choice_is_authorized_after_token_and_cooldown_checks(self):
        connection = FakeConnection(
            (9, "2026-08-17 Свадьба Ивановых", "awaiting_choice", None, None, "ev_token"),
            None,
            (JOB_ID,),
        )
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.claim_print_job_choice(
                job_id=JOB_ID,
                user_id=9,
                current_event_name="2026-08-17 Свадьба Ивановых",
                print_mode="fill",
                current_event_token="ev_token",
                cafe_mode=False,
            )

        self.assertEqual(result["outcome"], "authorized")
        self.assertEqual(result["authorization_kind"], "event")
        self.assertIn("status = 'queued'", connection.executed[1][0])
        self.assertIn("event_name = %s", connection.executed[1][0])
        self.assertEqual(connection.executed[2][1][0:3], (
            "authorized", "fill", "event"))

    async def test_event_cooldown_leaves_job_awaiting_choice(self):
        connection = FakeConnection(
            (9, "2026-08-17 Свадьба Ивановых", "awaiting_choice", None, None, "ev_token"),
            (123,),
        )
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.claim_print_job_choice(
                job_id=JOB_ID,
                user_id=9,
                current_event_name="2026-08-17 Свадьба Ивановых",
                print_mode="fit",
                current_event_token="ev_token",
                cafe_mode=False,
            )

        self.assertEqual(result, {
            "outcome": "cooldown",
            "job_id": JOB_ID,
            "retry_after_seconds": 123,
        })
        self.assertEqual(len(connection.executed), 2)
        self.assertIn(
            "authorization_kind = 'event'",
            connection.executed[1][0],
        )

    async def test_cafe_choice_waits_for_future_payment_or_cashier(self):
        connection = FakeConnection(
            (9, "Кафе", "awaiting_choice", None, None, "anything"),
            (JOB_ID,),
        )
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.claim_print_job_choice(
                job_id=JOB_ID,
                user_id=9,
                current_event_name="Кафе",
                print_mode="fit",
                current_event_token=None,
                cafe_mode=True,
            )

        self.assertEqual(result["outcome"], "awaiting_authorization")
        self.assertIsNone(result["authorization_kind"])
        self.assertEqual(connection.executed[1][1][0], "awaiting_authorization")

    async def test_allowlist_authorizes_without_guest_cooldown_query(self):
        connection = FakeConnection(
            (9, "Кафе", "awaiting_choice", None, None, None),
            (JOB_ID,),
        )
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.claim_print_job_choice(
                job_id=JOB_ID,
                user_id=9,
                current_event_name="Кафе",
                print_mode="fit",
                current_event_token=None,
                cafe_mode=True,
                allowlisted=True,
            )

        self.assertEqual(result["outcome"], "authorized")
        self.assertEqual(result["authorization_kind"], "allowlist")
        self.assertEqual(len(connection.executed), 2)

    async def test_command_response_transitions_are_idempotent(self):
        dispatch_connection = FakeConnection((JOB_ID,))
        queued_connection = FakeConnection((JOB_ID,))
        duplicate_connection = FakeConnection(None, (JOB_ID, "queued"))
        with patch(
            "database._connect",
            AsyncMock(side_effect=[
                dispatch_connection,
                queued_connection,
                duplicate_connection,
            ]),
        ):
            dispatch = await database.mark_print_job_dispatching(
                job_id=JOB_ID,
                command_id=COMMAND_ID,
            )
            queued = await database.mark_print_job_queued(command_id=COMMAND_ID)
            duplicate = await database.mark_print_job_queued(command_id=COMMAND_ID)

        self.assertEqual(dispatch["outcome"], "dispatching")
        self.assertEqual(queued["outcome"], "queued")
        self.assertEqual(duplicate["outcome"], "already_finished")
        self.assertEqual(duplicate["status"], "queued")

    async def test_admin_authorization_is_cafe_only_compare_and_set(self):
        row = (
            JOB_ID, 9, "Кафе", "123", "77", "88",
            "authorized", "fill", "cashier",
            "telegram", "456", "guest", "Иван", None, {},
        )
        connection = FakeConnection(row)
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.authorize_print_job_by_admin(
                job_id=JOB_ID,
                current_event_name="Кафе",
            )

        self.assertEqual(result["outcome"], "authorized")
        self.assertEqual(result["authorization_kind"], "cashier")
        self.assertEqual(result["user_provider_user_id"], "456")
        sql, parameters = connection.executed[0]
        self.assertIn("status = 'awaiting_authorization'", sql)
        self.assertIn("authorization_kind = 'cashier'", sql)
        self.assertEqual(parameters, (JOB_ID, "Кафе", "Кафе"))

    async def test_admin_rejection_closes_only_pending_cafe_job(self):
        row = (
            JOB_ID, 9, "Кафе", "123", "77", "88",
            "cancelled", "fit", None,
            "telegram", "456", "guest", "Иван", None, {},
        )
        connection = FakeConnection(row)
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.reject_print_job_by_admin(
                job_id=JOB_ID,
                current_event_name="Кафе",
            )

        self.assertEqual(result["outcome"], "cancelled")
        sql = connection.executed[0][0]
        self.assertIn("close_reason = 'cashier_rejected'", sql)
        self.assertIn("status = 'awaiting_authorization'", sql)

    async def test_cancel_closes_job_before_dispatch(self):
        connection = FakeConnection((JOB_ID,))
        with patch("database._connect", AsyncMock(return_value=connection)):
            result = await database.cancel_print_job(job_id=JOB_ID, user_id=9)

        self.assertEqual(result["outcome"], "cancelled")
        self.assertIn("status = 'cancelled'", connection.executed[0][0])
        self.assertNotIn("'dispatching'", connection.executed[0][0])


if __name__ == "__main__":
    unittest.main()
