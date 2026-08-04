"""SQL migration runner and migration state tracking."""

import argparse
import hashlib
import sys
from pathlib import Path

from db import describe_db_target, get_db_connection

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
    "llm_comparator_run",
    "llm_comparator_result",
    "quizbot_config",
    "quizbot_topic",
    "quizbot_session",
    "quizbot_audit_event",
    "webchat_config",
    "webchat_pipeline_step",
    "webchat_session",
    "webchat_message",
    "vectorization_config",
    "task_sequencer_config",
    "task_sequencer_run",
    "document_vision_config",
    "document_vision_run",
    "shard_quality_config",
    "shard_quality_run",
    "auth_token_revocation",
    "business_audit_event",
)

LEGACY_BOOTSTRAP_TABLES = tuple(
    table_name
    for table_name in APPLICATION_BOOTSTRAP_TABLES
    if table_name not in {"auth_token_revocation", "business_audit_event"}
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


def list_missing_application_tables(cur, expected_tables=APPLICATION_BOOTSTRAP_TABLES):
    """Return application bootstrap tables not present in public schema."""
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%s);
        """,
        (list(expected_tables),),
    )
    existing_tables = {row[0] for row in cur.fetchall()}
    return [
        table_name
        for table_name in expected_tables
        if table_name not in existing_tables
    ]


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

        is_legacy_baseline = (
            migration_id == "0001_bootstrap_schema.sql"
            and not list_missing_application_tables(cur, LEGACY_BOOTSTRAP_TABLES)
        )
        if is_legacy_baseline:
            print(f"[migrate] baseline {migration_id} (schema existant)")
        else:
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


def check_migration_state(cur):
    """Fail when a migration is pending, modified, or the schema is incomplete."""
    applied = get_applied_migrations(cur)
    migration_files = {path.name: path for path in list_migration_files()}
    orphaned = sorted(set(applied) - set(migration_files))
    if orphaned:
        raise RuntimeError(
            "Migrations appliquees sans fichier local: " + ", ".join(orphaned)
        )
    pending = []
    for migration_file in migration_files.values():
        migration_id = migration_file.name
        checksum = hashlib.sha256(migration_file.read_bytes()).hexdigest()
        if migration_id not in applied:
            pending.append(migration_id)
        elif applied[migration_id] != checksum:
            raise RuntimeError(f"Checksum mismatch pour la migration '{migration_id}'.")
    if pending:
        raise RuntimeError(f"Migrations en attente: {', '.join(pending)}")
    missing_tables = list_missing_application_tables(cur)
    if missing_tables:
        raise RuntimeError(f"Tables applicatives manquantes: {', '.join(missing_tables)}")


def main(check_only=False):
    """Run the migration command-line entry point."""
    print(f"[migrate] cible PostgreSQL: {describe_db_target()}")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if check_only:
                check_migration_state(cur)
                pgvector_version = get_pgvector_version(cur)
                print(f"[migrate] schema a jour; pgvector={pgvector_version}")
                return
            applied_now = apply_pending_migrations(cur)
            missing_bootstrap_tables = list_missing_application_tables(cur)
            if missing_bootstrap_tables:
                raise RuntimeError(
                    "Schema incomplet apres migrations: "
                    + ", ".join(missing_bootstrap_tables)
                )
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
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--check",
            action="store_true",
            help="Verifie les checksums, migrations en attente et tables attendues.",
        )
        args = parser.parse_args()
        main(check_only=args.check)
    except Exception as exc:
        print(f"[migrate] erreur: {exc}", file=sys.stderr)
        sys.exit(1)
