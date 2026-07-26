"""Small PostgreSQL access layer shared by messenger integrations."""

import os
import uuid


PROVIDERS = frozenset({"telegram", "max"})
PRINT_MODES = frozenset({"fit", "fill"})
PRINT_COOLDOWN_SECONDS = 5 * 60
TECHNICAL_EVENT_NAME = "Кафе"


async def _connect():
    """Open a database connection without importing psycopg at module import."""
    import psycopg

    dsn, kwargs = connection_config()
    return await psycopg.AsyncConnection.connect(dsn, **kwargs)


def _bot_user_values(
    *,
    provider: str,
    provider_user_id: str | int,
) -> tuple[str, str]:
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported bot provider: {provider}")
    external_id = str(provider_user_id).strip()
    if not external_id:
        raise ValueError("provider_user_id is required")
    return provider, external_id


def _required_text(value: str | int | None, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_text(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _uuid_text(value: str | uuid.UUID, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def connection_config() -> tuple[str, dict]:
    """Return a psycopg DSN/kwargs pair without connecting at import time."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return database_url, {"connect_timeout": 10}

    password = os.environ.get("DB_PASSWORD", "") or os.environ.get(
        "POSTGRES_PASSWORD", "")
    if not password:
        raise RuntimeError("DB_PASSWORD is not configured")
    try:
        port = int(os.environ.get("DB_PORT", "5432"))
    except ValueError as exc:
        raise RuntimeError("DB_PORT must be an integer") from exc

    return "", {
        "host": os.environ.get("DB_HOST", "postgres"),
        "port": port,
        "dbname": os.environ.get("DB_NAME", "")
        or os.environ.get("POSTGRES_DB", "photobooth"),
        "user": os.environ.get("DB_USER", "")
        or os.environ.get("POSTGRES_USER", "photobooth"),
        "password": password,
        "connect_timeout": 10,
    }


async def record_bot_start(
    *,
    provider: str,
    provider_user_id: str | int,
    start_parameter: str | None,
    provider_update_id: str | int | None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> int:
    """Upsert a messenger user and append one idempotent /start event."""
    provider, external_id = _bot_user_values(
        provider=provider,
        provider_user_id=provider_user_id,
    )
    if start_parameter is not None and not isinstance(start_parameter, str):
        raise TypeError("start_parameter must be a string or None")
    start_parameter = _optional_text(start_parameter)

    connection = await _connect()
    async with connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                INSERT INTO bot_users (
                    provider, provider_user_id, username, first_name, last_name,
                    first_start_parameter, current_start_parameter
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider, provider_user_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, bot_users.username),
                    first_name = COALESCE(EXCLUDED.first_name, bot_users.first_name),
                    last_name = COALESCE(EXCLUDED.last_name, bot_users.last_name),
                    first_start_parameter = COALESCE(
                        NULLIF(bot_users.first_start_parameter, ''),
                        NULLIF(EXCLUDED.first_start_parameter, '')
                    ),
                    current_start_parameter = COALESCE(
                        NULLIF(EXCLUDED.current_start_parameter, ''),
                        bot_users.current_start_parameter
                    ),
                    last_seen_at = now()
                RETURNING id
                """,
                (
                    provider,
                    external_id,
                    username,
                    first_name,
                    last_name,
                    start_parameter,
                    start_parameter,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("bot user upsert returned no id")
            user_id = int(row[0])
            await connection.execute(
                """
                INSERT INTO bot_start_events (
                    user_id, start_parameter, provider_update_id
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, provider_update_id)
                    WHERE provider_update_id IS NOT NULL
                    DO NOTHING
                """,
                (
                    user_id,
                    start_parameter,
                    str(provider_update_id) if provider_update_id is not None else None,
                ),
            )
    return user_id


async def ensure_bot_user(
    *,
    provider: str,
    provider_user_id: str | int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> int:
    """Upsert a bot user without changing /start fields or start history."""
    provider, external_id = _bot_user_values(
        provider=provider,
        provider_user_id=provider_user_id,
    )
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            INSERT INTO bot_users (
                provider, provider_user_id, username, first_name, last_name
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (provider, provider_user_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, bot_users.username),
                first_name = COALESCE(EXCLUDED.first_name, bot_users.first_name),
                last_name = COALESCE(EXCLUDED.last_name, bot_users.last_name),
                last_seen_at = now()
            RETURNING id
            """,
            (
                provider,
                external_id,
                username,
                first_name,
                last_name,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("bot user upsert returned no id")
        return int(row[0])


async def user_has_current_start_parameter(
    *,
    provider: str,
    provider_user_id: str | int,
    start_parameter: str,
) -> bool:
    """Return whether a messenger user currently has the expected event token."""
    provider, external_id = _bot_user_values(
        provider=provider,
        provider_user_id=provider_user_id,
    )
    start_parameter = _required_text(start_parameter, "start_parameter")

    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM bot_users
                WHERE provider = %s
                  AND provider_user_id = %s
                  AND current_start_parameter = %s
            )
            """,
            (provider, external_id, start_parameter),
        )
        row = await cursor.fetchone()
        return bool(row[0]) if row is not None else False


async def create_print_job(
    *,
    user_id: int,
    event_name: str,
    conversation_id: str | int,
    source_message_id: str | int | None = None,
    job_id: str | uuid.UUID | None = None,
) -> dict:
    """Create one durable processing job.

    The partial unique index intentionally lets PostgreSQL reject a second open
    job for the same user, including requests handled by another VPS process.
    """
    if int(user_id) <= 0:
        raise ValueError("user_id must be positive")
    job_id = str(uuid.uuid4()) if job_id is None else _uuid_text(job_id, "job_id")
    event_name = _required_text(event_name, "event_name")
    conversation_id = _required_text(conversation_id, "conversation_id")
    source_message_id = _optional_text(source_message_id)

    connection = await _connect()
    async with connection:
        async with connection.transaction():
            # A choice that belongs to an event which is no longer active can
            # never be dispatched. Close it before enforcing the one-open-job
            # rule so an old wedding does not block the user's new event.
            await connection.execute(
                """
                UPDATE print_jobs
                SET status = 'cancelled', closed_at = now(),
                    close_reason = 'event_changed'
                WHERE user_id = %s
                  AND event_name <> %s
                  AND status IN (
                      'processing',
                      'awaiting_choice',
                      'awaiting_authorization',
                      'authorized'
                  )
                """,
                (int(user_id), event_name),
            )
            cursor = await connection.execute(
                """
                INSERT INTO print_jobs (
                    id, user_id, event_name, conversation_id, source_message_id
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id::text, status
                """,
                (
                    job_id,
                    int(user_id),
                    event_name,
                    conversation_id,
                    source_message_id,
                ),
            )
            row = await cursor.fetchone()
            if row is not None:
                return {"outcome": "created", "job_id": row[0], "status": row[1]}
            cursor = await connection.execute(
                """
                SELECT id::text, status
                FROM print_jobs
                WHERE user_id = %s
                  AND status IN (
                      'processing',
                      'awaiting_choice',
                      'awaiting_authorization',
                      'authorized',
                      'dispatching'
                  )
                LIMIT 1
                """,
                (int(user_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("print job conflict has no existing open job")
            return {
                "outcome": "already_open",
                "job_id": row[0],
                "status": row[1],
            }


async def mark_print_job_awaiting_choice(
    *,
    job_id: str | uuid.UUID,
    choice_message_id: str | int | None,
) -> dict:
    """Move a processed job to the state exposed by fit/fill buttons."""
    job_id = _uuid_text(job_id, "job_id")
    choice_message_id = _optional_text(choice_message_id)
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            UPDATE print_jobs
            SET status = 'awaiting_choice', choice_message_id = %s
            WHERE id = %s AND status = 'processing'
            RETURNING id::text
            """,
            (choice_message_id, job_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return {
                "outcome": "awaiting_choice",
                "job_id": row[0],
                "status": "awaiting_choice",
            }
        cursor = await connection.execute(
            "SELECT status FROM print_jobs WHERE id = %s",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"outcome": "not_found", "job_id": job_id}
        return {"outcome": "not_processing", "job_id": job_id, "status": row[0]}


async def claim_print_job_choice(
    *,
    job_id: str | uuid.UUID,
    user_id: int,
    current_event_name: str,
    print_mode: str,
    current_event_token: str | None,
    cafe_mode: bool,
    allowlisted: bool = False,
    automatic: bool = False,
) -> dict:
    """Atomically claim one processed fit/fill mode and apply access policy.

    Outcomes are ``authorized``, ``awaiting_authorization``, ``cooldown``,
    ``access_denied``, ``event_changed``, ``not_owner``, ``already_claimed`` or
    ``not_found``. Rejected access/cooldown attempts leave the job in
    ``awaiting_choice`` so a guest can scan the valid QR or retry later.
    """
    job_id = _uuid_text(job_id, "job_id")
    if int(user_id) <= 0:
        raise ValueError("user_id must be positive")
    current_event_name = _required_text(current_event_name, "current_event_name")
    if print_mode not in PRINT_MODES:
        raise ValueError("print_mode must be 'fit' or 'fill'")
    current_event_token = _optional_text(current_event_token)
    source_status = "processing" if automatic else "awaiting_choice"

    connection = await _connect()
    async with connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                SELECT
                    print_jobs.user_id,
                    print_jobs.event_name,
                    print_jobs.status,
                    print_jobs.print_mode,
                    print_jobs.authorization_kind,
                    bot_users.current_start_parameter
                FROM print_jobs
                JOIN bot_users ON bot_users.id = print_jobs.user_id
                WHERE print_jobs.id = %s
                FOR UPDATE OF print_jobs, bot_users
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return {"outcome": "not_found", "job_id": job_id}

            owner_id, event_name, status, old_mode, old_auth, user_token = row
            if int(owner_id) != int(user_id):
                return {"outcome": "not_owner", "job_id": job_id}
            if event_name != current_event_name:
                return {
                    "outcome": "event_changed",
                    "job_id": job_id,
                    "job_event_name": event_name,
                    "current_event_name": current_event_name,
                }
            if status != source_status:
                return {
                    "outcome": "already_claimed",
                    "job_id": job_id,
                    "status": status,
                    "print_mode": old_mode,
                    "authorization_kind": old_auth,
                }

            if allowlisted:
                next_status = "authorized"
                authorization_kind = "allowlist"
            elif cafe_mode:
                next_status = "awaiting_authorization"
                authorization_kind = None
            elif current_event_token is None or user_token != current_event_token:
                return {"outcome": "access_denied", "job_id": job_id}
            else:
                cursor = await connection.execute(
                    """
                    SELECT GREATEST(
                        0,
                        CEIL(EXTRACT(EPOCH FROM (
                            queued_at + make_interval(secs => %s) - now()
                        )))::integer
                    )
                    FROM print_jobs
                    WHERE user_id = %s
                      AND event_name = %s
                      AND status = 'queued'
                      AND authorization_kind = 'event'
                      AND queued_at > now() - make_interval(secs => %s)
                    ORDER BY queued_at DESC
                    LIMIT 1
                    """,
                    (
                        PRINT_COOLDOWN_SECONDS,
                        int(user_id),
                        current_event_name,
                        PRINT_COOLDOWN_SECONDS,
                    ),
                )
                cooldown = await cursor.fetchone()
                if cooldown is not None and int(cooldown[0]) > 0:
                    return {
                        "outcome": "cooldown",
                        "job_id": job_id,
                        "retry_after_seconds": int(cooldown[0]),
                    }
                next_status = "authorized"
                authorization_kind = "event"

            cursor = await connection.execute(
                """
                UPDATE print_jobs
                SET status = %s,
                    print_mode = %s,
                    authorization_kind = %s,
                    selected_at = now(),
                    authorized_at = CASE
                        WHEN %s = 'authorized' THEN now()
                        ELSE NULL
                    END
                WHERE id = %s AND status = %s
                RETURNING id::text
                """,
                (
                    next_status,
                    print_mode,
                    authorization_kind,
                    next_status,
                    job_id,
                    source_status,
                ),
            )
            if await cursor.fetchone() is None:
                raise RuntimeError("locked print job changed unexpectedly")
            return {
                "outcome": next_status,
                "job_id": job_id,
                "status": next_status,
                "print_mode": print_mode,
                "authorization_kind": authorization_kind,
            }


async def recover_interrupted_print_jobs() -> int:
    """Close local-only states which cannot continue after a VPS restart.

    ``awaiting_choice`` and ``awaiting_authorization`` deliberately remain
    active without a time limit. ``dispatching`` remains untouched because its
    command may already be on the booth or its response may still arrive.
    """
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            WITH recovered AS (
                UPDATE print_jobs
                SET status = 'failed', closed_at = now(),
                    close_reason = 'vps_restarted',
                    last_error = COALESCE(
                        last_error,
                        'VPS restarted before print command dispatch'
                    )
                WHERE status IN ('processing', 'authorized')
                RETURNING 1
            )
            SELECT count(*) FROM recovered
            """
        )
        row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0


def _admin_print_job_from_row(row) -> dict:
    return {
        "job_id": row[0],
        "user_id": int(row[1]),
        "event_name": row[2],
        "conversation_id": row[3],
        "source_message_id": row[4],
        "choice_message_id": row[5],
        "status": row[6],
        "print_mode": row[7],
        "authorization_kind": row[8],
        "provider": row[9],
        "provider_user_id": row[10],
        "user_provider_user_id": row[10],
        "username": row[11],
        "first_name": row[12],
        "last_name": row[13],
    }


async def authorize_print_job_by_admin(
    *,
    job_id: str | uuid.UUID,
    current_event_name: str,
) -> dict:
    """Authorize one pending Cafe job with a compare-and-set transition."""
    job_id = _uuid_text(job_id, "job_id")
    current_event_name = _required_text(current_event_name, "current_event_name")
    connection = await _connect()
    async with connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                WITH changed AS (
                    UPDATE print_jobs
                    SET status = 'authorized',
                        authorization_kind = 'cashier',
                        authorized_at = now()
                    WHERE id = %s
                      AND event_name = %s
                      AND %s = 'Кафе'
                      AND status = 'awaiting_authorization'
                    RETURNING *
                )
                SELECT
                    changed.id::text,
                    changed.user_id,
                    changed.event_name,
                    changed.conversation_id,
                    changed.source_message_id,
                    changed.choice_message_id,
                    changed.status,
                    changed.print_mode,
                    changed.authorization_kind,
                    bot_users.provider,
                    bot_users.provider_user_id,
                    bot_users.username,
                    bot_users.first_name,
                    bot_users.last_name
                FROM changed
                JOIN bot_users ON bot_users.id = changed.user_id
                """,
                (job_id, current_event_name, current_event_name),
            )
            row = await cursor.fetchone()
            if row is not None:
                return {"outcome": "authorized", **_admin_print_job_from_row(row)}

            cursor = await connection.execute(
                """
                SELECT
                    print_jobs.id::text,
                    print_jobs.user_id,
                    print_jobs.event_name,
                    print_jobs.conversation_id,
                    print_jobs.source_message_id,
                    print_jobs.choice_message_id,
                    print_jobs.status,
                    print_jobs.print_mode,
                    print_jobs.authorization_kind,
                    bot_users.provider,
                    bot_users.provider_user_id,
                    bot_users.username,
                    bot_users.first_name,
                    bot_users.last_name
                FROM print_jobs
                JOIN bot_users ON bot_users.id = print_jobs.user_id
                WHERE print_jobs.id = %s
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return {"outcome": "not_found", "job_id": job_id}
            job = _admin_print_job_from_row(row)
            if (
                current_event_name != TECHNICAL_EVENT_NAME
                or job["event_name"] != current_event_name
            ):
                return {
                    "outcome": "event_changed",
                    **job,
                    "job_event_name": job["event_name"],
                    "current_event_name": current_event_name,
                }
            return {"outcome": "already_decided", **job}


async def reject_print_job_by_admin(
    *,
    job_id: str | uuid.UUID,
    current_event_name: str,
) -> dict:
    """Reject one pending Cafe job with a compare-and-set transition."""
    job_id = _uuid_text(job_id, "job_id")
    current_event_name = _required_text(current_event_name, "current_event_name")
    connection = await _connect()
    async with connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                WITH changed AS (
                    UPDATE print_jobs
                    SET status = 'cancelled',
                        closed_at = now(),
                        close_reason = 'cashier_rejected'
                    WHERE id = %s
                      AND event_name = %s
                      AND %s = 'Кафе'
                      AND status = 'awaiting_authorization'
                    RETURNING *
                )
                SELECT
                    changed.id::text,
                    changed.user_id,
                    changed.event_name,
                    changed.conversation_id,
                    changed.source_message_id,
                    changed.choice_message_id,
                    changed.status,
                    changed.print_mode,
                    changed.authorization_kind,
                    bot_users.provider,
                    bot_users.provider_user_id,
                    bot_users.username,
                    bot_users.first_name,
                    bot_users.last_name
                FROM changed
                JOIN bot_users ON bot_users.id = changed.user_id
                """,
                (job_id, current_event_name, current_event_name),
            )
            row = await cursor.fetchone()
            if row is not None:
                return {"outcome": "cancelled", **_admin_print_job_from_row(row)}

            cursor = await connection.execute(
                """
                SELECT
                    print_jobs.id::text,
                    print_jobs.user_id,
                    print_jobs.event_name,
                    print_jobs.conversation_id,
                    print_jobs.source_message_id,
                    print_jobs.choice_message_id,
                    print_jobs.status,
                    print_jobs.print_mode,
                    print_jobs.authorization_kind,
                    bot_users.provider,
                    bot_users.provider_user_id,
                    bot_users.username,
                    bot_users.first_name,
                    bot_users.last_name
                FROM print_jobs
                JOIN bot_users ON bot_users.id = print_jobs.user_id
                WHERE print_jobs.id = %s
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return {"outcome": "not_found", "job_id": job_id}
            job = _admin_print_job_from_row(row)
            if (
                current_event_name != TECHNICAL_EVENT_NAME
                or job["event_name"] != current_event_name
            ):
                return {
                    "outcome": "event_changed",
                    **job,
                    "job_event_name": job["event_name"],
                    "current_event_name": current_event_name,
                }
            return {"outcome": "already_decided", **job}


async def mark_print_job_dispatching(
    *,
    job_id: str | uuid.UUID,
    command_id: str | uuid.UUID,
) -> dict:
    """Attach a command UUID and reserve an authorized job for dispatch."""
    job_id = _uuid_text(job_id, "job_id")
    command_id = _uuid_text(command_id, "command_id")
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            UPDATE print_jobs
            SET status = 'dispatching', command_id = %s
            WHERE id = %s AND status = 'authorized'
            RETURNING id::text
            """,
            (command_id, job_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return {
                "outcome": "dispatching",
                "job_id": row[0],
                "status": "dispatching",
                "command_id": command_id,
            }
        cursor = await connection.execute(
            "SELECT status, command_id::text FROM print_jobs WHERE id = %s",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"outcome": "not_found", "job_id": job_id}
        if row[0] == "dispatching" and row[1] == command_id:
            return {
                "outcome": "already_dispatching",
                "job_id": job_id,
                "status": row[0],
                "command_id": row[1],
            }
        return {
            "outcome": "not_authorized",
            "job_id": job_id,
            "status": row[0],
            "command_id": row[1],
        }


async def cancel_print_job(
    *,
    job_id: str | uuid.UUID,
    user_id: int,
    close_reason: str = "user_cancelled",
) -> dict:
    """Cancel a user's job unless booth dispatch has already begun."""
    job_id = _uuid_text(job_id, "job_id")
    if int(user_id) <= 0:
        raise ValueError("user_id must be positive")
    close_reason = _required_text(close_reason, "close_reason")
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            UPDATE print_jobs
            SET status = 'cancelled', closed_at = now(), close_reason = %s
            WHERE id = %s
              AND user_id = %s
              AND status IN (
                  'processing',
                  'awaiting_choice',
                  'awaiting_authorization',
                  'authorized'
              )
            RETURNING id::text
            """,
            (close_reason, job_id, int(user_id)),
        )
        row = await cursor.fetchone()
        if row is not None:
            return {
                "outcome": "cancelled",
                "job_id": row[0],
                "status": "cancelled",
            }
        cursor = await connection.execute(
            "SELECT user_id, status FROM print_jobs WHERE id = %s",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"outcome": "not_found", "job_id": job_id}
        if int(row[0]) != int(user_id):
            return {"outcome": "not_owner", "job_id": job_id}
        return {"outcome": "not_cancellable", "job_id": job_id, "status": row[1]}


async def fail_print_job_before_dispatch(
    *,
    job_id: str | uuid.UUID,
    last_error: str,
    close_reason: str = "local_processing_failed",
) -> dict:
    """Release an open job after a local download/render/send failure."""
    job_id = _uuid_text(job_id, "job_id")
    last_error = _required_text(last_error, "last_error")
    close_reason = _required_text(close_reason, "close_reason")
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            UPDATE print_jobs
            SET status = 'failed', closed_at = now(), close_reason = %s,
                last_error = %s
            WHERE id = %s
              AND status IN (
                  'processing',
                  'awaiting_choice',
                  'awaiting_authorization',
                  'authorized'
              )
            RETURNING id::text
            """,
            (close_reason, last_error, job_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return {
                "outcome": "failed",
                "job_id": row[0],
                "status": "failed",
            }
        cursor = await connection.execute(
            "SELECT status FROM print_jobs WHERE id = %s",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"outcome": "not_found", "job_id": job_id}
        return {"outcome": "not_closeable", "job_id": job_id, "status": row[0]}


async def mark_print_job_queued(*, command_id: str | uuid.UUID) -> dict:
    """Record a successful booth command response idempotently."""
    command_id = _uuid_text(command_id, "command_id")
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            UPDATE print_jobs
            SET status = 'queued', queued_at = now(), closed_at = now()
            WHERE command_id = %s AND status = 'dispatching'
            RETURNING id::text
            """,
            (command_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            return {
                "outcome": "queued",
                "job_id": row[0],
                "status": "queued",
                "command_id": command_id,
            }
        return await _command_transition_fallback(
            connection=connection,
            command_id=command_id,
            requested_outcome="queued",
        )


async def mark_print_job_failed(
    *,
    command_id: str | uuid.UUID,
    last_error: str,
) -> dict:
    """Record a failed booth command response without changing queued jobs."""
    command_id = _uuid_text(command_id, "command_id")
    last_error = _required_text(last_error, "last_error")
    connection = await _connect()
    async with connection:
        cursor = await connection.execute(
            """
            UPDATE print_jobs
            SET status = 'failed', last_error = %s, closed_at = now(),
                close_reason = 'command_failed'
            WHERE command_id = %s AND status = 'dispatching'
            RETURNING id::text
            """,
            (last_error, command_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return {
                "outcome": "failed",
                "job_id": row[0],
                "status": "failed",
                "command_id": command_id,
            }
        return await _command_transition_fallback(
            connection=connection,
            command_id=command_id,
            requested_outcome="failed",
        )


async def _command_transition_fallback(
    *,
    connection,
    command_id: str,
    requested_outcome: str,
) -> dict:
    cursor = await connection.execute(
        "SELECT id::text, status FROM print_jobs WHERE command_id = %s",
        (command_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return {"outcome": "not_found", "command_id": command_id}
    return {
        "outcome": "already_finished"
        if row[1] in {"queued", "failed", "cancelled"}
        else f"not_{requested_outcome}",
        "job_id": row[0],
        "status": row[1],
        "command_id": command_id,
    }
