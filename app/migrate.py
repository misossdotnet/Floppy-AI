"""SQL migration runner and migration state tracking."""

import hashlib
import os
from pathlib import Path

import psycopg2

LOCAL_ENV_NAMES = {"local", "dev", "development", "test"}
APP_ENV = (os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "development").strip().lower()
MIGRATIONS_TABLE = "schema_migrations"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def is_local_env() -> bool:
    """Return whether the current environment is local-like."""
    return APP_ENV in LOCAL_ENV_NAMES


def resolve_db_password() -> str:
    """Resolve the database password for the current environment."""
    configured_password = os.getenv("POSTGRES_PASSWORD")
    if configured_password:
        return configured_password
    if is_local_env():
        return "postgres"
    raise RuntimeError(
        "La variable POSTGRES_PASSWORD est obligatoire hors environnement local."
    )


def get_db_connection():
    """Create and return a PostgreSQL connection."""
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "db"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "dataswarehouse"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=resolve_db_password(),
    )


def ensure_migrations_table(cur):
    """Create the migrations tracking table if needed."""
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{MIGRATIONS_TABLE} (
            migration_id text PRIMARY KEY,
            checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def get_applied_migrations(cur):
    """Load already applied migrations with checksums."""
    ensure_migrations_table(cur)
    cur.execute(f"SELECT migration_id, checksum FROM public.{MIGRATIONS_TABLE};")
    rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}


def list_migration_files():
    """List migration SQL files ordered by filename."""
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(path for path in MIGRATIONS_DIR.glob("*.sql") if path.is_file())


def apply_pending_migrations(cur):
    """Apply pending migrations and record their checksums."""
    applied_migrations = get_applied_migrations(cur)
    applied_now = []

    for migration_file in list_migration_files():
        migration_id = migration_file.name
        sql_payload = migration_file.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql_payload.encode("utf-8")).hexdigest()

        if migration_id in applied_migrations:
            if applied_migrations[migration_id] != checksum:
                raise RuntimeError(
                    f"Checksum mismatch pour la migration '{migration_id}'."
                )
            continue

        if not sql_payload.strip():
            raise RuntimeError(f"La migration '{migration_id}' est vide.")

        print(f"[migrate] apply {migration_id}")
        cur.execute(sql_payload)
        cur.execute(
            f"""
            INSERT INTO public.{MIGRATIONS_TABLE} (migration_id, checksum)
            VALUES (%s, %s);
            """,
            (migration_id, checksum),
        )
        applied_now.append(migration_id)

    return applied_now


def main():
    """Run the migration command-line entry point."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            applied_now = apply_pending_migrations(cur)

    if applied_now:
        print("[migrate] migrations appliquees:")
        for migration_id in applied_now:
            print(f"- {migration_id}")
    else:
        print("[migrate] aucune migration en attente")


if __name__ == "__main__":
    main()
