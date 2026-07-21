"""Small PostgreSQL access layer shared by messenger integrations."""

import json
import os


PROVIDERS = frozenset({"telegram", "max"})


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
    profile: dict | None = None,
) -> int:
    """Upsert a messenger user and append one idempotent /start event."""
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported bot provider: {provider}")
    external_id = str(provider_user_id).strip()
    if not external_id:
        raise ValueError("provider_user_id is required")
    if start_parameter is not None and not isinstance(start_parameter, str):
        raise TypeError("start_parameter must be a string or None")
    if profile is not None and not isinstance(profile, dict):
        raise TypeError("profile must be an object or None")

    # Import lazily so parser/unit tests do not require a running DB or driver.
    import psycopg

    dsn, kwargs = connection_config()
    connection = await psycopg.AsyncConnection.connect(dsn, **kwargs)
    async with connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                INSERT INTO bot_users (
                    provider, provider_user_id, username, first_name, last_name,
                    profile, first_start_parameter, current_start_parameter
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (provider, provider_user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    profile = bot_users.profile || EXCLUDED.profile,
                    current_start_parameter = EXCLUDED.current_start_parameter,
                    last_seen_at = now()
                RETURNING id
                """,
                (
                    provider,
                    external_id,
                    username,
                    first_name,
                    last_name,
                    json.dumps(profile or {}, ensure_ascii=False),
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
