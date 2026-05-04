"""SQL migration runner and migration state tracking."""

import hashlib
import sys
from pathlib import Path

from db import get_db_connection

MIGRATIONS_TABLE = "schema_migrations"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
APPLICATION_BOOTSTRAP_TABLES = (
    "project",
    "document_registry",
    "document_processing",
    "chunk_metadata",
    "dataset_build",
    "document_review_annotation",
    "document_section_exclusion",
    "llm_config",
    "llm_audit_session",
    "llm_audit_exchange",
    "quizbot_config",
    "quizbot_topic",
    "quizbot_session",
    "quizbot_audit_event",
    "webchat_config",
    "webchat_pipeline_step",
    "webchat_session",
    "webchat_message",
    "vectorization_config",
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


def list_missing_application_tables(cur):
    """Return application bootstrap tables not present in public schema."""
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%s);
        """,
        (list(APPLICATION_BOOTSTRAP_TABLES),),
    )
    existing_tables = {row[0] for row in cur.fetchall()}
    return [
        table_name
        for table_name in APPLICATION_BOOTSTRAP_TABLES
        if table_name not in existing_tables
    ]


def ensure_application_schema(cur):
    """Create and synchronize global application tables."""
    from llm_gateway import ensure_llm_tables
    from quizbot import ensure_quizbot_tables
    from services import (
        ensure_business_tables,
        ensure_pgvector_extension,
        ensure_projects_table,
    )
    from vectorization import ensure_vectorization_tables
    from webchat import ensure_webchat_tables

    ensure_pgvector_extension(cur)
    ensure_projects_table(cur)
    ensure_business_tables(cur)
    ensure_llm_tables(cur)
    ensure_quizbot_tables(cur)
    ensure_webchat_tables(cur)
    ensure_vectorization_tables(cur)


def get_pgvector_version(cur):
    """Return installed pgvector extension version."""
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
    row = cur.fetchone()
    if not row:
        raise RuntimeError(
            "Extension PostgreSQL 'vector' indisponible apres initialisation. "
            "Utilisez une image PostgreSQL avec pgvector, par exemple pgvector/pgvector:pg16."
        )
    return row[0]


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
            missing_bootstrap_tables = list_missing_application_tables(cur)
            ensure_application_schema(cur)
            pgvector_version = get_pgvector_version(cur)

    print(f"[migrate] pgvector disponible: {pgvector_version}")

    if applied_now:
        print("[migrate] migrations appliquees:")
        for migration_id in applied_now:
            print(f"- {migration_id}")

    if missing_bootstrap_tables:
        print("[migrate] schema applicatif initialise/verifie:")
        for table_name in missing_bootstrap_tables:
            print(f"- public.{table_name}")

    if not applied_now and not missing_bootstrap_tables:
        print("[migrate] aucune migration en attente")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[migrate] erreur: {exc}", file=sys.stderr)
        sys.exit(1)
