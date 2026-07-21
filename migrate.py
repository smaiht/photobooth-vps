"""Apply forward-only SQL migrations and record their checksums."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from database import connection_config


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_NAME = re.compile(r"^(\d{4,})_[a-z0-9_]+\.sql$")
LOCK_NAME = "photobooth_schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    checksum: str


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    seen_versions = set()
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise RuntimeError(f"invalid migration filename: {path.name}")
        version = match.group(1)
        if version in seen_versions:
            raise RuntimeError(f"duplicate migration version: {version}")
        seen_versions.add(version)
        payload = path.read_bytes()
        migrations.append(Migration(
            version=version,
            path=path,
            sql=payload.decode("utf-8"),
            checksum=hashlib.sha256(payload).hexdigest(),
        ))
    if not migrations:
        raise RuntimeError(f"no migrations found in {directory}")
    return migrations


def apply_migrations() -> None:
    import psycopg

    migrations = discover_migrations()
    dsn, kwargs = connection_config()
    with psycopg.connect(dsn, autocommit=True, **kwargs) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
        try:
            applied = dict(connection.execute(
                "SELECT version, checksum FROM schema_migrations"
            ).fetchall())
            for migration in migrations:
                old_checksum = applied.get(migration.version)
                if old_checksum is not None:
                    if old_checksum != migration.checksum:
                        raise RuntimeError(
                            f"applied migration was modified: {migration.path.name}"
                        )
                    print(f"migration {migration.path.name}: already applied", flush=True)
                    continue

                print(f"migration {migration.path.name}: applying", flush=True)
                with connection.transaction():
                    connection.execute(migration.sql)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (migration.version, migration.checksum),
                    )
                print(f"migration {migration.path.name}: applied", flush=True)
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))


def main() -> None:
    apply_migrations()


if __name__ == "__main__":
    main()
