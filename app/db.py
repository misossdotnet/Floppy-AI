"""Database connection helpers for the application."""

import os

import psycopg2

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


def get_db_connection():
    """Create and return a PostgreSQL connection."""
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "db"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "dataswarehouse"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=resolve_db_password(),
    )
