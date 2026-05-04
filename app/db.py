"""Database connection helpers for the application."""

import os

import psycopg2
from psycopg2 import errorcodes, errors, sql

LOCAL_ENV_NAMES = {"local", "dev", "development", "test"}
APP_ENV = (os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "development").strip().lower()
IS_LOCAL_ENV = APP_ENV in LOCAL_ENV_NAMES


def resolve_db_password() -> str:
    """Resolve the database password for the current environment."""
    configured_password = os.getenv("POSTGRES_PASSWORD")
    if configured_password:
        return configured_password
    if IS_LOCAL_ENV:
        return "postgres"
    raise RuntimeError(
        "La variable POSTGRES_PASSWORD est obligatoire hors environnement local."
    )


def resolve_db_name() -> str:
    """Resolve the target application database name."""
    return os.getenv("POSTGRES_DB") or "dataswarehouse"


def _connection_params(dbname: str) -> dict:
    """Build psycopg2 connection parameters for a database name."""
    return {
        "host": os.getenv("POSTGRES_HOST") or "db",
        "port": os.getenv("POSTGRES_PORT") or "5432",
        "dbname": dbname,
        "user": os.getenv("POSTGRES_USER") or "postgres",
        "password": resolve_db_password(),
    }


def _maintenance_database_candidates(target_db_name: str) -> list[str]:
    """Return databases to try for cluster-level maintenance commands."""
    configured_name = os.getenv("POSTGRES_MAINTENANCE_DB") or "postgres"
    candidates = [configured_name, "template1"]
    return [
        candidate
        for index, candidate in enumerate(candidates)
        if candidate and candidate != target_db_name and candidate not in candidates[:index]
    ]


def _is_missing_database_error(exc: psycopg2.OperationalError) -> bool:
    """Return whether psycopg2 failed because the selected database is missing."""
    message = str(exc).lower()
    return exc.pgcode == errorcodes.INVALID_CATALOG_NAME or (
        "database" in message and "does not exist" in message
    )


def _connect_to_maintenance_database(target_db_name: str):
    """Connect to an existing database in the same PostgreSQL cluster."""
    last_error = None
    for maintenance_db_name in _maintenance_database_candidates(target_db_name):
        try:
            return psycopg2.connect(**_connection_params(maintenance_db_name))
        except psycopg2.OperationalError as exc:
            last_error = exc
            if not _is_missing_database_error(exc):
                raise
    if last_error:
        raise last_error
    raise RuntimeError("Aucune base de maintenance PostgreSQL disponible.")


def ensure_database_exists(db_name: str) -> None:
    """Create the target PostgreSQL database when it is missing."""
    conn = _connect_to_maintenance_database(db_name)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (db_name,))
            if cur.fetchone():
                return

            try:
                cur.execute(
                    sql.SQL("CREATE DATABASE {};").format(sql.Identifier(db_name))
                )
                print(f"[db] base de donnees creee: {db_name}")
            except errors.DuplicateDatabase:
                pass
    finally:
        conn.close()


def get_db_connection():
    """Create and return a PostgreSQL connection."""
    db_name = resolve_db_name()
    try:
        return psycopg2.connect(**_connection_params(db_name))
    except psycopg2.OperationalError as exc:
        if not _is_missing_database_error(exc):
            raise
        ensure_database_exists(db_name)
        return psycopg2.connect(**_connection_params(db_name))
