"""Core business services, validation, and shared helpers."""

import os
import re
import unicodedata
import json
import html
import hashlib
import hmac
import logging
import math
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from psycopg2.extras import Json
from psycopg2 import sql
from db import get_db_connection

PROJECTS_TABLE = "project"
DOCUMENT_PROCESSING_TABLE = "document_processing"
CHUNK_METADATA_TABLE = "chunk_metadata"
DATASET_BUILD_TABLE = "dataset_build"
DOCUMENT_REGISTRY_TABLE = "document_registry"
DOCUMENT_REVIEW_ANNOTATION_TABLE = "document_review_annotation"
DOCUMENT_SECTION_EXCLUSION_TABLE = "document_section_exclusion"
QUALITY_OBSERVATION_TABLE = "quality_observation"
DEFAULT_NORMALIZATION_VERSION = "v2"
QUALITY_RULESET_VERSION = "quality-firewall/v1"
QUALITY_HASH_ENCODING_VERSION = "utf8-sha256/v1"
QUALITY_EVIDENCE_MAX_BYTES = 2048
QUALITY_DUPLICATE_SCORE_DELTA = -0.15
NORMALIZATION_STAGES = (
    "line_endings",
    "html_cleanup",
    "tables",
    "code_blocks",
    "lists",
    "whitespace",
    "references",
    "language",
    "content_type",
)
DEFAULT_NORMALIZATION_OPTIONS = {
    "enabled_stages": list(NORMALIZATION_STAGES),
    "heading_max_level": 6,
    "preserve_code_blocks": True,
}
LOCAL_ENV_NAMES = {"local", "dev", "development", "test"}
APP_ENV = (os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "development").strip().lower()
IS_LOCAL_ENV = APP_ENV in LOCAL_ENV_NAMES

DEFAULT_CHUNK_OPTIONS = {
    "chunkMaxTokens": 350,
    "chunkOverlapTokens": 50,
    "hardMaxTokens": 420,
    "headingMaxLevel": 3,
    "tokenEstimator": "words",
    "charsPerToken": 4,
    "codeAware": True,
    "tableAware": True,
    "mergeSmallParagraphs": True,
    "smallParagraphMinTokens": 40,
    "strictZoneTypes": ["code", "table", "strict"],
}

OPERATION_INPUT_SCHEMAS = {
    "import_documents": {
        "required": ["project_slug", "documents"],
        "fields": {
            "project_slug": {"type": "string", "allow_empty": False},
            "documents": {"type": "array", "items_type": "object", "min_items": 1},
        },
    },
    "normalize_document": {
        "required": ["document_id"],
        "fields": {
            "document_id": {"type": "string", "allow_empty": False},
            "project_slug": {"type": "string", "allow_empty": True, "default": ""},
            "normalization_version": {"type": "string", "allow_empty": True, "default": ""},
            "normalization_options": {"type": "object", "default": {}},
        },
    },
    "chunk_project": {
        "required": ["project_slug"],
        "fields": {
            "project_slug": {"type": "string", "allow_empty": False},
            "chunkMaxTokens": {"type": "integer", "min": 1},
            "chunkOverlapTokens": {"type": "integer", "min": 0},
            "hardMaxTokens": {"type": "integer", "min": 1},
            "headingMaxLevel": {"type": "integer", "min": 1, "max": 6},
            "tokenEstimator": {
                "type": "string",
                "allow_empty": True,
                "lower": True,
                "enum": ["words", "chars"],
            },
            "charsPerToken": {"type": "integer", "min": 1},
            "codeAware": {"type": "boolean", "default": True},
            "tableAware": {"type": "boolean", "default": True},
            "mergeSmallParagraphs": {"type": "boolean", "default": True},
            "smallParagraphMinTokens": {"type": "integer", "min": 1, "default": 40},
            "strictZoneTypes": {"type": "array", "default": ["code", "table", "strict"]},
        },
    },
    "build_dataset": {
        "required": ["project_slug"],
        "fields": {
            "project_slug": {"type": "string", "allow_empty": False},
            "quality_min": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.0},
            "approved_only": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "min": 1, "default": 2000},
        },
    },
    "get_dataset_build": {
        "required": ["build_id"],
        "fields": {
            "build_id": {"type": "string", "allow_empty": False},
        },
    },
    "search_chunks": {
        "required": ["project_slug"],
        "fields": {
            "project_slug": {"type": "string", "allow_empty": False},
            "quality_min": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.0},
            "limit": {"type": "integer", "min": 1, "max": 10000, "default": 100},
            "offset": {"type": "integer", "min": 0, "default": 0},
        },
    },
    "get_document_lineage": {
        "required": ["document_id"],
        "fields": {
            "document_id": {"type": "string", "allow_empty": False},
            "project_slug": {"type": "string", "allow_empty": True, "default": ""},
        },
    },
    "approve_document": {
        "required": ["document_id"],
        "fields": {
            "document_id": {"type": "string", "allow_empty": False},
            "project_slug": {"type": "string", "allow_empty": True, "default": ""},
            "status": {
                "type": "string",
                "allow_empty": True,
                "lower": True,
                "enum": ["pending", "approved", "rejected"],
                "default": "approved",
            },
            "comment": {"type": "string", "allow_empty": True, "default": ""},
            "approved_by": {"type": "string", "allow_empty": True, "default": ""},
        },
    },
}

MCP_TOOL_ACL_SCOPES = {
    "floppy.import_documents": {"imports"},
    "floppy.normalize_document": {"normalize"},
    "floppy.chunk_project": {"chunk"},
    "floppy.build_dataset": {"build_dataset"},
    "floppy.approve_document": {"approve"},
}

MCP_TOOL_ACL_ANY_SCOPES = {
    "floppy.get_dataset_build": {"build_dataset"},
    "floppy.search_chunks": {"approve", "build_dataset", "chunk"},
    "floppy.get_document_lineage": {"approve", "chunk"},
}

CHAT_RATING_FIELDS = (
    "rating_relevance",
    "rating_accuracy",
    "rating_clarity",
    "rating_completeness",
    "rating_helpfulness",
)

CHAT_RATING_LABELS = {
    "rating_relevance": "Pertinence",
    "rating_accuracy": "Exactitude",
    "rating_clarity": "Clarte",
    "rating_completeness": "Completude",
    "rating_helpfulness": "Utilite",
}

DEFAULT_ERROR_MESSAGES = {
    "bad_request": "Requete invalide.",
    "validation_error": "Certaines donnees sont invalides.",
    "not_found": "Ressource introuvable.",
    "unauthorized": "Authentification requise.",
    "forbidden": "Permissions insuffisantes.",
    "internal_error": "Une erreur interne est survenue.",
}

LOGGER = logging.getLogger("floppy_ai")
if not LOGGER.handlers:
    log_level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    logging.basicConfig(level=getattr(logging, log_level_name, logging.INFO))


def parse_env_bool(value, default: bool = False) -> bool:
    """Parse a boolean-like environment value."""
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "oui", "on"}:
        return True
    if normalized in {"0", "false", "no", "non", "off"}:
        return False
    return default


def resolve_flask_secret_key() -> str:
    """Resolve the Flask secret key for the current environment."""
    configured_secret = (os.getenv("FLASK_SECRET_KEY") or "").strip()
    if configured_secret:
        return configured_secret
    if IS_LOCAL_ENV:
        return f"local-dev-{uuid4().hex}"
    raise RuntimeError(
        "La variable FLASK_SECRET_KEY est obligatoire hors environnement local."
    )


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


def is_debug_enabled() -> bool:
    """Return whether Flask debug mode should be enabled."""
    if not IS_LOCAL_ENV:
        return False
    return parse_env_bool(os.getenv("FLASK_DEBUG"), default=True)


def is_auth_enforced() -> bool:
    """Return whether authentication enforcement is active."""
    explicit = os.getenv("FLOPPY_REQUIRE_AUTH")
    return parse_env_bool(explicit, default=not IS_LOCAL_ENV)


def resolve_admin_username() -> str:
    """Return the configured admin username."""
    return (
        os.getenv("FLOPPY_ADMIN_USERNAME")
        or os.getenv("ADMIN_USERNAME")
        or "admin"
    ).strip()


def resolve_admin_password() -> str:
    """Return the configured admin password."""
    password = (
        os.getenv("FLOPPY_ADMIN_PASSWORD")
        or os.getenv("ADMIN_PASSWORD")
        or ""
    )
    if password:
        return password
    if IS_LOCAL_ENV:
        return "admin"
    raise RuntimeError(
        "La variable FLOPPY_ADMIN_PASSWORD est obligatoire hors environnement local."
    )


def verify_admin_credentials(username: str, password: str) -> bool:
    """Verify admin credentials using constant-time comparisons."""
    expected_username = resolve_admin_username()
    expected_password = resolve_admin_password()
    return hmac.compare_digest(username or "", expected_username) and hmac.compare_digest(
        password or "",
        expected_password,
    )


def login_admin_user(username: str):
    """Open an admin browser session."""
    session.clear()
    session["admin_authenticated"] = True
    session["admin_username"] = username
    session["admin_role"] = "admin"


def logout_admin_user():
    """Close the admin browser session."""
    session.clear()


def is_admin_authenticated() -> bool:
    """Return whether the current browser session is authenticated for admin UI."""
    return bool(session.get("admin_authenticated"))


def safe_next_url(raw_next: str, default_endpoint: str = "admin_dashboard") -> str:
    """Resolve a local next URL for login redirects."""
    candidate = (raw_next or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return url_for(default_endpoint)


def admin_auth_required_response():
    """Return the right unauthorized response for admin UI routes."""
    if request.is_json:
        return api_error_response(status_code=401, code="unauthorized")
    flash("Authentification administration requise.", "error")
    return redirect(url_for("admin_login", next=(request.full_path or request.path).rstrip("?")))


def normalize_scopes(raw_scopes):
    """Normalize scopes."""
    if raw_scopes is None:
        return set()
    if isinstance(raw_scopes, str):
        parts = [item.strip().lower() for item in raw_scopes.split("|")]
        return {item for item in parts if item}
    if isinstance(raw_scopes, (list, tuple, set)):
        scopes = set()
        for item in raw_scopes:
            normalized = str(item).strip().lower()
            if normalized:
                scopes.add(normalized)
        return scopes
    return set()


def load_auth_tokens():
    """Load API tokens and associated scopes from environment."""
    raw_tokens = (os.getenv("FLOPPY_AUTH_TOKENS") or "").strip()
    if not raw_tokens:
        return {}

    parsed_tokens = {}
    try:
        decoded = json.loads(raw_tokens)
    except ValueError:
        decoded = None

    if isinstance(decoded, dict):
        for token_value, scopes in decoded.items():
            token = str(token_value).strip()
            normalized_scopes = normalize_scopes(scopes)
            if token and normalized_scopes:
                parsed_tokens[token] = normalized_scopes
        return parsed_tokens

    for chunk in raw_tokens.split(","):
        token_part, _, scopes_part = chunk.strip().partition(":")
        token = token_part.strip()
        if not token:
            continue
        parsed_tokens[token] = normalize_scopes(scopes_part or "admin")

    return parsed_tokens


def extract_request_token() -> str:
    """Extract the caller token from supported request headers only."""
    authorization_header = (request.headers.get("Authorization") or "").strip()
    if authorization_header.lower().startswith("bearer "):
        return authorization_header[7:].strip()

    for header_name in ("X-Floppy-Token", "X-Api-Token", "X-API-Token"):
        header_value = (request.headers.get(header_name) or "").strip()
        if header_value:
            return header_value

    return ""


def resolve_token_scopes(token):
    """Resolve scopes from a signed JWT first, then legacy static tokens."""
    if token.count(".") == 2:
        from security import decode_jwt
        claims = decode_jwt(token, "access")
        if claims:
            g.auth_claims = claims
            return normalize_scopes(claims.get("scopes"))
        return None
    scopes = load_auth_tokens().get(token)
    if scopes:
        # Static secrets must never appear in logs. A stable fingerprint still
        # makes their business-audit events attributable and searchable.
        g.auth_claims = {
            "sub": f"static:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:12]}",
            "scopes": sorted(scopes),
            "type": "static",
        }
    return scopes


def is_api_or_mcp_request() -> bool:
    """Return whether api or mcp request."""
    return request.path.startswith("/api/") or request.path.startswith("/mcp") or request.is_json


def public_exception_message(exc, fallback: str) -> str:
    """Run public exception message."""
    if exc.args:
        first_arg = exc.args[0]
        if isinstance(first_arg, str):
            cleaned = first_arg.strip()
            if cleaned:
                return cleaned
    return fallback


def log_internal_error(context: str, exc: Exception) -> str:
    """Run log internal error."""
    error_id = uuid4().hex[:12]
    LOGGER.exception("error_id=%s context=%s", error_id, context)
    return error_id


def ui_internal_error_message(context: str, exc: Exception) -> str:
    """Run ui internal error message."""
    error_id = log_internal_error(context, exc)
    return f"{DEFAULT_ERROR_MESSAGES['internal_error']} Reference: {error_id}"


def flash_internal_error(context: str, exc: Exception, prefix: str = ""):
    """Run flash internal error."""
    message = ui_internal_error_message(context, exc)
    if prefix:
        flash(f"{prefix} {message}", "error")
        return
    flash(message, "error")


def api_error_response(message: str = "", status_code: int = 400, code: str = "bad_request", details=None):
    """Handle the api error response request."""
    normalized_message = message or DEFAULT_ERROR_MESSAGES.get(code, DEFAULT_ERROR_MESSAGES["bad_request"])
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "message": normalized_message,
        },
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload, status_code


def api_internal_error_response(context: str, exc: Exception):
    """Handle the api internal error response request."""
    error_id = log_internal_error(context, exc)
    return api_error_response(
        status_code=500,
        code="internal_error",
        details={"error_id": error_id},
    )


def mcp_error_result(code: str, message: str, details=None):
    """Handle the mcp error result request."""
    payload = {
        "error": {
            "code": code,
            "message": message,
        }
    }
    if details is not None:
        payload["error"]["details"] = details
    return mcp_tool_result_payload(payload, is_error=True)


def auth_failure_response(status_code: int, message: str):
    """Run auth failure response."""
    if request.path.startswith("/mcp"):
        request_payload = request.get_json(silent=True)
        request_id = request_payload.get("id") if isinstance(request_payload, dict) else None
        return mcp_response_payload(
            request_id,
            error={"code": -32001, "message": message},
        ), status_code

    if is_api_or_mcp_request():
        code = "unauthorized" if status_code == 401 else "forbidden"
        return api_error_response(status_code=status_code, code=code)

    flash(message, "error")
    return redirect(url_for("home"))


def is_scope_authorized(granted_scopes, required_scopes) -> bool:
    """Return whether scope authorized."""
    required = {scope for scope in required_scopes if scope}
    if not required:
        return True
    return "admin" in granted_scopes or required.issubset(granted_scopes)


def is_any_scope_authorized(granted_scopes, allowed_scopes) -> bool:
    """Return whether at least one allowed scope is present."""
    allowed = {scope for scope in allowed_scopes if scope}
    if not allowed:
        return True
    return "admin" in granted_scopes or bool(allowed.intersection(granted_scopes))


def enforce_mcp_tool_acl(tool_name: str):
    """Enforce mcp tool acl."""
    required_scopes = MCP_TOOL_ACL_SCOPES.get(tool_name)
    if not is_auth_enforced():
        return

    token = extract_request_token()
    token_scopes = resolve_token_scopes(token) or set()
    if required_scopes and not is_scope_authorized(token_scopes, required_scopes):
        raise PermissionError(
            f"Permissions insuffisantes pour '{tool_name}'. "
            f"Scope(s) requis: {', '.join(sorted(required_scopes))}."
        )

    allowed_scopes = MCP_TOOL_ACL_ANY_SCOPES.get(tool_name)
    if allowed_scopes and not is_any_scope_authorized(token_scopes, allowed_scopes):
        raise PermissionError(
            f"Permissions insuffisantes pour '{tool_name}'. "
            f"Un des scopes suivants est requis: {', '.join(sorted(allowed_scopes))}."
        )


def require_scopes(*required_scopes):
    """Build a decorator that enforces token-based scope authorization.

    When authentication is disabled for the environment, the wrapped route is
    executed directly without scope checks.
    """
    required = {scope.strip().lower() for scope in required_scopes if scope}

    def decorator(func):
        """Decorate a function with scope enforcement."""
        @wraps(func)
        def wrapper(*args, **kwargs):
            """Enforce required scopes before executing the wrapped function."""
            if is_admin_authenticated() and not request.path.startswith(("/api/", "/mcp")):
                return func(*args, **kwargs)

            if not is_auth_enforced():
                return func(*args, **kwargs)

            token = extract_request_token()
            granted_scopes = resolve_token_scopes(token) if token else None
            if not granted_scopes:
                return auth_failure_response(
                    status_code=401,
                    message=DEFAULT_ERROR_MESSAGES["unauthorized"],
                )

            if not is_scope_authorized(granted_scopes, required):
                return auth_failure_response(
                    status_code=403,
                    message=DEFAULT_ERROR_MESSAGES["forbidden"],
                )

            return func(*args, **kwargs)

        return wrapper

    return decorator


def require_any_scope(*allowed_scopes):
    """Build a decorator that enforces at least one allowed token scope."""
    allowed = {scope.strip().lower() for scope in allowed_scopes if scope}

    def decorator(func):
        """Decorate a function with any-of scope enforcement."""
        @wraps(func)
        def wrapper(*args, **kwargs):
            """Enforce at least one allowed scope before executing the wrapped function."""
            if not is_auth_enforced():
                return func(*args, **kwargs)

            token = extract_request_token()
            granted_scopes = resolve_token_scopes(token) if token else None
            if not granted_scopes:
                return auth_failure_response(
                    status_code=401,
                    message=DEFAULT_ERROR_MESSAGES["unauthorized"],
                )

            if not is_any_scope_authorized(granted_scopes, allowed):
                return auth_failure_response(
                    status_code=403,
                    message=DEFAULT_ERROR_MESSAGES["forbidden"],
                )

            return func(*args, **kwargs)

        return wrapper

    return decorator



def to_slug(value: str) -> str:
    """Convert value to slug."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_value.lower()).strip("_")
    slug = slug[:50].strip("_")

    if not slug:
        raise ValueError("Le nom du projet ne contient pas assez de caracteres valides.")
    if slug[0].isdigit():
        slug = f"p_{slug}"

    return slug


def ensure_projects_table(cur):
    """Ensure projects table without running DDL on every request."""
    cur.execute(
        """
        SELECT COUNT(*)::int
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = ANY(%s);
        """,
        (
            PROJECTS_TABLE,
            ["uuid", "project_name", "project_nameslug", "last_date_edit"],
        ),
    )
    if cur.fetchone()[0] == 4:
        return

    # Legacy installations can still self-repair, but only one transaction may
    # provision this shared schema at a time.
    cur.execute("SELECT pg_advisory_xact_lock(61429, 3);")
    cur.execute(
        """
        SELECT COUNT(*)::int
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = ANY(%s);
        """,
        (
            PROJECTS_TABLE,
            ["uuid", "project_name", "project_nameslug", "last_date_edit"],
        ),
    )
    if cur.fetchone()[0] == 4:
        return

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.project (
            uuid text PRIMARY KEY,
            project_name text NOT NULL,
            project_nameslug text NOT NULL UNIQUE,
            last_date_edit timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    ensure_last_date_edit_timestamptz(cur, PROJECTS_TABLE)


def create_project_tables(cur, slug: str):
    """Create project tables."""
    shard_table = f"{slug}_shard"
    chunk_table = f"{slug}_chunk"
    train_table = f"{slug}_train"
    chat_table = f"{slug}_chat"
    chat_evaluation_table = f"{slug}_chat_evaluation"

    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                uuid text NOT NULL PRIMARY KEY,
                project_id text,
                source_document text,
                url_document text,
                title_document text,
                content_document text,
                autor_document text,
                last_date_edit timestamptz NOT NULL DEFAULT now()
            );
            """
        ).format(sql.Identifier("public", shard_table))
    )
    ensure_last_date_edit_timestamptz(cur, shard_table)

    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                uuid text NOT NULL PRIMARY KEY,
                shard_id text,
                source_document text,
                url_document text,
                title_document text,
                content_document text,
                autor_document text,
                last_date_edit timestamptz NOT NULL DEFAULT now()
            );
            """
        ).format(sql.Identifier("public", chunk_table))
    )
    ensure_last_date_edit_timestamptz(cur, chunk_table)

    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                uuid text NOT NULL PRIMARY KEY,
                project_id text,
                system_content text,
                user_content text,
                assistant_content text,
                metatags text,
                upvote integer,
                downvote integer,
                last_date_edit timestamptz NOT NULL DEFAULT now()
            );
            """
        ).format(sql.Identifier("public", train_table))
    )
    ensure_last_date_edit_timestamptz(cur, train_table)

    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                session_id varchar(255) NOT NULL,
                message jsonb NOT NULL,
                upvote integer DEFAULT 0,
                downvote integer DEFAULT 0
            );
            """
        ).format(sql.Identifier("public", chat_table))
    )

    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                session_id varchar(255) NOT NULL UNIQUE,
                rating_relevance smallint NOT NULL CHECK (rating_relevance BETWEEN 1 AND 5),
                rating_accuracy smallint NOT NULL CHECK (rating_accuracy BETWEEN 1 AND 5),
                rating_clarity smallint NOT NULL CHECK (rating_clarity BETWEEN 1 AND 5),
                rating_completeness smallint NOT NULL CHECK (rating_completeness BETWEEN 1 AND 5),
                rating_helpfulness smallint NOT NULL CHECK (rating_helpfulness BETWEEN 1 AND 5),
                rating_global numeric(3,2) GENERATED ALWAYS AS (
                    round((
                        rating_relevance +
                        rating_accuracy +
                        rating_clarity +
                        rating_completeness +
                        rating_helpfulness
                    )::numeric / 5, 2)
                ) STORED,
                comment text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            );
            """
        ).format(sql.Identifier("public", chat_evaluation_table))
    )

    safe_slug = re.sub(r"[^a-zA-Z0-9_]", "_", slug)[:40]
    slug_hash = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]
    safe_index_name = f"{safe_slug}_chat_eval_sid_{slug_hash}"
    cur.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (session_id);").format(
            sql.Identifier(safe_index_name),
            sql.Identifier("public", chat_evaluation_table),
        )
    )

    ensure_train_table_schema(cur, train_table)
    ensure_chat_table_schema(cur, chat_table)
    ensure_project_vector_schema(cur, slug)
    ensure_project_fk_constraints(cur, slug)


def create_project(name: str):
    """Create a project and provision all project-scoped tables.

    The function validates the name, derives a unique slug, creates the
    `{slug}_*` tables, then inserts the project record in `public.project`.
    """
    project_name = name.strip()
    if not project_name:
        raise ValueError("Le nom du projet est obligatoire.")

    slug = to_slug(project_name)
    project_uuid = str(uuid4())

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_projects_table(cur)
            cur.execute(
                sql.SQL("SELECT 1 FROM public.{} WHERE project_nameslug = %s;").format(
                    sql.Identifier(PROJECTS_TABLE)
                ),
                (slug,),
            )
            if cur.fetchone():
                raise ValueError(f"Le slug '{slug}' existe deja.")

            create_project_tables(cur, slug)
            cur.execute(
                sql.SQL(
                    "INSERT INTO public.{} (uuid, project_name, project_nameslug) VALUES (%s, %s, %s);"
                ).format(sql.Identifier(PROJECTS_TABLE)),
                (project_uuid, project_name, slug),
            )

    return project_uuid, slug


def list_projects():
    """Return projects."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_projects_table(cur)
            cur.execute(
                sql.SQL(
                    """
                    SELECT uuid, project_name, project_nameslug, last_date_edit
                    FROM public.{}
                    ORDER BY uuid DESC;
                    """
                ).format(sql.Identifier(PROJECTS_TABLE))
            )
            rows = cur.fetchall()
            for row in rows:
                ensure_project_fk_constraints(cur, row[2])

    projects = []
    for row in rows:
        projects.append(
            {
                "uuid": row[0],
                "name": row[1],
                "slug": row[2],
                "last_date_edit": row[3],
                "shard_table": f"{row[2]}_shard",
                "chunk_table": f"{row[2]}_chunk",
                "train_table": f"{row[2]}_train",
                "chat_table": f"{row[2]}_chat",
                "chat_evaluation_table": f"{row[2]}_chat_evaluation",
            }
        )
    return projects


def table_exists(cur, table_name: str) -> bool:
    """Run table exists."""
    cur.execute("SELECT to_regclass(%s);", (f"public.{table_name}",))
    return cur.fetchone()[0] is not None


def get_project_table_names(project_slug: str):
    """Return project table names."""
    return {
        "shard_table": f"{project_slug}_shard",
        "chunk_table": f"{project_slug}_chunk",
        "train_table": f"{project_slug}_train",
        "chat_table": f"{project_slug}_chat",
        "chat_evaluation_table": f"{project_slug}_chat_evaluation",
    }


def ensure_tables_exist(cur, table_names):
    """Ensure tables exist."""
    for table_name in table_names:
        if not table_exists(cur, table_name):
            raise ValueError(f"La table '{table_name}' est introuvable.")


def ensure_project_chat_tables(cur, project_slug: str):
    """Ensure chat tables exist for legacy projects."""
    table_names = get_project_table_names(project_slug)
    chat_table = table_names["chat_table"]
    chat_evaluation_table = table_names["chat_evaluation_table"]

    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                session_id varchar(255) NOT NULL,
                message jsonb NOT NULL,
                upvote integer DEFAULT 0,
                downvote integer DEFAULT 0
            );
            """
        ).format(sql.Identifier("public", chat_table))
    )
    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                session_id varchar(255) NOT NULL UNIQUE,
                rating_relevance smallint NOT NULL CHECK (rating_relevance BETWEEN 1 AND 5),
                rating_accuracy smallint NOT NULL CHECK (rating_accuracy BETWEEN 1 AND 5),
                rating_clarity smallint NOT NULL CHECK (rating_clarity BETWEEN 1 AND 5),
                rating_completeness smallint NOT NULL CHECK (rating_completeness BETWEEN 1 AND 5),
                rating_helpfulness smallint NOT NULL CHECK (rating_helpfulness BETWEEN 1 AND 5),
                rating_global numeric(3,2) GENERATED ALWAYS AS (
                    round((
                        rating_relevance +
                        rating_accuracy +
                        rating_clarity +
                        rating_completeness +
                        rating_helpfulness
                    )::numeric / 5, 2)
                ) STORED,
                comment text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            );
            """
        ).format(sql.Identifier("public", chat_evaluation_table))
    )

    safe_slug = re.sub(r"[^a-zA-Z0-9_]", "_", project_slug)[:40]
    slug_hash = hashlib.sha1(project_slug.encode("utf-8")).hexdigest()[:8]
    safe_index_name = f"{safe_slug}_chat_eval_sid_{slug_hash}"
    cur.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (session_id);").format(
            sql.Identifier(safe_index_name),
            sql.Identifier("public", chat_evaluation_table),
        )
    )
    ensure_chat_table_schema(cur, chat_table)


def ensure_project_tables_exist(cur, project_slug: str, include_chat: bool = False):
    """Ensure project tables exist."""
    table_names = get_project_table_names(project_slug)
    required_tables = [
        table_names["shard_table"],
        table_names["chunk_table"],
        table_names["train_table"],
    ]
    ensure_tables_exist(cur, required_tables)
    if include_chat:
        ensure_project_chat_tables(cur, project_slug)
        ensure_tables_exist(cur, [table_names["chat_table"], table_names["chat_evaluation_table"]])
    ensure_project_vector_schema(cur, project_slug)
    return table_names


def ensure_document_registry_table(cur):
    """Ensure document registry table without DDL on the request hot path."""
    index_name = f"idx_{DOCUMENT_REGISTRY_TABLE}_project_slug"
    cur.execute(
        "SELECT to_regclass(%s), to_regclass(%s);",
        (
            f"public.{DOCUMENT_REGISTRY_TABLE}",
            f"public.{index_name}",
        ),
    )
    table_regclass, index_regclass = cur.fetchone()
    if table_regclass is not None and index_regclass is not None:
        return

    cur.execute("SELECT pg_advisory_xact_lock(61429, 2);")
    cur.execute(
        "SELECT to_regclass(%s), to_regclass(%s);",
        (
            f"public.{DOCUMENT_REGISTRY_TABLE}",
            f"public.{index_name}",
        ),
    )
    table_regclass, index_regclass = cur.fetchone()
    if table_regclass is not None and index_regclass is not None:
        return

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{DOCUMENT_REGISTRY_TABLE} (
            document_id text PRIMARY KEY,
            project_slug text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {index_name}
        ON public.{DOCUMENT_REGISTRY_TABLE} (project_slug);
        """
    )


def upsert_document_registry_record(cur, document_id: str, project_slug: str):
    """Upsert document registry record."""
    doc_id = (document_id or "").strip()
    slug = (project_slug or "").strip()
    if not doc_id or not slug:
        return
    ensure_document_registry_table(cur)
    cur.execute(
        f"""
        INSERT INTO public.{DOCUMENT_REGISTRY_TABLE} (
            document_id,
            project_slug,
            created_at,
            updated_at
        ) VALUES (%s, %s, clock_timestamp(), clock_timestamp())
        ON CONFLICT (document_id)
        DO UPDATE SET
            project_slug = EXCLUDED.project_slug,
            updated_at = now();
        """,
        (doc_id, slug),
    )


def delete_document_registry_record(cur, document_id: str, project_slug: str = ""):
    """Delete document registry record."""
    doc_id = (document_id or "").strip()
    slug = (project_slug or "").strip()
    if not doc_id:
        return
    ensure_document_registry_table(cur)
    if slug:
        cur.execute(
            f"""
            DELETE FROM public.{DOCUMENT_REGISTRY_TABLE}
            WHERE document_id = %s
              AND project_slug = %s;
            """,
            (doc_id, slug),
        )
        return

    cur.execute(
        f"""
        DELETE FROM public.{DOCUMENT_REGISTRY_TABLE}
        WHERE document_id = %s;
        """,
        (doc_id,),
    )


def get_document_registry_project(cur, document_id: str):
    """Return document registry project."""
    doc_id = (document_id or "").strip()
    if not doc_id:
        return ""
    ensure_document_registry_table(cur)
    cur.execute(
        f"""
        SELECT project_slug
        FROM public.{DOCUMENT_REGISTRY_TABLE}
        WHERE document_id = %s
        LIMIT 1;
        """,
        (doc_id,),
    )
    row = cur.fetchone()
    return (row[0] or "").strip() if row else ""


def constraint_exists(cur, table_name: str, constraint_name: str) -> bool:
    """Run constraint exists."""
    cur.execute(
        """
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_schema = 'public'
          AND table_name = %s
          AND constraint_name = %s;
        """,
        (table_name, constraint_name),
    )
    return cur.fetchone() is not None


def build_fk_constraint_name(project_slug: str, suffix: str) -> str:
    """Build fk constraint name."""
    safe_suffix = re.sub(r"[^a-zA-Z0-9_]", "_", suffix)[:40]
    digest = hashlib.sha1(f"{project_slug}:{suffix}".encode("utf-8")).hexdigest()[:12]
    return f"fk_{safe_suffix}_{digest}"


def ensure_foreign_key_constraint(
    cur,
    table_name: str,
    constraint_name: str,
    column_name: str,
    referenced_table: str,
    referenced_column: str = "uuid",
    on_delete: str = "CASCADE",
):
    """Ensure foreign key constraint."""
    if on_delete not in {"CASCADE", "RESTRICT", "SET NULL", "NO ACTION"}:
        raise ValueError(f"Option ON DELETE non supportee: {on_delete}")
    if not table_exists(cur, table_name) or not table_exists(cur, referenced_table):
        return
    if constraint_exists(cur, table_name, constraint_name):
        return

    cur.execute(
        sql.SQL(
            """
            ALTER TABLE {}
            ADD CONSTRAINT {}
            FOREIGN KEY ({})
            REFERENCES {} ({})
            ON DELETE {}
            NOT VALID;
            """
        ).format(
            sql.Identifier("public", table_name),
            sql.Identifier(constraint_name),
            sql.Identifier(column_name),
            sql.Identifier("public", referenced_table),
            sql.Identifier(referenced_column),
            sql.SQL(on_delete),
        )
    )


def ensure_project_fk_constraints(cur, project_slug: str):
    """Ensure project fk constraints."""
    shard_table = f"{project_slug}_shard"
    chunk_table = f"{project_slug}_chunk"
    train_table = f"{project_slug}_train"

    ensure_foreign_key_constraint(
        cur,
        table_name=shard_table,
        constraint_name=build_fk_constraint_name(project_slug, "shard_project"),
        column_name="project_id",
        referenced_table=PROJECTS_TABLE,
        referenced_column="uuid",
        on_delete="CASCADE",
    )
    ensure_foreign_key_constraint(
        cur,
        table_name=train_table,
        constraint_name=build_fk_constraint_name(project_slug, "train_project"),
        column_name="project_id",
        referenced_table=PROJECTS_TABLE,
        referenced_column="uuid",
        on_delete="CASCADE",
    )
    ensure_foreign_key_constraint(
        cur,
        table_name=chunk_table,
        constraint_name=build_fk_constraint_name(project_slug, "chunk_shard"),
        column_name="shard_id",
        referenced_table=shard_table,
        referenced_column="uuid",
        on_delete="CASCADE",
    )


def ensure_last_date_edit_timestamptz(cur, table_name: str):
    """Ensure `last_date_edit` is a non-null `timestamptz` column.

    Legacy schemas may still expose `last_date_edit` with an older type.
    In that case, the value is migrated through a temporary column.
    """
    cur.execute(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = 'last_date_edit';
        """,
        (table_name,),
    )
    row = cur.fetchone()
    if not row:
        return

    current_data_type = row[0]
    table_identifier = sql.Identifier("public", table_name)
    if current_data_type == "timestamp with time zone":
        # Fast path for already-compliant schemas: only enforce defaults/constraints.
        cur.execute(
            sql.SQL("ALTER TABLE {} ALTER COLUMN last_date_edit SET DEFAULT now();").format(
                table_identifier
            )
        )
        cur.execute(
            sql.SQL("UPDATE {} SET last_date_edit = now() WHERE last_date_edit IS NULL;").format(
                table_identifier
            )
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} ALTER COLUMN last_date_edit SET NOT NULL;").format(
                table_identifier
            )
        )
        return

    # Legacy path: create a replacement timestamptz column, backfill it,
    # then swap names to preserve the historical column contract.
    cur.execute(
        sql.SQL(
            "ALTER TABLE {} ADD COLUMN IF NOT EXISTS last_date_edit_migrated timestamptz;"
        ).format(table_identifier)
    )
    cur.execute(
        sql.SQL(
            "UPDATE {} SET last_date_edit_migrated = now() WHERE last_date_edit_migrated IS NULL;"
        ).format(table_identifier)
    )
    cur.execute(
        sql.SQL(
            "ALTER TABLE {} ALTER COLUMN last_date_edit_migrated SET DEFAULT now();"
        ).format(table_identifier)
    )
    cur.execute(
        sql.SQL(
            "ALTER TABLE {} ALTER COLUMN last_date_edit_migrated SET NOT NULL;"
        ).format(table_identifier)
    )
    cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN last_date_edit;").format(table_identifier))
    cur.execute(
        sql.SQL(
            "ALTER TABLE {} RENAME COLUMN last_date_edit_migrated TO last_date_edit;"
        ).format(table_identifier)
    )


def ensure_train_table_schema(cur, train_table: str):
    """Ensure train table schema."""
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS metatags text;").format(
            sql.Identifier("public", train_table)
        )
    )


def ensure_chat_table_schema(cur, chat_table: str):
    """Ensure chat table schema."""
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS upvote integer DEFAULT 0;").format(
            sql.Identifier("public", chat_table)
        )
    )
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS downvote integer DEFAULT 0;").format(
            sql.Identifier("public", chat_table)
        )
    )


def ensure_pgvector_extension(cur):
    """Ensure the PostgreSQL pgvector extension is available."""
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except Exception as exc:
        raise RuntimeError(
            "Extension PostgreSQL 'vector' indisponible. "
            "Utilisez une image PostgreSQL avec pgvector, par exemple pgvector/pgvector:pg16."
        ) from exc


def ensure_table_vector_columns(cur, table_name: str):
    """Add pgvector embedding columns to a project-scoped data table."""
    if not table_exists(cur, table_name):
        return
    table_identifier = sql.Identifier("public", table_name)
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS embedding vector;").format(
            table_identifier
        )
    )
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS embedding_model text;").format(
            table_identifier
        )
    )
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS embedding_config_id text;").format(
            table_identifier
        )
    )
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS embedding_dimensions integer;").format(
            table_identifier
        )
    )
    cur.execute(
        sql.SQL(
            "ALTER TABLE {} ADD COLUMN IF NOT EXISTS embedding_status text NOT NULL DEFAULT 'pending';"
        ).format(table_identifier)
    )
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS embedding_error text;").format(
            table_identifier
        )
    )
    cur.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;").format(
            table_identifier
        )
    )


def ensure_project_vector_schema(cur, project_slug: str):
    """Ensure project embedding columns, keeping DDL off the request hot path."""
    slug = (project_slug or "").strip()
    if not slug:
        return

    table_names = [f"{slug}_shard", f"{slug}_chunk", f"{slug}_train"]
    vector_columns = [
        "embedding",
        "embedding_model",
        "embedding_config_id",
        "embedding_dimensions",
        "embedding_status",
        "embedding_error",
        "embedding_updated_at",
    ]

    def schema_is_ready():
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');")
        if not cur.fetchone()[0]:
            return False
        cur.execute(
            """
            SELECT table_name, COUNT(*)::int
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
              AND column_name = ANY(%s)
            GROUP BY table_name;
            """,
            (table_names, vector_columns),
        )
        counts = dict(cur.fetchall())
        return all(counts.get(table_name) == len(vector_columns) for table_name in table_names)

    if schema_is_ready():
        return

    # The second key scopes legacy repair to one project, so independent
    # projects can still be provisioned concurrently.
    cur.execute("SELECT pg_advisory_xact_lock(61429, hashtext(%s));", (slug,))
    if schema_is_ready():
        return
    ensure_pgvector_extension(cur)
    for table_name in table_names:
        ensure_table_vector_columns(cur, table_name)


def lock_project_corpus_mutation(cur, project_slug: str):
    """Serialize corpus rewrites for one project within the transaction."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(61430, hashtext(%s));",
        ((project_slug or "").strip(),),
    )


BUSINESS_SCHEMA_COLUMNS = {
    DOCUMENT_REGISTRY_TABLE: {
        "document_id",
        "project_slug",
        "updated_at",
        "created_at",
    },
    DOCUMENT_PROCESSING_TABLE: {
        "document_id",
        "project_slug",
        "normalization_version",
        "raw_content",
        "normalized_content",
        "rendered_text",
        "structured_content",
        "normalization_config",
        "detected_language",
        "content_type",
        "extracted_metadata",
        "approval_status",
        "quality_score",
    },
    CHUNK_METADATA_TABLE: {
        "chunk_id",
        "project_slug",
        "shard_id",
        "document_id",
        "section_title",
        "section_path",
        "previous_document_id",
        "previous_chunk_id",
        "next_chunk_id",
        "summary_short",
        "document_position_ratio",
        "chunk_type",
        "chunking_method",
        "zone_type",
        "strict_zone",
        "metadata",
        "quality_score",
    },
    DATASET_BUILD_TABLE: {
        "build_id",
        "project_slug",
        "status",
        "quality_min",
        "options",
        "stats",
        "items_preview",
    },
    DOCUMENT_REVIEW_ANNOTATION_TABLE: {
        "annotation_id",
        "document_id",
        "project_slug",
        "target_type",
        "status",
        "note",
    },
    DOCUMENT_SECTION_EXCLUSION_TABLE: {
        "exclusion_id",
        "document_id",
        "project_slug",
        "section_path",
    },
}
BUSINESS_SCHEMA_INDEXES = {
    f"idx_{DOCUMENT_REGISTRY_TABLE}_project_slug",
    f"idx_{DOCUMENT_PROCESSING_TABLE}_project_slug",
    f"idx_{DOCUMENT_PROCESSING_TABLE}_approval_status",
    f"idx_{DOCUMENT_PROCESSING_TABLE}_quality_score",
    f"idx_{CHUNK_METADATA_TABLE}_project_shard",
    f"idx_{CHUNK_METADATA_TABLE}_shard_id",
    f"idx_{CHUNK_METADATA_TABLE}_quality_score",
    f"idx_{DATASET_BUILD_TABLE}_project",
    f"idx_{DOCUMENT_REVIEW_ANNOTATION_TABLE}_document",
    f"idx_{DOCUMENT_SECTION_EXCLUSION_TABLE}_document",
}


def business_schema_is_ready(cur):
    """Return whether the migrated shared schema can serve hot-path requests."""
    table_names = list(BUSINESS_SCHEMA_COLUMNS)
    cur.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY(%s);
        """,
        (table_names,),
    )
    observed_columns = {table_name: set() for table_name in table_names}
    for table_name, column_name in cur.fetchall():
        observed_columns.setdefault(table_name, set()).add(column_name)
    if any(
        not required_columns.issubset(observed_columns.get(table_name, set()))
        for table_name, required_columns in BUSINESS_SCHEMA_COLUMNS.items()
    ):
        return False

    cur.execute(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = ANY(%s);
        """,
        (list(BUSINESS_SCHEMA_INDEXES),),
    )
    return BUSINESS_SCHEMA_INDEXES.issubset({row[0] for row in cur.fetchall()})


def ensure_business_tables(cur):
    """Ensure shared business tables exist with required indexes/constraints.

    This covers cross-project tables such as `document_registry`,
    `document_processing`, `chunk_metadata`, and `dataset_build`. Migrated
    databases take a read-only fast path. Legacy repair DDL is serialized with
    a transaction-scoped PostgreSQL advisory lock to avoid concurrent ALTER
    TABLE deadlocks.
    """
    if business_schema_is_ready(cur):
        return

    cur.execute("SELECT pg_advisory_xact_lock(61429, 1);")
    if business_schema_is_ready(cur):
        return

    ensure_document_registry_table(cur)
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{DOCUMENT_PROCESSING_TABLE} (
            document_id text PRIMARY KEY,
            project_slug text NOT NULL,
            normalization_version text NOT NULL DEFAULT '{DEFAULT_NORMALIZATION_VERSION}',
            raw_content text,
            normalized_content text,
            rendered_text text,
            structured_content jsonb,
            normalization_config jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            detected_language text NOT NULL DEFAULT 'und',
            content_type text NOT NULL DEFAULT 'unknown',
            extracted_metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            approval_status text NOT NULL DEFAULT 'pending' CHECK (approval_status IN ('pending', 'approved', 'rejected')),
            approval_comment text,
            approved_by text,
            approved_at timestamptz,
            quality_score numeric(5,4),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        f"ALTER TABLE public.{DOCUMENT_PROCESSING_TABLE} ADD COLUMN IF NOT EXISTS raw_content text;"
    )
    cur.execute(
        f"ALTER TABLE public.{DOCUMENT_PROCESSING_TABLE} ADD COLUMN IF NOT EXISTS normalization_config jsonb NOT NULL DEFAULT '{{}}'::jsonb;"
    )
    cur.execute(
        f"ALTER TABLE public.{DOCUMENT_PROCESSING_TABLE} ADD COLUMN IF NOT EXISTS detected_language text NOT NULL DEFAULT 'und';"
    )
    cur.execute(
        f"ALTER TABLE public.{DOCUMENT_PROCESSING_TABLE} ADD COLUMN IF NOT EXISTS content_type text NOT NULL DEFAULT 'unknown';"
    )
    cur.execute(
        f"ALTER TABLE public.{DOCUMENT_PROCESSING_TABLE} ADD COLUMN IF NOT EXISTS extracted_metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb;"
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{CHUNK_METADATA_TABLE} (
            chunk_id text PRIMARY KEY,
            project_slug text NOT NULL,
            shard_id text NOT NULL,
            document_id text NOT NULL,
            section_title text,
            section_path text,
            previous_document_id text,
            previous_chunk_id text,
            next_chunk_id text,
            summary_short text NOT NULL DEFAULT '',
            document_position_ratio numeric(6,5) NOT NULL DEFAULT 0,
            zone_type text NOT NULL DEFAULT 'text',
            strict_zone boolean NOT NULL DEFAULT false,
            quality_score numeric(5,4),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS chunk_type text NOT NULL DEFAULT 'markdown';")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS chunking_method text NOT NULL DEFAULT 'deterministic';")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS llm_config_id text;")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS llm_profile_type text;")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS llm_audit_session_id text;")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb;")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS summary_short text NOT NULL DEFAULT '';")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS document_position_ratio numeric(6,5) NOT NULL DEFAULT 0;")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS zone_type text NOT NULL DEFAULT 'text';")
    cur.execute(f"ALTER TABLE public.{CHUNK_METADATA_TABLE} ADD COLUMN IF NOT EXISTS strict_zone boolean NOT NULL DEFAULT false;")
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{DATASET_BUILD_TABLE} (
            build_id text PRIMARY KEY,
            project_slug text NOT NULL,
            status text NOT NULL,
            quality_min numeric(5,4) NOT NULL DEFAULT 0,
            options jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            stats jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            items_preview jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz
        );
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{DOCUMENT_REVIEW_ANNOTATION_TABLE} (
            annotation_id text PRIMARY KEY,
            document_id text NOT NULL,
            project_slug text NOT NULL,
            target_type text NOT NULL DEFAULT 'document'
                CHECK (target_type IN ('document', 'section', 'chunk')),
            target_id text,
            section_path text,
            severity text NOT NULL DEFAULT 'medium'
                CHECK (severity IN ('low', 'medium', 'high')),
            status text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'resolved')),
            note text NOT NULL,
            created_by text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{DOCUMENT_SECTION_EXCLUSION_TABLE} (
            exclusion_id text PRIMARY KEY,
            document_id text NOT NULL,
            project_slug text NOT NULL,
            section_path text NOT NULL,
            section_title text,
            reason text,
            excluded_by text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (document_id, project_slug, section_path)
        );
        """
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{CHUNK_METADATA_TABLE}_project_shard ON public.{CHUNK_METADATA_TABLE} (project_slug, shard_id);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{CHUNK_METADATA_TABLE}_shard_id ON public.{CHUNK_METADATA_TABLE} (shard_id);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{CHUNK_METADATA_TABLE}_quality_score ON public.{CHUNK_METADATA_TABLE} (quality_score DESC);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENT_PROCESSING_TABLE}_project_slug ON public.{DOCUMENT_PROCESSING_TABLE} (project_slug);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENT_PROCESSING_TABLE}_approval_status ON public.{DOCUMENT_PROCESSING_TABLE} (approval_status);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENT_PROCESSING_TABLE}_quality_score ON public.{DOCUMENT_PROCESSING_TABLE} (quality_score DESC);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DATASET_BUILD_TABLE}_project ON public.{DATASET_BUILD_TABLE} (project_slug, created_at DESC);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENT_REVIEW_ANNOTATION_TABLE}_document ON public.{DOCUMENT_REVIEW_ANNOTATION_TABLE} (project_slug, document_id, created_at DESC);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENT_SECTION_EXCLUSION_TABLE}_document ON public.{DOCUMENT_SECTION_EXCLUSION_TABLE} (project_slug, document_id);"
    )


def parse_float_field(value, field_name: str, default: float = 0.0) -> float:
    """Parse float field."""
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"Le champ '{field_name}' doit etre un nombre.") from exc


def parse_bool_field(value, default: bool = False) -> bool:
    """Parse bool field."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "oui", "on"}:
        return True
    if normalized in {"0", "false", "no", "non", "off"}:
        return False
    return default


def now_utc():
    """Run now utc."""
    return datetime.now(timezone.utc)


def normalize_document_content(content: str) -> str:
    """Normalize document content with the default advanced pipeline."""
    return run_normalization_pipeline(content)["normalized_content"]


def normalize_normalization_options(raw_options=None):
    """Validate normalization stages and return a stable pipeline configuration."""
    options = DEFAULT_NORMALIZATION_OPTIONS.copy()
    options["enabled_stages"] = list(NORMALIZATION_STAGES)
    if not isinstance(raw_options, dict):
        return options

    requested_stages = raw_options.get("enabled_stages")
    if isinstance(requested_stages, str):
        requested_stages = requested_stages.split(",")
    if isinstance(requested_stages, (list, tuple, set)):
        normalized_stages = []
        for stage in requested_stages:
            stage_name = str(stage).strip().lower()
            if stage_name and stage_name not in NORMALIZATION_STAGES:
                raise ValueError(f"Etape de normalisation inconnue: '{stage_name}'.")
            if stage_name and stage_name not in normalized_stages:
                normalized_stages.append(stage_name)
        options["enabled_stages"] = normalized_stages

    try:
        heading_max_level = int(raw_options.get("heading_max_level", 6))
    except (TypeError, ValueError):
        raise ValueError("'heading_max_level' doit etre un entier.") from None
    options["heading_max_level"] = min(6, max(1, heading_max_level))
    options["preserve_code_blocks"] = parse_bool_field(
        raw_options.get("preserve_code_blocks"),
        default=True,
    )
    return options


def extract_code_blocks(text):
    """Extract fenced code blocks and replace them with stable placeholders."""
    code_blocks = []

    def replace(match):
        language = (match.group(1) or "").strip().lower()
        content = (match.group(2) or "").strip("\n")
        index = len(code_blocks)
        code_blocks.append(
            {
                "index": index,
                "language": language,
                "content": content,
                "line_count": len(content.splitlines()) if content else 0,
            }
        )
        return f"\n@@FLOPPY_CODE_BLOCK_{index}@@\n"

    protected = re.sub(r"```([A-Za-z0-9_+.-]*)[ \t]*\n(.*?)```", replace, text, flags=re.DOTALL)
    return protected, code_blocks


def restore_code_blocks(text, code_blocks):
    """Restore protected fenced code blocks after textual cleanup."""
    restored = text
    for block in code_blocks:
        language = block["language"]
        fence_header = f"```{language}" if language else "```"
        fenced = f"{fence_header}\n{block['content']}\n```"
        restored = restored.replace(f"@@FLOPPY_CODE_BLOCK_{block['index']}@@", fenced)
    return restored


def split_markdown_table_row(line):
    """Split a Markdown table row while preserving escaped pipes."""
    value = line.strip().strip("|")
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", value)]


def extract_markdown_tables(text):
    """Extract valid GitHub-style Markdown tables."""
    lines = text.splitlines()
    tables = []
    index = 0
    while index + 1 < len(lines):
        header = lines[index]
        separator = lines[index + 1]
        if "|" not in header or not re.match(
            r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$",
            separator,
        ):
            index += 1
            continue
        headers = split_markdown_table_row(header)
        alignments = []
        for cell in split_markdown_table_row(separator):
            stripped = cell.strip()
            alignments.append(
                "center" if stripped.startswith(":") and stripped.endswith(":")
                else "right" if stripped.endswith(":")
                else "left"
            )
        rows = []
        cursor = index + 2
        while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
            row = split_markdown_table_row(lines[cursor])
            row += [""] * max(0, len(headers) - len(row))
            rows.append(row[:len(headers)])
            cursor += 1
        tables.append(
            {
                "index": len(tables),
                "headers": headers,
                "alignments": alignments[:len(headers)],
                "rows": rows,
                "row_count": len(rows),
                "column_count": len(headers),
            }
        )
        index = cursor
    return tables


def extract_markdown_lists(text):
    """Extract ordered, unordered and task-list items with nesting metadata."""
    items = []
    pattern = re.compile(r"^(\s*)([-+*]|\d+[.)])\s+(\[[ xX]\]\s+)?(.+)$")
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = pattern.match(line)
        if not match:
            continue
        marker = match.group(2)
        task_marker = (match.group(3) or "").strip().lower()
        items.append(
            {
                "line": line_number,
                "level": (len(match.group(1).replace("\t", "    ")) // 2) + 1,
                "kind": "ordered" if marker[0].isdigit() else "unordered",
                "text": match.group(4).strip(),
                "checked": None if not task_marker else task_marker == "[x]",
            }
        )
    return items


def extract_markdown_references(text):
    """Extract links, images and footnotes without fetching external resources."""
    images = [
        {"alt": alt.strip(), "url": url.strip()}
        for alt, url in re.findall(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)", text)
    ]
    text_without_images = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    links = [
        {"label": label.strip(), "url": url.strip()}
        for label, url in re.findall(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)", text_without_images)
    ]
    footnote_definitions = {
        key: value.strip()
        for key, value in re.findall(r"(?m)^\[\^([^\]]+)\]:\s*(.+)$", text)
    }
    footnote_refs = sorted(set(re.findall(r"\[\^([^\]]+)\]", text)))
    return {
        "links": links,
        "images": images,
        "notes": [
            {"id": note_id, "content": footnote_definitions.get(note_id, "")}
            for note_id in footnote_refs
        ],
    }


def detect_document_language(text):
    """Detect a likely language using deterministic stop-word evidence."""
    words = re.findall(r"[A-Za-zÀ-ÿ']+", (text or "").lower())
    if not words:
        return {"language": "und", "confidence": 0.0, "scores": {}}
    stop_words = {
        "fr": {"le", "la", "les", "des", "une", "un", "et", "est", "dans", "pour", "avec", "que", "qui", "sur", "du"},
        "en": {"the", "a", "an", "and", "is", "in", "for", "with", "that", "this", "of", "to", "on", "are"},
        "es": {"el", "la", "los", "las", "una", "un", "y", "es", "en", "para", "con", "que", "de", "por"},
        "de": {"der", "die", "das", "ein", "eine", "und", "ist", "in", "für", "mit", "dass", "von", "zu", "auf"},
    }
    scores = {language: sum(1 for word in words if word in markers) for language, markers in stop_words.items()}
    language, score = max(scores.items(), key=lambda item: item[1])
    evidence = sum(scores.values())
    if score < 2:
        language = "und"
    confidence = round(score / max(1, evidence), 4) if language != "und" else 0.0
    return {"language": language, "confidence": confidence, "scores": scores}


def detect_document_content_type(raw_text, code_blocks, tables, list_items):
    """Classify the dominant document structure."""
    signals = {
        "code": len(code_blocks),
        "table": len(tables),
        "list": len(list_items),
        "markdown": len(re.findall(r"(?m)^#{1,6}\s+", raw_text or "")),
        "html": len(re.findall(r"<[A-Za-z][^>]*>", raw_text or "")),
    }
    active = [name for name, count in signals.items() if count]
    if len(active) > 1:
        content_type = "mixed"
    elif active:
        content_type = active[0]
    else:
        content_type = "plain_text"
    return {"content_type": content_type, "signals": signals}


def build_rendered_text(normalized_content):
    """Build a readable LLM-facing representation without Markdown decoration."""
    rendered = re.sub(r"(?m)^#{1,6}\s+", "", normalized_content or "")
    rendered = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", rendered)
    rendered = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", rendered)
    rendered = re.sub(r"(?m)^\[\^[^\]]+\]:\s*", "Note: ", rendered)
    return rendered.strip()


def run_normalization_pipeline(raw_content, raw_options=None):
    """Execute the configurable v2 normalization pipeline."""
    options = normalize_normalization_options(raw_options)
    enabled = set(options["enabled_stages"])
    text = raw_content or ""
    if "line_endings" in enabled:
        text = text.replace("\r\n", "\n").replace("\r", "\n")

    protected_text, code_blocks = extract_code_blocks(text)
    if "html_cleanup" in enabled:
        protected_text = re.sub(r"<\s*br\s*/?\s*>", "\n", protected_text, flags=re.IGNORECASE)
        protected_text = re.sub(r"</\s*(p|div|section|article|li)\s*>", "\n", protected_text, flags=re.IGNORECASE)
        protected_text = re.sub(r"<[^>]+>", " ", protected_text)
        protected_text = html.unescape(protected_text)
    if "whitespace" in enabled:
        protected_text = re.sub(r"[ \t]+", " ", protected_text)
        protected_text = re.sub(r"[ \t]+\n", "\n", protected_text)
        protected_text = re.sub(r"\n{3,}", "\n\n", protected_text)

    if options["preserve_code_blocks"]:
        normalized_content = restore_code_blocks(protected_text.strip(), code_blocks)
    else:
        normalized_content = protected_text.strip()
        for block in code_blocks:
            normalized_content = normalized_content.replace(
                f"@@FLOPPY_CODE_BLOCK_{block['index']}@@",
                block["content"],
            )
    tables = extract_markdown_tables(normalized_content) if "tables" in enabled else []
    list_items = extract_markdown_lists(normalized_content) if "lists" in enabled else []
    references = (
        extract_markdown_references(normalized_content)
        if "references" in enabled
        else {"links": [], "images": [], "notes": []}
    )
    language = (
        detect_document_language(normalized_content)
        if "language" in enabled
        else {"language": "und", "confidence": 0.0, "scores": {}}
    )
    content_type = (
        detect_document_content_type(raw_content or "", code_blocks, tables, list_items)
        if "content_type" in enabled
        else {"content_type": "unknown", "signals": {}}
    )
    extracted_metadata = {
        "tables": tables,
        "code_blocks": code_blocks if "code_blocks" in enabled else [],
        "lists": list_items,
        **references,
        "language_detection": language,
        "content_type_detection": content_type,
    }
    return {
        "raw_content": raw_content or "",
        "normalized_content": normalized_content,
        "rendered_text": build_rendered_text(normalized_content),
        "normalization_config": options,
        "detected_language": language["language"],
        "content_type": content_type["content_type"],
        "extracted_metadata": extracted_metadata,
    }


def compute_quality_score_breakdown(text: str):
    """Return the deterministic heuristic score and its weighted components."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {
            "score": 0.0,
            "token_count": 0,
            "character_count": len(text or ""),
            "components": [],
        }

    token_list = re.findall(r"\w+", cleaned.lower())
    token_count = len(token_list)
    if token_count == 0:
        return {
            "score": 0.0,
            "token_count": 0,
            "character_count": len(cleaned),
            "components": [],
        }

    min_target = 40
    max_target = 380
    if token_count < min_target:
        length_score = token_count / float(min_target)
    elif token_count > max_target:
        overflow = token_count - max_target
        length_score = max(0.0, 1.0 - (overflow / float(max_target * 2)))
    else:
        length_score = 1.0

    unique_ratio = len(set(token_list)) / float(token_count)
    lexical_score = max(0.0, min(1.0, (unique_ratio - 0.2) / 0.6))

    alnum_chars = len(re.findall(r"[A-Za-z0-9]", cleaned))
    char_count = max(1, len(cleaned))
    noise_score = max(0.0, min(1.0, (alnum_chars / float(char_count)) * 1.2))

    components = [
        {
            "rule_code": "QF_LENGTH_SCORE",
            "component_score": round(length_score, 6),
            "weight": 0.5,
            "score_delta": round(0.5 * length_score, 6),
            "evidence": {"token_count": token_count, "minimum_target": min_target,
                         "maximum_target": max_target},
        },
        {
            "rule_code": "QF_LEXICAL_DIVERSITY",
            "component_score": round(lexical_score, 6),
            "weight": 0.3,
            "score_delta": round(0.3 * lexical_score, 6),
            "evidence": {"token_count": token_count,
                         "unique_token_count": len(set(token_list)),
                         "unique_ratio": round(unique_ratio, 6)},
        },
        {
            "rule_code": "QF_CHARACTER_SIGNAL",
            "component_score": round(noise_score, 6),
            "weight": 0.2,
            "score_delta": round(0.2 * noise_score, 6),
            "evidence": {"character_count": char_count,
                         "alphanumeric_count": alnum_chars,
                         "alphanumeric_ratio": round(alnum_chars / float(char_count), 6)},
        },
    ]
    score = sum(component["score_delta"] for component in components)
    return {
        "score": round(max(0.0, min(1.0, score)), 4),
        "token_count": token_count,
        "character_count": char_count,
        "components": components,
    }


def compute_quality_score(text: str) -> float:
    """Compute the legacy-compatible deterministic document quality score."""
    return compute_quality_score_breakdown(text)["score"]


def sha256_text(value: str) -> str:
    """Hash the exact UTF-8 representation of a text value with SHA-256."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def quality_normalization_hash_version(normalization_version: str) -> str:
    """Build the explicit version attached to normalized-content hashes."""
    source_version = (normalization_version or DEFAULT_NORMALIZATION_VERSION).strip()
    return f"{source_version}:{QUALITY_HASH_ENCODING_VERSION}"


def bound_quality_evidence(evidence):
    """Return a small JSON object suitable for non-sensitive quality evidence."""
    safe_evidence = evidence if isinstance(evidence, dict) else {}
    encoded = json.dumps(
        safe_evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) <= QUALITY_EVIDENCE_MAX_BYTES:
        return safe_evidence
    return {
        "evidence_truncated": True,
        "original_size_bytes": len(encoded),
        "retained_keys": sorted(str(key)[:80] for key in safe_evidence)[:20],
    }


def build_structured_content(
    normalized_content: str,
    heading_max_level: int = 3,
    extracted_metadata=None,
):
    """Build a hierarchical representation enriched with typed elements."""
    sections = split_markdown_sections(normalized_content, heading_max_level)
    if not sections and normalized_content.strip():
        sections = [
            {
                "section_title": "Document",
                "section_path": "Document",
                "content": normalized_content.strip(),
            }
        ]

    normalized_sections = []
    for section in sections:
        text = (section.get("content") or "").strip()
        normalized_sections.append(
            {
                "section_title": section.get("section_title") or "Section",
                "section_path": section.get("section_path") or "Section",
                "token_estimate": len(re.findall(r"\S+", text)),
                "content": text,
            }
        )

    return {
        "section_count": len(normalized_sections),
        "sections": normalized_sections,
        "elements": extracted_metadata or {
            "tables": [],
            "code_blocks": [],
            "lists": [],
            "links": [],
            "images": [],
            "notes": [],
        },
    }


def parse_chunk_options(payload):
    """Parse chunk options."""
    options = DEFAULT_CHUNK_OPTIONS.copy()
    if not payload:
        return options

    int_fields = [
        "chunkMaxTokens",
        "chunkOverlapTokens",
        "hardMaxTokens",
        "headingMaxLevel",
        "charsPerToken",
        "smallParagraphMinTokens",
    ]
    for field in int_fields:
        if field in payload and str(payload[field]).strip():
            options[field] = int(payload[field])

    if "tokenEstimator" in payload and str(payload["tokenEstimator"]).strip():
        options["tokenEstimator"] = str(payload["tokenEstimator"]).strip().lower()
    for field in ("codeAware", "tableAware", "mergeSmallParagraphs"):
        if field in payload:
            options[field] = parse_bool_field(payload[field], default=options[field])
    if "strictZoneTypes" in payload:
        raw_zone_types = payload["strictZoneTypes"]
        if isinstance(raw_zone_types, str):
            raw_zone_types = raw_zone_types.split(",")
        if not isinstance(raw_zone_types, (list, tuple, set)):
            raise ValueError("'strictZoneTypes' doit etre une liste.")
        allowed_zone_types = {"code", "table", "strict"}
        zone_types = []
        for raw_zone_type in raw_zone_types:
            zone_type = str(raw_zone_type).strip().lower()
            if zone_type not in allowed_zone_types:
                raise ValueError(f"Type de zone stricte inconnu: '{zone_type}'.")
            if zone_type not in zone_types:
                zone_types.append(zone_type)
        options["strictZoneTypes"] = zone_types

    options["chunkMaxTokens"] = max(1, options["chunkMaxTokens"])
    options["hardMaxTokens"] = max(options["chunkMaxTokens"], options["hardMaxTokens"])
    options["chunkOverlapTokens"] = max(0, options["chunkOverlapTokens"])
    if options["chunkOverlapTokens"] >= options["chunkMaxTokens"]:
        options["chunkOverlapTokens"] = max(0, options["chunkMaxTokens"] - 1)
    options["headingMaxLevel"] = min(6, max(1, options["headingMaxLevel"]))
    options["charsPerToken"] = max(1, options["charsPerToken"])
    options["smallParagraphMinTokens"] = min(
        options["chunkMaxTokens"],
        max(1, options["smallParagraphMinTokens"]),
    )
    if options["tokenEstimator"] not in {"words", "chars"}:
        options["tokenEstimator"] = "words"

    return options


def parse_int_field(value, field_name: str, default: int = 0) -> int:
    """Parse int field."""
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"Le champ '{field_name}' doit etre un entier.") from exc


def normalize_payload_dict(payload):
    """Normalize payload dict."""
    if isinstance(payload, dict):
        return payload
    return {}


def merge_payloads(*payloads):
    """Run merge payloads."""
    merged = {}
    for payload in payloads:
        normalized = normalize_payload_dict(payload)
        for key, value in normalized.items():
            if key in merged:
                continue
            merged[key] = value
    return merged


def apply_numeric_constraints(value, field_name: str, field_schema):
    """Validate min and max constraints for a numeric field value."""
    minimum = field_schema.get("min")
    maximum = field_schema.get("max")
    if minimum is not None and value < minimum:
        raise ValueError(f"Le champ '{field_name}' doit etre >= {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"Le champ '{field_name}' doit etre <= {maximum}.")
    return value


def coerce_schema_value(value, field_name: str, field_schema):
    """Coerce a raw payload value to the type declared in the schema."""
    field_type = field_schema.get("type", "string")

    if field_type == "string":
        parsed_value = str(value).strip()
        if field_schema.get("lower"):
            parsed_value = parsed_value.lower()
        enum_values = field_schema.get("enum")
        if enum_values is not None and parsed_value not in enum_values:
            raise ValueError(
                f"Le champ '{field_name}' doit etre dans: {', '.join(enum_values)}."
            )
        if not parsed_value and not field_schema.get("allow_empty", True):
            raise ValueError(f"Le champ '{field_name}' est obligatoire.")
        return parsed_value

    if field_type == "integer":
        parsed_value = parse_int_field(value, field_name)
        return apply_numeric_constraints(parsed_value, field_name, field_schema)

    if field_type == "number":
        parsed_value = parse_float_field(value, field_name)
        return apply_numeric_constraints(parsed_value, field_name, field_schema)

    if field_type == "boolean":
        return parse_bool_field(value, default=bool(field_schema.get("default", False)))

    if field_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"Le champ '{field_name}' doit etre une liste.")
        min_items = field_schema.get("min_items")
        if min_items is not None and len(value) < min_items:
            raise ValueError(f"Le champ '{field_name}' doit contenir au moins {min_items} element(s).")
        items_type = field_schema.get("items_type")
        if items_type == "object":
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Le champ '{field_name}' contient un element invalide a l'index {index}."
                    )
        return value

    if field_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"Le champ '{field_name}' doit etre un objet JSON.")
        return value

    raise ValueError(f"Type de schema non supporte pour '{field_name}': {field_type}.")


def validate_operation_payload(operation_name: str, payload):
    """Validate and normalize a payload against an operation schema.

    This is the common validation entry point used by REST and MCP handlers so
    both interfaces share the same coercion/default/error behavior.
    """
    schema = OPERATION_INPUT_SCHEMAS.get(operation_name)
    if schema is None:
        raise ValueError(f"Schema inconnu pour l'operation '{operation_name}'.")

    normalized_payload = normalize_payload_dict(payload)
    required_fields = set(schema.get("required", []))
    field_definitions = schema.get("fields", {})
    parsed = {}

    # Apply defaults and coercion in one pass over declared schema fields.
    for field_name, field_schema in field_definitions.items():
        raw_value = normalized_payload.get(field_name)

        if raw_value is None:
            if "default" in field_schema:
                parsed[field_name] = field_schema["default"]
                continue
            if field_name in required_fields:
                raise ValueError(f"Le champ '{field_name}' est obligatoire.")
            continue

        # HTML forms commonly submit empty strings; treat them explicitly.
        if isinstance(raw_value, str) and not raw_value.strip():
            if "default" in field_schema and field_schema.get("allow_empty", True):
                parsed[field_name] = field_schema["default"]
                continue
            if field_name in required_fields and not field_schema.get("allow_empty", True):
                raise ValueError(f"Le champ '{field_name}' est obligatoire.")

        parsed[field_name] = coerce_schema_value(raw_value, field_name, field_schema)

    for required_field in required_fields:
        if required_field not in parsed:
            raise ValueError(f"Le champ '{required_field}' est obligatoire.")

    return parsed


def normalize_metatags(raw_value) -> str:
    """Normalize metatags."""
    text_value = str(raw_value or "").strip()
    if not text_value:
        return ""

    tags = []
    seen = set()
    for raw_tag in text_value.split(","):
        normalized_tag = raw_tag.strip()
        if not normalized_tag:
            continue
        tag_key = normalized_tag.lower()
        if tag_key in seen:
            continue
        seen.add(tag_key)
        tags.append(normalized_tag)

    return ", ".join(tags)


def parse_rating_field(value, field_name: str) -> int:
    """Parse rating field."""
    parsed = parse_int_field(value, field_name, default=0)
    if parsed < 1 or parsed > 5:
        raise ValueError(f"Le champ '{field_name}' doit etre compris entre 1 et 5.")
    return parsed


def parse_chat_message(raw_message):
    """Parse chat message."""
    if isinstance(raw_message, dict):
        payload = raw_message
    elif isinstance(raw_message, str):
        try:
            payload = json.loads(raw_message)
        except ValueError:
            payload = {"content": raw_message}
    else:
        payload = {"content": str(raw_message)}

    if not isinstance(payload, dict):
        payload = {"content": payload}

    message_type = str(payload.get("type") or payload.get("role") or payload.get("name") or "message")
    message_content = payload.get("content")
    if isinstance(message_content, str):
        message_content = message_content.strip()
    elif message_content is None:
        message_content = ""
    else:
        message_content = json.dumps(message_content, ensure_ascii=False)

    if not message_content:
        message_content = json.dumps(payload, ensure_ascii=False)

    return {
        "type": message_type,
        "content": message_content,
        "raw": payload,
    }


def shorten_text(value: str, max_chars: int = 180) -> str:
    """Run shorten text."""
    text = (value or "").strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[: max_chars - 3]}..."


def to_float_or_none(value):
    """Convert value to float or none."""
    if value is None:
        return None
    converted = float(value)
    if math.isnan(converted):
        return None
    return converted


def find_project_by_slug(cur, project_slug: str):
    """Find project by slug."""
    ensure_projects_table(cur)
    cur.execute(
        sql.SQL(
            """
            SELECT uuid, project_name, project_nameslug
            FROM public.{}
            WHERE project_nameslug = %s;
            """
        ).format(sql.Identifier(PROJECTS_TABLE)),
        (project_slug,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Projet introuvable pour le slug '{project_slug}'.")
    return {"uuid": row[0], "name": row[1], "slug": row[2]}


def get_project_crud_payload(project_slug: str):
    """Build the aggregated CRUD payload used by project management views.

    Returns project metadata along with shards, chunks, and train items for the
    requested project.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            table_names = ensure_project_tables_exist(cur, project_slug, include_chat=False)
            shard_table = table_names["shard_table"]
            chunk_table = table_names["chunk_table"]
            train_table = table_names["train_table"]
            ensure_train_table_schema(cur, train_table)

            cur.execute(
                sql.SQL(
                    """
                    SELECT
                        s.uuid,
                        s.source_document,
                        s.url_document,
                        s.title_document,
                        s.content_document,
                        s.autor_document,
                        COUNT(c.uuid)::int AS chunk_count
                    FROM {} AS s
                    LEFT JOIN {} AS c
                        ON c.shard_id = s.uuid
                    GROUP BY
                        s.uuid,
                        s.source_document,
                        s.url_document,
                        s.title_document,
                        s.content_document,
                        s.autor_document
                    ORDER BY s.uuid;
                    """
                ).format(
                    sql.Identifier("public", shard_table),
                    sql.Identifier("public", chunk_table),
                )
            )
            shard_rows = cur.fetchall()

            shards = []
            for row in shard_rows:
                shards.append(
                    {
                        "uuid": row[0],
                        "source_document": row[1] or "",
                        "url_document": row[2] or "",
                        "title_document": row[3] or "",
                        "content_document": row[4] or "",
                        "autor_document": row[5] or "",
                        "chunk_count": int(row[6]),
                    }
                )

            cur.execute(
                sql.SQL(
                    f"""
                    SELECT
                        c.uuid,
                        c.shard_id,
                        c.source_document,
                        c.url_document,
                        c.title_document,
                        c.content_document,
                        c.autor_document,
                        m.chunk_type,
                        m.chunking_method,
                        m.llm_config_id,
                        m.llm_profile_type
                    FROM {{}} c
                    LEFT JOIN public.{CHUNK_METADATA_TABLE} m
                        ON m.chunk_id = c.uuid
                       AND m.project_slug = %s
                    ORDER BY c.uuid;
                    """
                ).format(sql.Identifier("public", chunk_table)),
                (project_slug,),
            )
            chunk_rows = cur.fetchall()

            chunks = []
            for row in chunk_rows:
                chunks.append(
                    {
                        "uuid": row[0],
                        "shard_id": row[1] or "",
                        "source_document": row[2] or "",
                        "url_document": row[3] or "",
                        "title_document": row[4] or "",
                        "content_document": row[5] or "",
                        "autor_document": row[6] or "",
                        "metadata": {
                            "chunk_type": row[7] or "markdown",
                            "chunking_method": row[8] or "deterministic",
                            "llm_config_id": row[9] or "",
                            "llm_profile_type": row[10] or "",
                        },
                    }
                )

            cur.execute(
                sql.SQL(
                    """
                    SELECT
                        uuid,
                        project_id,
                        system_content,
                        user_content,
                        assistant_content,
                        metatags,
                        upvote,
                        downvote
                    FROM {}
                    ORDER BY uuid;
                    """
                ).format(sql.Identifier("public", train_table))
            )
            train_rows = cur.fetchall()

            train_items = []
            for row in train_rows:
                train_items.append(
                    {
                        "uuid": row[0],
                        "project_id": row[1] or "",
                        "system_content": row[2] or "",
                        "user_content": row[3] or "",
                        "assistant_content": row[4] or "",
                        "metatags": row[5] or "",
                        "upvote": row[6] if row[6] is not None else 0,
                        "downvote": row[7] if row[7] is not None else 0,
                    }
                )

    return {
        "project": project,
        "shard_table": shard_table,
        "chunk_table": chunk_table,
        "train_table": train_table,
        "shards": shards,
        "chunks": chunks,
        "train_items": train_items,
    }


def get_project_chat_payload(
    project_slug: str, selected_session_id: str = "", auto_select_latest_session: bool = False
):
    """Build chat sessions/messages payload for a project.

    A specific session can be selected explicitly, or auto-selected from the
    latest known session when requested.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            table_names = ensure_project_tables_exist(cur, project_slug, include_chat=True)
            chat_table = table_names["chat_table"]
            chat_evaluation_table = table_names["chat_evaluation_table"]

            cur.execute(
                sql.SQL(
                    """
                    WITH ranked AS (
                        SELECT
                            c.session_id,
                            c.id,
                            c.message,
                            COUNT(*) OVER (PARTITION BY c.session_id)::int AS message_count,
                            ROW_NUMBER() OVER (PARTITION BY c.session_id ORDER BY c.id DESC) AS rn
                        FROM {} AS c
                    )
                    SELECT
                        r.session_id,
                        r.message_count,
                        r.id AS last_message_id,
                        r.message AS last_message_payload,
                        e.rating_global,
                        e.updated_at
                    FROM ranked AS r
                    LEFT JOIN {} AS e
                        ON e.session_id = r.session_id
                    WHERE r.rn = 1
                    ORDER BY r.id DESC;
                    """
                ).format(
                    sql.Identifier("public", chat_table),
                    sql.Identifier("public", chat_evaluation_table),
                )
            )
            session_rows = cur.fetchall()

            sessions = []
            for row in session_rows:
                parsed_last_message = parse_chat_message(row[3])
                sessions.append(
                    {
                        "session_id": row[0],
                        "message_count": int(row[1]),
                        "last_message_id": int(row[2]),
                        "last_message_type": parsed_last_message["type"],
                        "last_message_preview": shorten_text(parsed_last_message["content"], 200),
                        "rating_global": float(row[4]) if row[4] is not None else None,
                        "evaluation_updated_at": row[5],
                    }
                )

            resolved_session_id = (selected_session_id or "").strip()
            if not resolved_session_id and auto_select_latest_session and sessions:
                resolved_session_id = sessions[0]["session_id"]

            selected_session_messages = []
            selected_evaluation = None
            if resolved_session_id:
                cur.execute(
                    sql.SQL(
                        """
                        SELECT
                            id,
                            message
                        FROM {}
                        WHERE session_id = %s
                        ORDER BY id;
                        """
                    ).format(sql.Identifier("public", chat_table)),
                    (resolved_session_id,),
                )
                message_rows = cur.fetchall()
                if not message_rows:
                    raise ValueError(f"Session introuvable: '{resolved_session_id}'.")

                for message_row in message_rows:
                    parsed_message = parse_chat_message(message_row[1])
                    selected_session_messages.append(
                        {
                            "id": int(message_row[0]),
                            "type": parsed_message["type"],
                            "content": parsed_message["content"],
                        }
                    )

                cur.execute(
                    sql.SQL(
                        """
                        SELECT
                            session_id,
                            rating_relevance,
                            rating_accuracy,
                            rating_clarity,
                            rating_completeness,
                            rating_helpfulness,
                            rating_global,
                            comment,
                            created_at,
                            updated_at
                        FROM {}
                        WHERE session_id = %s;
                        """
                    ).format(sql.Identifier("public", chat_evaluation_table)),
                    (resolved_session_id,),
                )
                evaluation_row = cur.fetchone()
                if evaluation_row:
                    selected_evaluation = {
                        "session_id": evaluation_row[0],
                        "rating_relevance": int(evaluation_row[1]),
                        "rating_accuracy": int(evaluation_row[2]),
                        "rating_clarity": int(evaluation_row[3]),
                        "rating_completeness": int(evaluation_row[4]),
                        "rating_helpfulness": int(evaluation_row[5]),
                        "rating_global": float(evaluation_row[6]) if evaluation_row[6] is not None else None,
                        "comment": evaluation_row[7] or "",
                        "created_at": evaluation_row[8],
                        "updated_at": evaluation_row[9],
                    }

    return {
        "project": project,
        "chat_table": chat_table,
        "chat_evaluation_table": chat_evaluation_table,
        "sessions": sessions,
        "selected_session_id": resolved_session_id,
        "selected_session_messages": selected_session_messages,
        "selected_evaluation": selected_evaluation,
    }


def get_project_chat_dashboard_payload(project_slug: str):
    """Build KPI analytics payload for the project chat dashboard.

    The payload includes score aggregates, weekly trends, and a session-length
    versus quality correlation view.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            table_names = ensure_project_tables_exist(cur, project_slug, include_chat=True)
            chat_table = table_names["chat_table"]
            chat_evaluation_table = table_names["chat_evaluation_table"]

            cur.execute(
                sql.SQL(
                    """
                    SELECT
                        COUNT(*)::int AS total_sessions,
                        round(avg(rating_global)::numeric, 2) AS avg_global,
                        round(avg(rating_relevance)::numeric, 2) AS avg_relevance,
                        round(avg(rating_accuracy)::numeric, 2) AS avg_accuracy,
                        round(avg(rating_clarity)::numeric, 2) AS avg_clarity,
                        round(avg(rating_completeness)::numeric, 2) AS avg_completeness,
                        round(avg(rating_helpfulness)::numeric, 2) AS avg_helpfulness,
                        round(
                            100.0 * count(*) FILTER (
                                WHERE rating_relevance >= 4
                                  AND rating_accuracy >= 4
                                  AND rating_clarity >= 4
                                  AND rating_completeness >= 4
                                  AND rating_helpfulness >= 4
                            ) / nullif(count(*), 0),
                            2
                        ) AS excellent_rate_pct,
                        round(
                            100.0 * count(*) FILTER (
                                WHERE rating_relevance <= 2
                                   OR rating_accuracy <= 2
                                   OR rating_clarity <= 2
                                   OR rating_completeness <= 2
                                   OR rating_helpfulness <= 2
                            ) / nullif(count(*), 0),
                            2
                        ) AS problematic_rate_pct
                    FROM {};
                    """
                ).format(sql.Identifier("public", chat_evaluation_table))
            )
            global_row = cur.fetchone()

            total_evaluated_sessions = int(global_row[0]) if global_row and global_row[0] is not None else 0
            avg_global_score = to_float_or_none(global_row[1]) if global_row else None

            axis_metrics = [
                {
                    "key": "rating_relevance",
                    "label": CHAT_RATING_LABELS["rating_relevance"],
                    "avg": to_float_or_none(global_row[2]) if global_row else None,
                },
                {
                    "key": "rating_accuracy",
                    "label": CHAT_RATING_LABELS["rating_accuracy"],
                    "avg": to_float_or_none(global_row[3]) if global_row else None,
                },
                {
                    "key": "rating_clarity",
                    "label": CHAT_RATING_LABELS["rating_clarity"],
                    "avg": to_float_or_none(global_row[4]) if global_row else None,
                },
                {
                    "key": "rating_completeness",
                    "label": CHAT_RATING_LABELS["rating_completeness"],
                    "avg": to_float_or_none(global_row[5]) if global_row else None,
                },
                {
                    "key": "rating_helpfulness",
                    "label": CHAT_RATING_LABELS["rating_helpfulness"],
                    "avg": to_float_or_none(global_row[6]) if global_row else None,
                },
            ]
            for axis_metric in axis_metrics:
                axis_metric["avg_pct"] = (
                    round((axis_metric["avg"] / 5.0) * 100.0, 2) if axis_metric["avg"] is not None else None
                )

            excellent_rate_pct = to_float_or_none(global_row[7]) if global_row else None
            problematic_rate_pct = to_float_or_none(global_row[8]) if global_row else None

            non_null_axis_metrics = [metric for metric in axis_metrics if metric["avg"] is not None]
            weakest_axis = min(non_null_axis_metrics, key=lambda metric: metric["avg"]) if non_null_axis_metrics else None

            cur.execute(
                sql.SQL(
                    """
                    WITH session_stats AS (
                        SELECT
                            session_id,
                            COUNT(*)::int AS msg_count
                        FROM {}
                        GROUP BY session_id
                    )
                    SELECT round(avg(s.msg_count)::numeric, 2) AS avg_messages_per_session
                    FROM session_stats s;
                    """
                ).format(
                    sql.Identifier("public", chat_table),
                )
            )
            avg_messages_row = cur.fetchone()
            avg_messages_per_session = to_float_or_none(avg_messages_row[0]) if avg_messages_row else None

            cur.execute(
                sql.SQL(
                    """
                    SELECT
                        date_trunc('week', created_at)::date AS week_start,
                        COUNT(*)::int AS sessions_count,
                        round(avg(rating_global)::numeric, 2) AS avg_global,
                        round(avg(rating_relevance)::numeric, 2) AS avg_relevance,
                        round(avg(rating_accuracy)::numeric, 2) AS avg_accuracy,
                        round(avg(rating_clarity)::numeric, 2) AS avg_clarity,
                        round(avg(rating_completeness)::numeric, 2) AS avg_completeness,
                        round(avg(rating_helpfulness)::numeric, 2) AS avg_helpfulness
                    FROM {}
                    GROUP BY 1
                    ORDER BY 1;
                    """
                ).format(sql.Identifier("public", chat_evaluation_table))
            )
            weekly_rows = cur.fetchall()
            weekly_scores = []
            for weekly_row in weekly_rows:
                avg_week_global = to_float_or_none(weekly_row[2])
                weekly_scores.append(
                    {
                        "week_start": weekly_row[0].isoformat() if weekly_row[0] else "",
                        "sessions_count": int(weekly_row[1]),
                        "avg_global": avg_week_global,
                        "avg_global_pct": round((avg_week_global / 5.0) * 100.0, 2)
                        if avg_week_global is not None
                        else None,
                        "avg_relevance": to_float_or_none(weekly_row[3]),
                        "avg_accuracy": to_float_or_none(weekly_row[4]),
                        "avg_clarity": to_float_or_none(weekly_row[5]),
                        "avg_completeness": to_float_or_none(weekly_row[6]),
                        "avg_helpfulness": to_float_or_none(weekly_row[7]),
                    }
                )

            cur.execute(
                sql.SQL(
                    """
                    WITH session_stats AS (
                        SELECT
                            session_id,
                            COUNT(*)::int AS msg_count
                        FROM {}
                        GROUP BY session_id
                    )
                    SELECT
                        s.msg_count,
                        COUNT(*)::int AS sessions_count,
                        round(avg(e.rating_global)::numeric, 2) AS avg_global
                    FROM session_stats s
                    JOIN {} e
                      ON e.session_id = s.session_id
                    GROUP BY s.msg_count
                    ORDER BY s.msg_count;
                    """
                ).format(
                    sql.Identifier("public", chat_table),
                    sql.Identifier("public", chat_evaluation_table),
                )
            )
            length_rows = cur.fetchall()
            length_score_relation = []
            for length_row in length_rows:
                avg_length_global = to_float_or_none(length_row[2])
                length_score_relation.append(
                    {
                        "msg_count": int(length_row[0]),
                        "sessions_count": int(length_row[1]),
                        "avg_global": avg_length_global,
                        "avg_global_pct": round((avg_length_global / 5.0) * 100.0, 2)
                        if avg_length_global is not None
                        else None,
                    }
                )

            cur.execute(
                sql.SQL(
                    """
                    WITH session_stats AS (
                        SELECT
                            session_id,
                            COUNT(*)::int AS msg_count
                        FROM {}
                        GROUP BY session_id
                    )
                    SELECT
                        CASE
                            WHEN s.msg_count BETWEEN 1 AND 2 THEN '1-2 messages'
                            WHEN s.msg_count BETWEEN 3 AND 5 THEN '3-5 messages'
                            WHEN s.msg_count BETWEEN 6 AND 10 THEN '6-10 messages'
                            ELSE '11+ messages'
                        END AS bucket_label,
                        CASE
                            WHEN s.msg_count BETWEEN 1 AND 2 THEN 1
                            WHEN s.msg_count BETWEEN 3 AND 5 THEN 2
                            WHEN s.msg_count BETWEEN 6 AND 10 THEN 3
                            ELSE 4
                        END AS bucket_order,
                        COUNT(*)::int AS sessions_count,
                        round(avg(e.rating_global)::numeric, 2) AS avg_global
                    FROM session_stats s
                    JOIN {} e
                      ON e.session_id = s.session_id
                    GROUP BY 1, 2
                    ORDER BY 2;
                    """
                ).format(
                    sql.Identifier("public", chat_table),
                    sql.Identifier("public", chat_evaluation_table),
                )
            )
            bucket_rows = cur.fetchall()
            length_buckets = []
            for bucket_row in bucket_rows:
                avg_bucket_global = to_float_or_none(bucket_row[3])
                length_buckets.append(
                    {
                        "bucket_label": bucket_row[0],
                        "bucket_order": int(bucket_row[1]),
                        "sessions_count": int(bucket_row[2]),
                        "avg_global": avg_bucket_global,
                        "avg_global_pct": round((avg_bucket_global / 5.0) * 100.0, 2)
                        if avg_bucket_global is not None
                        else None,
                    }
                )

            cur.execute(
                sql.SQL(
                    """
                    WITH session_stats AS (
                        SELECT
                            session_id,
                            COUNT(*)::int AS msg_count
                        FROM {}
                        GROUP BY session_id
                    )
                    SELECT corr(s.msg_count::float, e.rating_global::float) AS corr_msg_count_rating_global
                    FROM session_stats s
                    JOIN {} e
                      ON e.session_id = s.session_id;
                    """
                ).format(
                    sql.Identifier("public", chat_table),
                    sql.Identifier("public", chat_evaluation_table),
                )
            )
            correlation_row = cur.fetchone()
            corr_msg_count_rating_global = to_float_or_none(correlation_row[0]) if correlation_row else None

    return {
        "project": project,
        "chat_table": chat_table,
        "chat_evaluation_table": chat_evaluation_table,
        "total_evaluated_sessions": total_evaluated_sessions,
        "avg_global_score": avg_global_score,
        "axis_metrics": axis_metrics,
        "excellent_rate_pct": excellent_rate_pct,
        "problematic_rate_pct": problematic_rate_pct,
        "weakest_axis": weakest_axis,
        "avg_messages_per_session": avg_messages_per_session,
        "weekly_scores": weekly_scores,
        "length_score_relation": length_score_relation,
        "length_buckets": length_buckets,
        "corr_msg_count_rating_global": corr_msg_count_rating_global,
    }


def upsert_chat_evaluation(project_slug: str, payload):
    """Upsert chat evaluation."""
    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("Le champ 'session_id' est obligatoire.")

    ratings = {}
    for rating_field in CHAT_RATING_FIELDS:
        ratings[rating_field] = parse_rating_field(payload.get(rating_field), rating_field)
    comment = (payload.get("comment") or "").strip()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            _ = find_project_by_slug(cur, project_slug)
            create_project_tables(cur, project_slug)
            chat_table = f"{project_slug}_chat"
            chat_evaluation_table = f"{project_slug}_chat_evaluation"
            for table_name in [chat_table, chat_evaluation_table]:
                if not table_exists(cur, table_name):
                    raise ValueError(f"La table '{table_name}' est introuvable.")

            cur.execute(
                sql.SQL("SELECT 1 FROM {} WHERE session_id = %s LIMIT 1;").format(
                    sql.Identifier("public", chat_table)
                ),
                (session_id,),
            )
            if not cur.fetchone():
                raise ValueError(f"Session introuvable pour ce projet: '{session_id}'.")

            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        session_id,
                        rating_relevance,
                        rating_accuracy,
                        rating_clarity,
                        rating_completeness,
                        rating_helpfulness,
                        comment,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (session_id)
                    DO UPDATE SET
                        rating_relevance = EXCLUDED.rating_relevance,
                        rating_accuracy = EXCLUDED.rating_accuracy,
                        rating_clarity = EXCLUDED.rating_clarity,
                        rating_completeness = EXCLUDED.rating_completeness,
                        rating_helpfulness = EXCLUDED.rating_helpfulness,
                        comment = EXCLUDED.comment,
                        updated_at = now();
                    """
                ).format(sql.Identifier("public", chat_evaluation_table)),
                (
                    session_id,
                    ratings["rating_relevance"],
                    ratings["rating_accuracy"],
                    ratings["rating_clarity"],
                    ratings["rating_completeness"],
                    ratings["rating_helpfulness"],
                    comment or None,
                ),
            )

    return session_id


def delete_project(project_slug: str):
    """Delete project."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            _ = find_project_by_slug(cur, project_slug)
            ensure_business_tables(cur)
            cur.execute(
                f"DELETE FROM public.{QUALITY_OBSERVATION_TABLE} WHERE project_slug = %s;",
                (project_slug,),
            )
            cur.execute(
                f"DELETE FROM public.{DATASET_BUILD_TABLE} WHERE project_slug = %s;",
                (project_slug,),
            )
            cur.execute(
                f"DELETE FROM public.{CHUNK_METADATA_TABLE} WHERE project_slug = %s;",
                (project_slug,),
            )
            cur.execute(
                f"DELETE FROM public.{DOCUMENT_PROCESSING_TABLE} WHERE project_slug = %s;",
                (project_slug,),
            )
            cur.execute(
                f"DELETE FROM public.{DOCUMENT_REVIEW_ANNOTATION_TABLE} WHERE project_slug = %s;",
                (project_slug,),
            )
            cur.execute(
                f"DELETE FROM public.{DOCUMENT_SECTION_EXCLUSION_TABLE} WHERE project_slug = %s;",
                (project_slug,),
            )
            cur.execute(
                f"DELETE FROM public.{DOCUMENT_REGISTRY_TABLE} WHERE project_slug = %s;",
                (project_slug,),
            )
            for table_name in [
                f"{project_slug}_chat_evaluation",
                f"{project_slug}_chat",
                f"{project_slug}_chunk",
                f"{project_slug}_train",
                f"{project_slug}_shard",
            ]:
                cur.execute(
                    sql.SQL("DROP TABLE IF EXISTS {} CASCADE;").format(
                        sql.Identifier("public", table_name)
                    )
                )
            cur.execute(
                sql.SQL("DELETE FROM public.{} WHERE project_nameslug = %s;").format(
                    sql.Identifier(PROJECTS_TABLE)
                ),
                (project_slug,),
            )


def add_shard_record(project_slug: str, payload):
    """Add shard record."""
    source_document = (payload.get("source_document") or "").strip()
    url_document = (payload.get("url_document") or "").strip()
    title_document = (payload.get("title_document") or "").strip()
    content_document = (payload.get("content_document") or "").strip()
    autor_document = (payload.get("autor_document") or "").strip()
    if not content_document:
        raise ValueError("Le champ 'content_document' est obligatoire pour un shard.")

    shard_uuid = str(uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            ensure_business_tables(cur)
            shard_table = f"{project_slug}_shard"
            if not table_exists(cur, shard_table):
                raise ValueError(f"La table '{shard_table}' est introuvable.")
            lock_project_corpus_mutation(cur, project_slug)
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        uuid,
                        project_id,
                        source_document,
                        url_document,
                        title_document,
                        content_document,
                        autor_document
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """
                ).format(sql.Identifier("public", shard_table)),
                (
                    shard_uuid,
                    project["uuid"],
                    source_document or None,
                    url_document or None,
                    title_document or None,
                    content_document,
                    autor_document or None,
                ),
            )
            upsert_document_registry_record(cur, shard_uuid, project_slug)
            pipeline = run_normalization_pipeline(content_document)
            document = {
                "uuid": shard_uuid,
                "project_slug": project_slug,
            }
            upsert_document_processing_record(
                cur,
                document_id=shard_uuid,
                project_slug=project_slug,
                raw_content=content_document,
                quality_score=compute_quality_score(pipeline["normalized_content"]),
                approval_status="pending",
            )
            recalculate_document_quality_with_cursor(
                cur,
                document,
                content_document,
                pipeline["normalized_content"],
                DEFAULT_NORMALIZATION_VERSION,
            )
    return shard_uuid


def delete_shard_record(project_slug: str, shard_uuid: str):
    """Delete shard record."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            _ = find_project_by_slug(cur, project_slug)
            ensure_business_tables(cur)
            shard_table = f"{project_slug}_shard"
            chunk_table = f"{project_slug}_chunk"
            cur.execute(
                f"""
                DELETE FROM public.{QUALITY_OBSERVATION_TABLE}
                WHERE project_slug = %s
                  AND (document_id = %s OR chunk_id IN (
                      SELECT chunk_id
                      FROM public.{CHUNK_METADATA_TABLE}
                      WHERE project_slug = %s AND shard_id = %s
                  ));
                """,
                (project_slug, shard_uuid, project_slug, shard_uuid),
            )
            cur.execute(
                f"DELETE FROM public.{CHUNK_METADATA_TABLE} WHERE project_slug = %s AND shard_id = %s;",
                (project_slug, shard_uuid),
            )
            cur.execute(
                f"DELETE FROM public.{DOCUMENT_PROCESSING_TABLE} WHERE project_slug = %s AND document_id = %s;",
                (project_slug, shard_uuid),
            )
            cur.execute(
                f"DELETE FROM public.{DOCUMENT_REVIEW_ANNOTATION_TABLE} WHERE project_slug = %s AND document_id = %s;",
                (project_slug, shard_uuid),
            )
            cur.execute(
                f"DELETE FROM public.{DOCUMENT_SECTION_EXCLUSION_TABLE} WHERE project_slug = %s AND document_id = %s;",
                (project_slug, shard_uuid),
            )
            delete_document_registry_record(cur, shard_uuid, project_slug)
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE shard_id = %s;").format(
                    sql.Identifier("public", chunk_table)
                ),
                (shard_uuid,),
            )
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE uuid = %s;").format(
                    sql.Identifier("public", shard_table)
                ),
                (shard_uuid,),
            )


def add_chunk_record(project_slug: str, payload):
    """Add chunk record."""
    shard_id = (payload.get("shard_id") or "").strip()
    source_document = (payload.get("source_document") or "").strip()
    url_document = (payload.get("url_document") or "").strip()
    title_document = (payload.get("title_document") or "").strip()
    content_document = (payload.get("content_document") or "").strip()
    autor_document = (payload.get("autor_document") or "").strip()

    if not shard_id:
        raise ValueError("Le champ 'shard_id' est obligatoire pour un chunk.")
    if not content_document:
        raise ValueError("Le champ 'content_document' est obligatoire pour un chunk.")

    chunk_uuid = str(uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            _ = find_project_by_slug(cur, project_slug)
            ensure_business_tables(cur)
            shard_table = f"{project_slug}_shard"
            chunk_table = f"{project_slug}_chunk"
            cur.execute(
                sql.SQL("SELECT 1 FROM {} WHERE uuid = %s;").format(
                    sql.Identifier("public", shard_table)
                ),
                (shard_id,),
            )
            if not cur.fetchone():
                raise ValueError(f"Shard '{shard_id}' introuvable pour ce projet.")
            cur.execute(
                sql.SQL(
                    f"""
                    SELECT c.uuid
                    FROM {{}} AS c
                    LEFT JOIN public.{CHUNK_METADATA_TABLE} AS m
                      ON m.chunk_id = c.uuid
                     AND m.project_slug = %s
                    WHERE c.shard_id = %s
                    ORDER BY m.document_position_ratio NULLS LAST,
                             c.last_date_edit,
                             c.uuid;
                    """
                ).format(sql.Identifier("public", chunk_table)),
                (project_slug, shard_id),
            )
            existing_chunk_ids = [row[0] for row in cur.fetchall()]
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        uuid,
                        shard_id,
                        source_document,
                        url_document,
                        title_document,
                        content_document,
                        autor_document
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """
                ).format(sql.Identifier("public", chunk_table)),
                (
                    chunk_uuid,
                    shard_id,
                    source_document or None,
                    url_document or None,
                    title_document or None,
                    content_document,
                    autor_document or None,
                ),
            )
            cur.execute(
                f"""
                INSERT INTO public.{CHUNK_METADATA_TABLE} (
                    chunk_id,
                    project_slug,
                    shard_id,
                    document_id,
                    section_title,
                    section_path,
                    previous_document_id,
                    previous_chunk_id,
                    next_chunk_id,
                    summary_short,
                    document_position_ratio,
                    chunk_type,
                    zone_type,
                    strict_zone,
                    metadata,
                    quality_score,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, NULL, %s, 0, 'markdown', 'text', false, %s, %s, now())
                ON CONFLICT (chunk_id)
                DO UPDATE SET
                    project_slug = EXCLUDED.project_slug,
                    shard_id = EXCLUDED.shard_id,
                    document_id = EXCLUDED.document_id,
                    section_title = EXCLUDED.section_title,
                    section_path = EXCLUDED.section_path,
                    summary_short = EXCLUDED.summary_short,
                    document_position_ratio = EXCLUDED.document_position_ratio,
                    chunk_type = EXCLUDED.chunk_type,
                    zone_type = EXCLUDED.zone_type,
                    strict_zone = EXCLUDED.strict_zone,
                    metadata = EXCLUDED.metadata,
                    quality_score = EXCLUDED.quality_score,
                    updated_at = now();
                """,
                (
                    chunk_uuid,
                    project_slug,
                    shard_id,
                    shard_id,
                    title_document or "Manual",
                    title_document or "Manual",
                    build_summary_short(content_document),
                    Json({"source": "manual"}),
                    compute_quality_score(content_document),
                ),
            )
            update_persisted_chunk_lineage(
                cur,
                project_slug,
                [*existing_chunk_ids, chunk_uuid],
            )
    return chunk_uuid


def delete_chunk_record(project_slug: str, chunk_uuid: str):
    """Delete chunk record."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            _ = find_project_by_slug(cur, project_slug)
            ensure_business_tables(cur)
            chunk_table = f"{project_slug}_chunk"
            cur.execute(
                sql.SQL("SELECT shard_id FROM {} WHERE uuid = %s;").format(
                    sql.Identifier("public", chunk_table)
                ),
                (chunk_uuid,),
            )
            chunk_row = cur.fetchone()
            shard_id = chunk_row[0] if chunk_row else None
            cur.execute(
                f"""
                DELETE FROM public.{QUALITY_OBSERVATION_TABLE}
                WHERE project_slug = %s AND chunk_id = %s;
                """,
                (project_slug, chunk_uuid),
            )
            cur.execute(
                f"DELETE FROM public.{CHUNK_METADATA_TABLE} WHERE project_slug = %s AND chunk_id = %s;",
                (project_slug, chunk_uuid),
            )
            cur.execute(
                f"""
                DELETE FROM public.{DOCUMENT_REVIEW_ANNOTATION_TABLE}
                WHERE project_slug = %s
                  AND target_type = 'chunk'
                  AND target_id = %s;
                """,
                (project_slug, chunk_uuid),
            )
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE uuid = %s;").format(
                    sql.Identifier("public", chunk_table)
                ),
                (chunk_uuid,),
            )
            if shard_id:
                cur.execute(
                    sql.SQL(
                        f"""
                        SELECT c.uuid
                        FROM {{}} AS c
                        LEFT JOIN public.{CHUNK_METADATA_TABLE} AS m
                          ON m.chunk_id = c.uuid
                         AND m.project_slug = %s
                        WHERE c.shard_id = %s
                        ORDER BY m.document_position_ratio NULLS LAST,
                                 c.last_date_edit,
                                 c.uuid;
                        """
                    ).format(sql.Identifier("public", chunk_table)),
                    (project_slug, shard_id),
                )
                update_persisted_chunk_lineage(
                    cur,
                    project_slug,
                    [row[0] for row in cur.fetchall()],
                )


def delete_train_record(project_slug: str, train_uuid: str):
    """Delete train record."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            _ = find_project_by_slug(cur, project_slug)
            train_table = f"{project_slug}_train"
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE uuid = %s;").format(
                    sql.Identifier("public", train_table)
                ),
                (train_uuid,),
            )


def vote_train_record(project_slug: str, train_uuid: str, direction: str):
    """Vote train record."""
    if direction not in {"up", "down"}:
        raise ValueError("Direction de vote invalide.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            _ = find_project_by_slug(cur, project_slug)
            train_table = f"{project_slug}_train"
            field = "upvote" if direction == "up" else "downvote"
            cur.execute(
                sql.SQL("UPDATE {} SET {} = COALESCE({}, 0) + 1 WHERE uuid = %s;").format(
                    sql.Identifier("public", train_table),
                    sql.Identifier(field),
                    sql.Identifier(field),
                ),
                (train_uuid,),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Item train '{train_uuid}' introuvable.")


def add_train_record(project_slug: str, payload):
    """Add train record."""
    system_content = (payload.get("system_content") or "").strip()
    user_content = (payload.get("user_content") or "").strip()
    assistant_content = (payload.get("assistant_content") or "").strip()
    metatags = normalize_metatags(payload.get("metatags"))
    upvote = parse_int_field(payload.get("upvote"), "upvote", default=0)
    downvote = parse_int_field(payload.get("downvote"), "downvote", default=0)

    if not any([system_content, user_content, assistant_content]):
        raise ValueError("Saisissez au moins un contenu: system, user ou assistant.")
    if upvote < 0 or downvote < 0:
        raise ValueError("Les valeurs upvote/downvote doivent etre positives.")

    train_uuid = str(uuid4())

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            train_table = f"{project_slug}_train"
            if not table_exists(cur, train_table):
                raise ValueError(f"La table '{train_table}' est introuvable.")
            ensure_train_table_schema(cur, train_table)

            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        uuid,
                        project_id,
                        system_content,
                        user_content,
                        assistant_content,
                        metatags,
                        upvote,
                        downvote
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """
                ).format(sql.Identifier("public", train_table)),
                (
                    train_uuid,
                    project["uuid"],
                    system_content or None,
                    user_content or None,
                    assistant_content or None,
                    metatags or None,
                    upvote,
                    downvote,
                ),
            )

    return train_uuid


def estimate_tokens(text: str, options) -> int:
    """Estimate tokens."""
    if not text:
        return 0
    if options["tokenEstimator"] == "chars":
        return max(1, (len(text) + options["charsPerToken"] - 1) // options["charsPerToken"])
    return len(re.findall(r"\S+", text))


def split_by_token_window(text: str, max_tokens: int, overlap_tokens: int, options):
    """Split by token window."""
    if max_tokens <= 0 or not text.strip():
        return []

    if options["tokenEstimator"] == "chars":
        window_chars = max(1, max_tokens * options["charsPerToken"])
        overlap_chars = max(0, overlap_tokens * options["charsPerToken"])
        if overlap_chars >= window_chars:
            overlap_chars = window_chars - 1
        step = max(1, window_chars - overlap_chars)

        chunks = []
        start = 0
        while start < len(text):
            part = text[start : start + window_chars].strip()
            if part:
                chunks.append(part)
            if start + window_chars >= len(text):
                break
            start += step
        return chunks

    words = re.findall(r"\S+", text)
    if not words:
        return []

    if overlap_tokens >= max_tokens:
        overlap_tokens = max_tokens - 1
    step = max(1, max_tokens - overlap_tokens)

    chunks = []
    start = 0
    while start < len(words):
        part_words = words[start : start + max_tokens]
        if part_words:
            chunks.append(" ".join(part_words))
        if start + max_tokens >= len(words):
            break
        start += step
    return chunks


def extract_chunk_zones(section_content):
    """Split a section into normal, code, table and explicitly strict zones."""
    lines = (section_content or "").splitlines()
    zones = []
    normal_lines = []
    index = 0

    def flush_normal():
        text = "\n".join(normal_lines).strip()
        if text:
            zones.append({"zone_type": "text", "content": text, "strict": False})
        normal_lines.clear()

    while index < len(lines):
        line = lines[index]
        strict_match = re.match(
            r"^\s*<!--\s*chunk:strict:start(?:\s+([^>]+?))?\s*-->\s*$",
            line,
            flags=re.IGNORECASE,
        )
        if strict_match:
            flush_normal()
            strict_lines = []
            index += 1
            while index < len(lines) and not re.match(
                r"^\s*<!--\s*chunk:strict:end\s*-->\s*$",
                lines[index],
                flags=re.IGNORECASE,
            ):
                strict_lines.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ValueError("Zone stricte non fermee: marqueur 'chunk:strict:end' manquant.")
            strict_content = "\n".join(strict_lines).strip()
            if strict_content:
                zones.append(
                    {
                        "zone_type": "strict",
                        "content": strict_content,
                        "strict": True,
                        "label": (strict_match.group(1) or "").strip(),
                    }
                )
            index += 1
            continue

        fence_match = re.match(r"^\s*(```+|~~~+)(.*)$", line)
        if fence_match:
            flush_normal()
            fence = fence_match.group(1)
            closing_pattern = rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$"
            code_lines = [line]
            closed = False
            index += 1
            while index < len(lines):
                code_lines.append(lines[index])
                if re.match(closing_pattern, lines[index]):
                    closed = True
                    break
                index += 1
            if not closed:
                raise ValueError("Bloc de code non ferme.")
            zones.append(
                {
                    "zone_type": "code",
                    "content": "\n".join(code_lines).strip(),
                    "strict": True,
                }
            )
            index += 1
            continue

        if (
            index + 1 < len(lines)
            and "|" in line
            and re.match(
                r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$",
                lines[index + 1],
            )
        ):
            flush_normal()
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table_lines.append(lines[index])
                index += 1
            zones.append(
                {
                    "zone_type": "table",
                    "content": "\n".join(table_lines).strip(),
                    "strict": True,
                }
            )
            continue

        normal_lines.append(line)
        index += 1

    flush_normal()
    return zones


def merge_small_paragraphs(paragraphs, options):
    """Merge undersized paragraphs with adjacent prose before chunk packing."""
    if not options.get("mergeSmallParagraphs", True):
        return paragraphs
    minimum = options.get("smallParagraphMinTokens", 40)
    merged = []
    pending = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if pending:
            candidate = f"{pending}\n\n{paragraph}"
            if estimate_tokens(candidate, options) <= options["chunkMaxTokens"]:
                pending = candidate
                if estimate_tokens(pending, options) >= minimum:
                    merged.append(pending)
                    pending = ""
                continue
            merged.append(pending)
            pending = ""
        if estimate_tokens(paragraph, options) < minimum:
            if merged:
                candidate = f"{merged[-1]}\n\n{paragraph}"
                if estimate_tokens(candidate, options) <= options["chunkMaxTokens"]:
                    merged[-1] = candidate
                    continue
            pending = paragraph
        else:
            merged.append(paragraph)
    if pending:
        if merged:
            candidate = f"{merged[-1]}\n\n{pending}"
            if estimate_tokens(candidate, options) <= options["chunkMaxTokens"]:
                merged[-1] = candidate
            else:
                merged.append(pending)
        else:
            merged.append(pending)
    return merged


def split_code_zone(code_content, options):
    """Split oversized fenced code only at line boundaries and keep valid fences."""
    target_limit = min(options["chunkMaxTokens"], options["hardMaxTokens"])
    if estimate_tokens(code_content, options) <= target_limit:
        return [code_content]
    lines = code_content.splitlines()
    opener = lines[0]
    closer = lines[-1]
    body_lines = lines[1:-1]
    fence_cost = estimate_tokens(f"{opener}\n{closer}", options)
    if fence_cost > options["hardMaxTokens"]:
        raise ValueError("Les marqueurs du bloc de code depassent hardMaxTokens.")
    chunks = []
    current = []
    for line in body_lines:
        candidate = "\n".join([opener, *current, line, closer])
        if current and estimate_tokens(candidate, options) > target_limit:
            chunks.append("\n".join([opener, *current, closer]))
            current = []
        single = "\n".join([opener, line, closer])
        if estimate_tokens(single, options) > options["hardMaxTokens"]:
            raise ValueError("Une ligne de code depasse hardMaxTokens.")
        current.append(line)
    if current:
        chunks.append("\n".join([opener, *current, closer]))
    if not chunks:
        chunks = [f"{opener}\n{closer}"]
    if any(estimate_tokens(chunk, options) > options["hardMaxTokens"] for chunk in chunks):
        raise ValueError("Un bloc de code genere depasse hardMaxTokens.")
    return chunks


def split_table_zone(table_content, options):
    """Split oversized Markdown tables by rows while repeating the header."""
    target_limit = min(options["chunkMaxTokens"], options["hardMaxTokens"])
    if estimate_tokens(table_content, options) <= target_limit:
        return [table_content]
    lines = table_content.splitlines()
    header = lines[:2]
    rows = lines[2:]
    header_content = "\n".join(header)
    if estimate_tokens(header_content, options) > options["hardMaxTokens"]:
        raise ValueError("L'en-tete du tableau depasse hardMaxTokens.")
    chunks = []
    current_rows = []
    for row in rows:
        candidate = "\n".join([*header, *current_rows, row])
        if current_rows and estimate_tokens(candidate, options) > target_limit:
            chunks.append("\n".join([*header, *current_rows]))
            current_rows = []
        if estimate_tokens("\n".join([*header, row]), options) > options["hardMaxTokens"]:
            raise ValueError("Une ligne de tableau depasse hardMaxTokens.")
        current_rows.append(row)
    if current_rows:
        chunks.append("\n".join([*header, *current_rows]))
    if not chunks:
        chunks = [header_content]
    if any(estimate_tokens(chunk, options) > options["hardMaxTokens"] for chunk in chunks):
        raise ValueError("Un tableau genere depasse hardMaxTokens.")
    return chunks


def split_text_zone(text, options):
    """Split ordinary prose with paragraph merging and local overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not options.get("mergeSmallParagraphs", True):
        chunks = []
        for paragraph in paragraphs or [text]:
            if estimate_tokens(paragraph, options) <= options["chunkMaxTokens"]:
                chunks.append(paragraph)
            else:
                chunks.extend(
                    split_by_token_window(
                        paragraph,
                        options["chunkMaxTokens"],
                        options["chunkOverlapTokens"],
                        options,
                    )
                )
        return chunks

    if estimate_tokens(text, options) <= options["chunkMaxTokens"]:
        return [text]
    paragraphs = merge_small_paragraphs(paragraphs or [text], options)
    chunks = []
    current_parts = []
    for paragraph in paragraphs:
        if estimate_tokens(paragraph, options) > options["chunkMaxTokens"]:
            if current_parts:
                chunks.append("\n\n".join(current_parts).strip())
                current_parts = []
            chunks.extend(
                split_by_token_window(
                    paragraph,
                    options["chunkMaxTokens"],
                    options["chunkOverlapTokens"],
                    options,
                )
            )
            continue

        candidate_text = "\n\n".join([*current_parts, paragraph]).strip()
        if not current_parts or estimate_tokens(candidate_text, options) <= options["chunkMaxTokens"]:
            current_parts.append(paragraph)
            continue
        chunks.append("\n\n".join(current_parts).strip())
        current_parts = [paragraph]
    if current_parts:
        chunks.append("\n\n".join(current_parts).strip())
    chunks = [chunk for chunk in chunks if chunk]
    if any(estimate_tokens(chunk, options) > options["hardMaxTokens"] for chunk in chunks):
        raise ValueError("Un chunk de texte genere depasse hardMaxTokens.")
    return chunks


def split_markdown_sections(markdown_text: str, heading_max_level: int):
    """Split markdown sections."""
    text = (markdown_text or "").replace("\r\n", "\n")
    if not text.strip():
        return []

    sections = []
    heading_stack = []
    current_title = "Introduction"
    current_path = ["Introduction"]
    current_lines = []
    active_fence = ""

    def push_current_section():
        """Run push current section."""
        content = "\n".join(current_lines).strip()
        if content or current_title != "Introduction":
            sections.append(
                {
                    "section_title": current_title,
                    "section_path": " > ".join(current_path),
                    "content": content,
                }
            )

    for line in text.split("\n"):
        if active_fence:
            closing_pattern = rf"^\s*{re.escape(active_fence[0])}{{{len(active_fence)},}}\s*$"
            if re.match(closing_pattern, line):
                active_fence = ""
            current_lines.append(line)
            continue
        fence_match = re.match(r"^\s*(```+|~~~+)(.*)$", line)
        if fence_match:
            active_fence = fence_match.group(1)
            current_lines.append(line)
            continue
        heading_match = (
            None
            if active_fence
            else re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        )
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip() or "Section"
            if level <= heading_max_level:
                push_current_section()
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(title)
                current_title = title
                current_path = heading_stack.copy()
                current_lines = []
                continue
        current_lines.append(line)

    push_current_section()

    normalized = []
    for section in sections:
        section_content = section["content"].strip()
        if section_content:
            normalized.append(section)
        elif section["section_title"]:
            normalized.append(
                {
                    "section_title": section["section_title"],
                    "section_path": section["section_path"],
                    "content": section["section_title"],
                }
            )
    return normalized


def split_section_content(section_content: str, options):
    """Backward-compatible text-only view of structure-aware section chunks."""
    return [part["content"] for part in split_section_content_aware(section_content, options)]


def split_section_content_aware(section_content: str, options):
    """Split a section without crossing strict code, table or custom zones."""
    section_content = (section_content or "").strip()
    if not section_content:
        return []
    strict_zone_types = set(options.get("strictZoneTypes") or [])
    parts = []
    for zone in extract_chunk_zones(section_content):
        zone_type = zone["zone_type"]
        is_strict = zone_type in strict_zone_types
        if zone_type == "code" and options.get("codeAware", True):
            contents = split_code_zone(zone["content"], options)
            chunk_type = "code"
        elif zone_type == "table" and options.get("tableAware", True):
            contents = split_table_zone(zone["content"], options)
            chunk_type = "table"
        elif zone_type == "strict":
            if estimate_tokens(zone["content"], options) > options["hardMaxTokens"]:
                raise ValueError(
                    f"Zone stricte '{zone.get('label') or 'sans nom'}' depasse hardMaxTokens."
                )
            contents = [zone["content"]]
            chunk_type = "strict"
        else:
            contents = split_text_zone(zone["content"], options)
            chunk_type = "markdown"
        for content in contents:
            parts.append(
                {
                    "content": content,
                    "zone_type": zone_type,
                    "chunk_type": chunk_type,
                    "strict_zone": is_strict,
                }
            )
    return parts


def build_summary_short(text, max_chars=240):
    """Build a compact deterministic summary suitable for retrieval previews."""
    max_chars = max(0, int(max_chars))
    if max_chars == 0:
        return ""
    cleaned = re.sub(r"(?m)^#{1,6}\s+", "", text or "")
    cleaned = re.sub(r"(?m)^(```+|~~~+).*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
    summary = first_sentence if len(first_sentence) <= max_chars else cleaned[:max_chars]
    is_truncated = len(summary) < len(cleaned)
    if not is_truncated:
        return summary[:max_chars]
    if max_chars == 1:
        return "…"
    return summary[: max_chars - 1].rstrip(" ,;:-") + "…"


def add_section_heading_to_text(text, section_title, options):
    """Prefix prose with its heading without exceeding the hard token budget."""
    body = (text or "").strip()
    title = (section_title or "").strip()
    if not body or not title or title == "Introduction" or body.startswith(title):
        return [body]

    prefixed = f"{title}\n\n{body}"
    if estimate_tokens(prefixed, options) <= options["hardMaxTokens"]:
        return [prefixed]

    if options["tokenEstimator"] == "chars":
        hard_capacity = options["hardMaxTokens"] * options["charsPerToken"]
        target_capacity = options["chunkMaxTokens"] * options["charsPerToken"]
        prefix_length = len(title) + 2
        hard_body_tokens = (hard_capacity - prefix_length) // options["charsPerToken"]
        target_body_tokens = (target_capacity - prefix_length) // options["charsPerToken"]
    else:
        title_tokens = estimate_tokens(title, options)
        hard_body_tokens = options["hardMaxTokens"] - title_tokens
        target_body_tokens = options["chunkMaxTokens"] - title_tokens

    if hard_body_tokens < 1:
        # The title remains available in metadata when it cannot safely be
        # repeated inside the chunk content.
        return [body]

    body_options = options.copy()
    body_options["hardMaxTokens"] = hard_body_tokens
    body_options["chunkMaxTokens"] = max(
        1,
        min(hard_body_tokens, target_body_tokens),
    )
    # The body part was already split once with the configured overlap.
    body_options["chunkOverlapTokens"] = 0
    body_options["smallParagraphMinTokens"] = min(
        body_options["smallParagraphMinTokens"],
        body_options["chunkMaxTokens"],
    )
    chunks = [
        f"{title}\n\n{part}"
        for part in split_text_zone(body, body_options)
        if part
    ]
    if any(estimate_tokens(chunk, options) > options["hardMaxTokens"] for chunk in chunks):
        raise ValueError("Le titre de section fait depasser hardMaxTokens.")
    return chunks


def finalize_chunk_lineage(items):
    """Recompute linked-list pointers and position ratios after filtering."""
    item_count = len(items)
    for index, item in enumerate(items):
        metadata = item["metadata"]
        metadata["previous_chunk_id"] = items[index - 1]["chunk_id"] if index > 0 else None
        metadata["next_chunk_id"] = items[index + 1]["chunk_id"] if index < item_count - 1 else None
        metadata["document_position_ratio"] = (
            round(index / float(item_count - 1), 5) if item_count > 1 else 0.0
        )
    return items


def update_persisted_chunk_lineage(cur, project_slug, ordered_chunk_ids):
    """Persist pointers and normalized positions for an ordered chunk list."""
    chunk_count = len(ordered_chunk_ids)
    for index, chunk_id in enumerate(ordered_chunk_ids):
        previous_chunk_id = ordered_chunk_ids[index - 1] if index > 0 else None
        next_chunk_id = (
            ordered_chunk_ids[index + 1]
            if index < chunk_count - 1
            else None
        )
        position_ratio = (
            round(index / float(chunk_count - 1), 5)
            if chunk_count > 1
            else 0.0
        )
        cur.execute(
            f"""
            UPDATE public.{CHUNK_METADATA_TABLE}
            SET previous_chunk_id = %s,
                next_chunk_id = %s,
                document_position_ratio = %s,
                updated_at = now()
            WHERE project_slug = %s
              AND chunk_id = %s;
            """,
            (
                previous_chunk_id,
                next_chunk_id,
                position_ratio,
                project_slug,
                chunk_id,
            ),
        )


def build_chunks_for_document(document, previous_document_id, options):
    """Build chunk payloads for one document and wire chunk lineage metadata."""
    sections = split_markdown_sections(
        document.get("content_document", ""),
        options["headingMaxLevel"],
    )
    if not sections:
        # Guarantee a fallback section so empty/flat documents still produce chunks.
        sections = [
            {
                "section_title": document.get("title_document") or "Document",
                "section_path": document.get("title_document") or "Document",
                "content": document.get("content_document") or "",
            }
        ]

    items = []
    for section in sections:
        section_parts = split_section_content_aware(section["content"], options)
        if not section_parts:
            section_parts = [
                {
                    "content": section["section_title"],
                    "zone_type": "text",
                    "chunk_type": "markdown",
                    "strict_zone": False,
                }
            ]

        for section_part in section_parts:
            text = section_part["content"].strip()
            if not text:
                text = section["section_title"]
            texts = (
                add_section_heading_to_text(text, section["section_title"], options)
                if section_part["zone_type"] == "text"
                else [text]
            )
            for final_text in texts:
                if estimate_tokens(final_text, options) > options["hardMaxTokens"]:
                    raise ValueError("Un chunk genere depasse hardMaxTokens.")
                chunk_id = str(uuid4())
                items.append(
                    {
                        "chunk_id": chunk_id,
                        "pageContent": final_text,
                        "text": final_text,
                        "metadata": {
                            "document_id": document["uuid"],
                            "section_title": section["section_title"],
                            "section_path": section["section_path"],
                            "previous_document_id": previous_document_id,
                            "previous_chunk_id": None,
                            "next_chunk_id": None,
                            "summary_short": build_summary_short(final_text),
                            "document_position_ratio": 0.0,
                            "chunk_type": section_part["chunk_type"],
                            "zone_type": section_part["zone_type"],
                            "strict_zone": section_part["strict_zone"],
                        },
                    }
                )

    return finalize_chunk_lineage(items)


def chunkify_project_shards(project_slug: str, options):
    """Regenerate chunks and lineage metadata from project shard documents.

    Existing chunk rows/metadata for each shard are cleared first, then rebuilt
    in the same transaction to keep content and metadata consistent.
    """
    shard_table = f"{project_slug}_shard"
    chunk_table = f"{project_slug}_chunk"
    generated_items = []

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            if not table_exists(cur, shard_table):
                raise ValueError(f"La table '{shard_table}' est introuvable.")
            if not table_exists(cur, chunk_table):
                raise ValueError(f"La table '{chunk_table}' est introuvable.")

            lock_project_corpus_mutation(cur, project_slug)

            cur.execute(
                sql.SQL(
                    """
                    SELECT
                        uuid,
                        project_id,
                        source_document,
                        url_document,
                        title_document,
                        content_document,
                        autor_document
                    FROM {}
                    ORDER BY uuid;
                    """
                ).format(sql.Identifier("public", shard_table))
            )
            shard_rows = cur.fetchall()

            previous_document_id = None
            excluded_paths_by_document = load_project_excluded_section_paths(cur, project_slug)
            for shard_row in shard_rows:
                document = {
                    "uuid": shard_row[0],
                    "project_id": shard_row[1],
                    "source_document": shard_row[2],
                    "url_document": shard_row[3],
                    "title_document": shard_row[4],
                    "content_document": shard_row[5] or "",
                    "autor_document": shard_row[6],
                }
                document_chunks = build_chunks_for_document(
                    document,
                    previous_document_id,
                    options,
                )
                excluded_section_paths = excluded_paths_by_document.get(document["uuid"], set())
                if excluded_section_paths:
                    document_chunks = [
                        chunk_item
                        for chunk_item in document_chunks
                        if chunk_item["metadata"].get("section_path") not in excluded_section_paths
                    ]
                finalize_chunk_lineage(document_chunks)
                previous_document_id = document["uuid"]

                # Regeneration is idempotent: remove previous chunks for this shard first.
                cur.execute(
                    f"""
                    DELETE FROM public.{QUALITY_OBSERVATION_TABLE}
                    WHERE project_slug = %s
                      AND chunk_id IN (
                          SELECT chunk_id
                          FROM public.{CHUNK_METADATA_TABLE}
                          WHERE project_slug = %s AND shard_id = %s
                      );
                    """,
                    (project_slug, project_slug, document["uuid"]),
                )
                cur.execute(
                    sql.SQL("DELETE FROM {} WHERE shard_id = %s;").format(
                        sql.Identifier("public", chunk_table)
                    ),
                    (document["uuid"],),
                )
                cur.execute(
                    f"DELETE FROM public.{CHUNK_METADATA_TABLE} WHERE project_slug = %s AND shard_id = %s;",
                    (project_slug, document["uuid"]),
                )

                for chunk_item in document_chunks:
                    generated_items.append(chunk_item)
                    cur.execute(
                        sql.SQL(
                            """
                            INSERT INTO {} (
                                uuid,
                                shard_id,
                                source_document,
                                url_document,
                                title_document,
                                content_document,
                                autor_document
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                            """
                        ).format(sql.Identifier("public", chunk_table)),
                        (
                            chunk_item["chunk_id"],
                            document["uuid"],
                            document["source_document"],
                            document["url_document"],
                            chunk_item["metadata"]["section_title"],
                            chunk_item["pageContent"],
                            document["autor_document"],
                        ),
                    )
                    # Keep metadata synchronized with content rows in the same transaction.
                    cur.execute(
                        f"""
                        INSERT INTO public.{CHUNK_METADATA_TABLE} (
                            chunk_id,
                            project_slug,
                            shard_id,
                            document_id,
                            section_title,
                            section_path,
                            previous_document_id,
                            previous_chunk_id,
                            next_chunk_id,
                            summary_short,
                            document_position_ratio,
                            chunk_type,
                            zone_type,
                            strict_zone,
                            metadata,
                            quality_score,
                            updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (chunk_id)
                        DO UPDATE SET
                            project_slug = EXCLUDED.project_slug,
                            shard_id = EXCLUDED.shard_id,
                            document_id = EXCLUDED.document_id,
                            section_title = EXCLUDED.section_title,
                            section_path = EXCLUDED.section_path,
                            previous_document_id = EXCLUDED.previous_document_id,
                            previous_chunk_id = EXCLUDED.previous_chunk_id,
                            next_chunk_id = EXCLUDED.next_chunk_id,
                            summary_short = EXCLUDED.summary_short,
                            document_position_ratio = EXCLUDED.document_position_ratio,
                            chunk_type = EXCLUDED.chunk_type,
                            zone_type = EXCLUDED.zone_type,
                            strict_zone = EXCLUDED.strict_zone,
                            metadata = EXCLUDED.metadata,
                            quality_score = EXCLUDED.quality_score,
                            updated_at = now();
                        """,
                        (
                            chunk_item["chunk_id"],
                            project_slug,
                            document["uuid"],
                            chunk_item["metadata"]["document_id"],
                            chunk_item["metadata"]["section_title"],
                            chunk_item["metadata"]["section_path"],
                            chunk_item["metadata"]["previous_document_id"],
                            chunk_item["metadata"]["previous_chunk_id"],
                            chunk_item["metadata"]["next_chunk_id"],
                            chunk_item["metadata"]["summary_short"],
                            chunk_item["metadata"]["document_position_ratio"],
                            chunk_item["metadata"]["chunk_type"],
                            chunk_item["metadata"]["zone_type"],
                            chunk_item["metadata"]["strict_zone"],
                            Json(
                                {
                                    "zone_type": chunk_item["metadata"]["zone_type"],
                                    "strict_zone": chunk_item["metadata"]["strict_zone"],
                                    "chunk_options": options,
                                }
                            ),
                            compute_quality_score(chunk_item["pageContent"]),
                        ),
                    )

    return generated_items


def list_projects_shards():
    """Return projects shards."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_projects_table(cur)
            cur.execute(
                sql.SQL(
                    """
                    SELECT uuid, project_name, project_nameslug
                    FROM public.{}
                    ORDER BY uuid DESC;
                    """
                ).format(sql.Identifier(PROJECTS_TABLE))
            )
            project_rows = cur.fetchall()

            projects = []
            for project_row in project_rows:
                project_uuid, project_name, project_slug = project_row
                shard_table = f"{project_slug}_shard"
                chunk_table = f"{project_slug}_chunk"
                train_table = f"{project_slug}_train"
                chat_table = f"{project_slug}_chat"
                chat_evaluation_table = f"{project_slug}_chat_evaluation"

                if not table_exists(cur, shard_table):
                    shard_rows = []
                elif table_exists(cur, chunk_table):
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT
                                s.uuid,
                                s.title_document,
                                s.source_document,
                                COUNT(c.uuid)::int AS chunk_count
                            FROM {} AS s
                            LEFT JOIN {} AS c
                                ON c.shard_id = s.uuid
                            GROUP BY s.uuid, s.title_document, s.source_document
                            ORDER BY s.uuid;
                            """
                        ).format(
                            sql.Identifier("public", shard_table),
                            sql.Identifier("public", chunk_table),
                        )
                    )
                    shard_rows = cur.fetchall()
                else:
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT
                                s.uuid,
                                s.title_document,
                                s.source_document,
                                0 AS chunk_count
                            FROM {} AS s
                            ORDER BY s.uuid;
                            """
                        ).format(sql.Identifier("public", shard_table))
                    )
                    shard_rows = cur.fetchall()

                shards = []
                total_chunks = 0
                for shard_row in shard_rows:
                    chunk_count = int(shard_row[3])
                    total_chunks += chunk_count
                    shards.append(
                        {
                            "uuid": shard_row[0],
                            "title_document": shard_row[1],
                            "source_document": shard_row[2],
                            "chunk_count": chunk_count,
                        }
                    )

                train_count = 0
                train_items = []
                if table_exists(cur, train_table):
                    ensure_train_table_schema(cur, train_table)
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT COUNT(*)::int
                            FROM {};
                            """
                        ).format(sql.Identifier("public", train_table))
                    )
                    train_count = int(cur.fetchone()[0])
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT
                                uuid,
                                system_content,
                                user_content,
                                assistant_content,
                                metatags,
                                upvote,
                                downvote
                            FROM {}
                            ORDER BY last_date_edit DESC, uuid DESC
                            LIMIT 5;
                            """
                        ).format(sql.Identifier("public", train_table))
                    )
                    train_rows = cur.fetchall()
                    for train_row in train_rows:
                        train_items.append(
                            {
                                "uuid": train_row[0],
                                "system_content": train_row[1] or "",
                                "user_content": train_row[2] or "",
                                "assistant_content": train_row[3] or "",
                                "metatags": train_row[4] or "",
                                "upvote": train_row[5] if train_row[5] is not None else 0,
                                "downvote": train_row[6] if train_row[6] is not None else 0,
                            }
                        )

                chat_message_count = 0
                chat_session_count = 0
                if table_exists(cur, chat_table):
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT
                                COUNT(*)::int AS message_count,
                                COUNT(DISTINCT session_id)::int AS session_count
                            FROM {};
                            """
                        ).format(sql.Identifier("public", chat_table))
                    )
                    chat_stats_row = cur.fetchone()
                    chat_message_count = int(chat_stats_row[0]) if chat_stats_row else 0
                    chat_session_count = int(chat_stats_row[1]) if chat_stats_row else 0

                chat_evaluation_count = 0
                if table_exists(cur, chat_evaluation_table):
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT COUNT(*)::int
                            FROM {};
                            """
                        ).format(sql.Identifier("public", chat_evaluation_table))
                    )
                    chat_evaluation_count = int(cur.fetchone()[0])

                projects.append(
                    {
                        "uuid": project_uuid,
                        "name": project_name,
                        "slug": project_slug,
                        "shard_table": shard_table,
                        "chunk_table": chunk_table,
                        "train_table": train_table,
                        "chat_table": chat_table,
                        "chat_evaluation_table": chat_evaluation_table,
                        "shard_count": len(shards),
                        "total_chunks": total_chunks,
                        "train_count": train_count,
                        "chat_message_count": chat_message_count,
                        "chat_session_count": chat_session_count,
                        "chat_evaluation_count": chat_evaluation_count,
                        "train_items": train_items,
                        "shards": shards,
                    }
                )

    return projects


def to_iso_or_none(value):
    """Convert value to iso or none."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def read_json_payload():
    """Read json payload."""
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return {}


def list_project_slugs(cur):
    """Return project slugs."""
    ensure_projects_table(cur)
    cur.execute(
        sql.SQL(
            """
            SELECT project_nameslug
            FROM public.{}
            ORDER BY project_nameslug;
            """
        ).format(sql.Identifier(PROJECTS_TABLE))
    )
    return [row[0] for row in cur.fetchall()]


def load_document_record_from_project(cur, project_slug: str, document_id: str):
    """Load document record from project."""
    slug = (project_slug or "").strip()
    doc_id = (document_id or "").strip()
    if not slug or not doc_id:
        return None

    shard_table = f"{slug}_shard"
    if not table_exists(cur, shard_table):
        return None

    cur.execute(
        sql.SQL(
            """
            SELECT
                uuid,
                project_id,
                source_document,
                url_document,
                title_document,
                content_document,
                autor_document
            FROM {}
            WHERE uuid = %s
            LIMIT 1;
            """
        ).format(sql.Identifier("public", shard_table)),
        (doc_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    upsert_document_registry_record(cur, doc_id, slug)
    return {
        "uuid": row[0],
        "project_id": row[1] or "",
        "source_document": row[2] or "",
        "url_document": row[3] or "",
        "title_document": row[4] or "",
        "content_document": row[5] or "",
        "autor_document": row[6] or "",
        "project_slug": slug,
        "shard_table": shard_table,
        "chunk_table": f"{slug}_chunk",
    }


def find_document_record(cur, document_id: str, preferred_project_slug: str = ""):
    """Resolve a document across projects with deterministic lookup order.

    Lookup order is: preferred project, document registry hint, then project
    scan. Stale registry mappings are cleaned up when they no longer resolve.
    """
    doc_id = (document_id or "").strip()
    if not doc_id:
        raise ValueError("Le champ 'document_id' est obligatoire.")

    preferred_slug = (preferred_project_slug or "").strip()
    # Lookup order is optimized for the most probable source to avoid full scans.
    if preferred_slug:
        preferred_record = load_document_record_from_project(cur, preferred_slug, doc_id)
        if preferred_record:
            return preferred_record

    registry_slug = get_document_registry_project(cur, doc_id)
    if registry_slug and registry_slug != preferred_slug:
        registry_record = load_document_record_from_project(cur, registry_slug, doc_id)
        if registry_record:
            return registry_record
        # Remove stale registry mappings when the referenced record disappeared.
        delete_document_registry_record(cur, doc_id, registry_slug)

    skip_slugs = {slug for slug in [preferred_slug, registry_slug] if slug}
    for slug in list_project_slugs(cur):
        if slug in skip_slugs:
            continue
        scanned_record = load_document_record_from_project(cur, slug, doc_id)
        if scanned_record:
            return scanned_record

    if preferred_slug:
        raise ValueError(
            f"Document '{doc_id}' introuvable dans le projet '{preferred_slug}'."
        )
    raise ValueError(f"Document introuvable pour l'UUID '{doc_id}'.")


def get_document_processing_record(cur, document_id: str):
    """Return the latest document processing state for a document id."""
    ensure_business_tables(cur)
    cur.execute(
        f"""
        SELECT
            document_id,
            project_slug,
            normalization_version,
            raw_content,
            normalized_content,
            rendered_text,
            structured_content,
            normalization_config,
            detected_language,
            content_type,
            extracted_metadata,
            approval_status,
            approval_comment,
            approved_by,
            approved_at,
            quality_score,
            created_at,
            updated_at
        FROM public.{DOCUMENT_PROCESSING_TABLE}
        WHERE document_id = %s;
        """,
        (document_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    return {
        "document_id": row[0],
        "project_slug": row[1],
        "normalization_version": row[2],
        "raw_content": row[3] or "",
        "normalized_content": row[4] or "",
        "rendered_text": row[5] or "",
        "structured_content": row[6] or {"section_count": 0, "sections": []},
        "normalization_config": row[7] or {},
        "detected_language": row[8] or "und",
        "content_type": row[9] or "unknown",
        "extracted_metadata": row[10] or {},
        "approval_status": row[11],
        "approval_comment": row[12] or "",
        "approved_by": row[13] or "",
        "approved_at": to_iso_or_none(row[14]),
        "quality_score": float(row[15]) if row[15] is not None else None,
        "created_at": to_iso_or_none(row[16]),
        "updated_at": to_iso_or_none(row[17]),
    }


def upsert_document_processing_record(
    cur,
    document_id: str,
    project_slug: str,
    normalization_version=None,
    raw_content=None,
    normalized_content=None,
    rendered_text=None,
    structured_content=None,
    normalization_config=None,
    detected_language=None,
    content_type=None,
    extracted_metadata=None,
    approval_status=None,
    approval_comment=None,
    approved_by=None,
    approved_at=None,
    quality_score=None,
):
    """Upsert document processing record."""
    if approval_status is not None and approval_status not in {"pending", "approved", "rejected"}:
        raise ValueError("Le champ 'status' doit etre: pending, approved ou rejected.")

    existing = get_document_processing_record(cur, document_id)
    if existing:
        merged_project_slug = (project_slug or existing["project_slug"]).strip()
        merged_normalization_version = (
            normalization_version
            if normalization_version is not None
            else existing["normalization_version"]
        )
        merged_raw_content = raw_content if raw_content is not None else existing["raw_content"]
        merged_normalized_content = (
            normalized_content if normalized_content is not None else existing["normalized_content"]
        )
        merged_rendered_text = rendered_text if rendered_text is not None else existing["rendered_text"]
        merged_structured_content = (
            structured_content if structured_content is not None else existing["structured_content"]
        )
        merged_normalization_config = (
            normalization_config
            if normalization_config is not None
            else existing["normalization_config"]
        )
        merged_detected_language = (
            detected_language
            if detected_language is not None
            else existing["detected_language"]
        )
        merged_content_type = content_type if content_type is not None else existing["content_type"]
        merged_extracted_metadata = (
            extracted_metadata
            if extracted_metadata is not None
            else existing["extracted_metadata"]
        )
        merged_approval_status = approval_status if approval_status is not None else existing["approval_status"]
        merged_approval_comment = approval_comment if approval_comment is not None else existing["approval_comment"]
        merged_approved_by = approved_by if approved_by is not None else existing["approved_by"]
        merged_approved_at = approved_at if approved_at is not None else existing["approved_at"]
        merged_quality_score = quality_score if quality_score is not None else existing["quality_score"]

        if merged_approval_status in {"approved", "rejected"} and not merged_approved_at:
            merged_approved_at = now_utc()
        if merged_approval_status == "pending":
            merged_approved_at = None
            merged_approved_by = ""

        cur.execute(
            f"""
            UPDATE public.{DOCUMENT_PROCESSING_TABLE}
            SET
                project_slug = %s,
                normalization_version = %s,
                raw_content = %s,
                normalized_content = %s,
                rendered_text = %s,
                structured_content = %s,
                normalization_config = %s,
                detected_language = %s,
                content_type = %s,
                extracted_metadata = %s,
                approval_status = %s,
                approval_comment = %s,
                approved_by = %s,
                approved_at = %s,
                quality_score = %s,
                updated_at = now()
            WHERE document_id = %s;
            """,
            (
                merged_project_slug,
                merged_normalization_version or DEFAULT_NORMALIZATION_VERSION,
                merged_raw_content,
                merged_normalized_content,
                merged_rendered_text,
                Json(merged_structured_content) if merged_structured_content is not None else None,
                Json(merged_normalization_config),
                merged_detected_language or "und",
                merged_content_type or "unknown",
                Json(merged_extracted_metadata),
                merged_approval_status or "pending",
                merged_approval_comment or None,
                merged_approved_by or None,
                merged_approved_at,
                merged_quality_score,
                document_id,
            ),
        )
    else:
        initial_approval_status = approval_status or "pending"
        initial_approved_at = approved_at
        if initial_approval_status in {"approved", "rejected"} and not initial_approved_at:
            initial_approved_at = now_utc()
        if initial_approval_status == "pending":
            initial_approved_at = None
            approved_by = None

        cur.execute(
            f"""
            INSERT INTO public.{DOCUMENT_PROCESSING_TABLE} (
                document_id,
                project_slug,
                normalization_version,
                raw_content,
                normalized_content,
                rendered_text,
                structured_content,
                normalization_config,
                detected_language,
                content_type,
                extracted_metadata,
                approval_status,
                approval_comment,
                approved_by,
                approved_at,
                quality_score,
                updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now());
            """,
            (
                document_id,
                project_slug,
                normalization_version or DEFAULT_NORMALIZATION_VERSION,
                raw_content,
                normalized_content,
                rendered_text,
                Json(structured_content) if structured_content is not None else None,
                Json(normalization_config or {}),
                detected_language or "und",
                content_type or "unknown",
                Json(extracted_metadata or {}),
                initial_approval_status,
                approval_comment or None,
                approved_by or None,
                initial_approved_at,
                quality_score,
            ),
        )

    return get_document_processing_record(cur, document_id)


def _score_quality_observations(score_breakdown, sha256_raw, sha256_normalized,
                                normalization_hash_version):
    """Build explainable observations for the existing heuristic score."""
    if not score_breakdown["components"]:
        return [
            {
                "rule_code": "QF_CONTENT_EMPTY",
                "severity": "error",
                "score_delta": 0.0,
                "message": "La representation normalisee ne contient aucun texte exploitable.",
                "evidence": {
                    "token_count": score_breakdown["token_count"],
                    "character_count": score_breakdown["character_count"],
                },
                "sha256_raw": sha256_raw,
                "sha256_normalized": sha256_normalized,
                "normalization_hash_version": normalization_hash_version,
                "canonical_document_id": None,
            }
        ]

    definitions = {
        "QF_LENGTH_SCORE": (
            "La longueur contribue au score qualite selon la plage cible de tokens.",
            0.6,
        ),
        "QF_LEXICAL_DIVERSITY": (
            "La diversite lexicale contribue au score qualite.",
            0.35,
        ),
        "QF_CHARACTER_SIGNAL": (
            "Le ratio de caracteres alphanumeriques contribue au score qualite.",
            0.65,
        ),
    }
    observations = []
    for component in score_breakdown["components"]:
        message, warning_threshold = definitions[component["rule_code"]]
        evidence = {
            **component["evidence"],
            "component_score": component["component_score"],
            "weight": component["weight"],
        }
        observations.append(
            {
                "rule_code": component["rule_code"],
                "severity": (
                    "warning"
                    if component["component_score"] < warning_threshold
                    else "info"
                ),
                "score_delta": component["score_delta"],
                "message": message,
                "evidence": evidence,
                "sha256_raw": sha256_raw,
                "sha256_normalized": sha256_normalized,
                "normalization_hash_version": normalization_hash_version,
                "canonical_document_id": None,
            }
        )
    return observations


def find_quality_duplicate_canonical(
    cur,
    project_slug: str,
    document_id: str,
    sha256_raw: str,
    sha256_normalized: str,
    normalization_hash_version: str,
):
    """Return the stable project-local canonical and exact match type, if any."""
    cur.execute(
        f"""
        WITH matches AS (
            SELECT
                document_id,
                bool_or(sha256_raw = %s) AS raw_match,
                bool_or(
                    normalization_hash_version = %s
                    AND sha256_normalized = %s
                ) AS normalized_match
            FROM public.{QUALITY_OBSERVATION_TABLE}
            WHERE project_slug = %s
              AND document_id IS NOT NULL
              AND document_id <> %s
              AND (
                    sha256_raw = %s
                    OR (
                        normalization_hash_version = %s
                        AND sha256_normalized = %s
                    )
              )
            GROUP BY document_id
        )
        SELECT
            registry.document_id,
            COALESCE(matches.raw_match, false),
            COALESCE(matches.normalized_match, false)
        FROM public.{DOCUMENT_REGISTRY_TABLE} AS registry
        LEFT JOIN matches ON matches.document_id = registry.document_id
        WHERE registry.project_slug = %s
          AND (registry.document_id = %s OR matches.document_id IS NOT NULL)
        ORDER BY registry.created_at, registry.document_id
        LIMIT 1;
        """,
        (
            sha256_raw,
            normalization_hash_version,
            sha256_normalized,
            project_slug,
            document_id,
            sha256_raw,
            normalization_hash_version,
            sha256_normalized,
            project_slug,
            document_id,
        ),
    )
    row = cur.fetchone()
    if not row or row[0] == document_id:
        return None
    return {
        "canonical_document_id": row[0],
        "match_type": "raw" if row[1] else "normalized",
    }


def persist_quality_observations(
    cur,
    project_slug: str,
    document_id: str,
    observations,
    ruleset_version: str = QUALITY_RULESET_VERSION,
):
    """Persist one ruleset idempotently while retaining other ruleset versions."""
    rule_codes = []
    for observation in observations:
        rule_code = observation["rule_code"]
        rule_codes.append(rule_code)
        hash_version = observation["normalization_hash_version"]
        identity = "\x00".join(
            [
                project_slug,
                "document",
                document_id,
                rule_code,
                ruleset_version,
                hash_version,
            ]
        )
        observation_id = "qf_" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:40]
        evidence = bound_quality_evidence(observation.get("evidence"))
        cur.execute(
            f"""
            INSERT INTO public.{QUALITY_OBSERVATION_TABLE} (
                observation_id,
                project_slug,
                document_id,
                chunk_id,
                rule_code,
                ruleset_version,
                severity,
                score_delta,
                message,
                evidence,
                sha256_raw,
                sha256_normalized,
                normalization_hash_version,
                canonical_document_id,
                updated_at
            ) VALUES (
                %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, now()
            )
            ON CONFLICT (observation_id)
            DO UPDATE SET
                severity = EXCLUDED.severity,
                score_delta = EXCLUDED.score_delta,
                message = EXCLUDED.message,
                evidence = EXCLUDED.evidence,
                sha256_raw = EXCLUDED.sha256_raw,
                sha256_normalized = EXCLUDED.sha256_normalized,
                normalization_hash_version = EXCLUDED.normalization_hash_version,
                canonical_document_id = EXCLUDED.canonical_document_id,
                updated_at = now();
            """,
            (
                observation_id,
                project_slug,
                document_id,
                rule_code,
                ruleset_version,
                observation["severity"],
                observation.get("score_delta"),
                observation["message"][:500],
                Json(evidence),
                observation.get("sha256_raw"),
                observation.get("sha256_normalized"),
                hash_version,
                observation.get("canonical_document_id"),
            ),
        )

    cur.execute(
        f"""
        DELETE FROM public.{QUALITY_OBSERVATION_TABLE}
        WHERE project_slug = %s
          AND document_id = %s
          AND ruleset_version = %s
          AND normalization_hash_version = %s
          AND NOT (rule_code = ANY(%s));
        """,
        (
            project_slug,
            document_id,
            ruleset_version,
            observations[0]["normalization_hash_version"],
            rule_codes,
        ),
    )


def list_document_quality_observations(
    cur,
    project_slug: str,
    document_id: str,
    ruleset_version: str = QUALITY_RULESET_VERSION,
    normalization_hash_version: str = "",
):
    """Read explainable observations for one document and ruleset."""
    cur.execute(
        f"""
        SELECT
            observation_id,
            rule_code,
            ruleset_version,
            severity,
            score_delta,
            message,
            evidence,
            sha256_raw,
            sha256_normalized,
            normalization_hash_version,
            canonical_document_id,
            created_at,
            updated_at
        FROM public.{QUALITY_OBSERVATION_TABLE}
        WHERE project_slug = %s
          AND document_id = %s
          AND ruleset_version = %s
          AND normalization_hash_version = COALESCE(
              NULLIF(%s, ''),
              (
                  SELECT latest.normalization_hash_version
                  FROM public.{QUALITY_OBSERVATION_TABLE} AS latest
                  WHERE latest.project_slug = %s
                    AND latest.document_id = %s
                    AND latest.ruleset_version = %s
                  ORDER BY latest.updated_at DESC, latest.normalization_hash_version
                  LIMIT 1
              )
          )
        ORDER BY
            CASE severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
            rule_code;
        """,
        (
            project_slug,
            document_id,
            ruleset_version,
            normalization_hash_version,
            project_slug,
            document_id,
            ruleset_version,
        ),
    )
    return [
        {
            "observation_id": row[0],
            "rule_code": row[1],
            "ruleset_version": row[2],
            "severity": row[3],
            "score_delta": float(row[4]) if row[4] is not None else None,
            "message": row[5],
            "evidence": row[6] or {},
            "sha256_raw": row[7] or "",
            "sha256_normalized": row[8] or "",
            "normalization_hash_version": row[9],
            "canonical_document_id": row[10],
            "created_at": to_iso_or_none(row[11]),
            "updated_at": to_iso_or_none(row[12]),
        }
        for row in cur.fetchall()
    ]


def _quality_summary(observations):
    """Aggregate persisted observations into a review-friendly score summary."""
    positive = sum(
        item["score_delta"] or 0.0
        for item in observations
        if (item["score_delta"] or 0.0) > 0
    )
    penalties = sum(
        item["score_delta"] or 0.0
        for item in observations
        if (item["score_delta"] or 0.0) < 0
    )
    first = observations[0] if observations else {}
    return {
        "ruleset_version": QUALITY_RULESET_VERSION,
        "normalization_hash_version": first.get("normalization_hash_version", ""),
        "sha256_raw": first.get("sha256_raw", ""),
        "sha256_normalized": first.get("sha256_normalized", ""),
        "base_score": round(positive, 4),
        "penalty_total": round(penalties, 4),
        "score": round(max(0.0, min(1.0, positive + penalties)), 4),
        "observations": observations,
    }


def recalculate_document_quality_with_cursor(
    cur,
    document,
    raw_content: str,
    normalized_content: str,
    normalization_version: str,
):
    """Recalculate and persist Quality Firewall v1 in an existing transaction."""
    raw_hash = sha256_text(raw_content)
    normalized_hash = sha256_text(normalized_content)
    hash_version = quality_normalization_hash_version(normalization_version)
    score_breakdown = compute_quality_score_breakdown(normalized_content)
    observations = _score_quality_observations(
        score_breakdown,
        raw_hash,
        normalized_hash,
        hash_version,
    )
    duplicate = find_quality_duplicate_canonical(
        cur,
        document["project_slug"],
        document["uuid"],
        raw_hash,
        normalized_hash,
        hash_version,
    )
    if duplicate:
        raw_match = duplicate["match_type"] == "raw"
        observations.append(
            {
                "rule_code": (
                    "QF_EXACT_DUPLICATE_RAW"
                    if raw_match
                    else "QF_EXACT_DUPLICATE_NORMALIZED"
                ),
                "severity": "warning",
                "score_delta": QUALITY_DUPLICATE_SCORE_DELTA,
                "message": (
                    "La source brute est identique a un document canonique du projet."
                    if raw_match
                    else "La forme normalisee est identique a un document canonique du projet."
                ),
                "evidence": {
                    "match_type": duplicate["match_type"],
                    "canonical_document_id": duplicate["canonical_document_id"],
                    "hash_prefix": (raw_hash if raw_match else normalized_hash)[:12],
                },
                "sha256_raw": raw_hash,
                "sha256_normalized": normalized_hash,
                "normalization_hash_version": hash_version,
                "canonical_document_id": duplicate["canonical_document_id"],
            }
        )

    persist_quality_observations(
        cur,
        document["project_slug"],
        document["uuid"],
        observations,
    )
    persisted = list_document_quality_observations(
        cur,
        document["project_slug"],
        document["uuid"],
        normalization_hash_version=hash_version,
    )
    summary = _quality_summary(persisted)
    upsert_document_processing_record(
        cur,
        document_id=document["uuid"],
        project_slug=document["project_slug"],
        quality_score=summary["score"],
    )
    return summary


def recalculate_document_quality(
    document_id: str,
    project_slug: str = "",
):
    """Recalculate Quality Firewall v1 for one existing document."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            lock_project_corpus_mutation(cur, document["project_slug"])
            processing = get_document_processing_record(cur, document["uuid"])
            if processing and processing.get("normalized_content"):
                normalized_content = processing["normalized_content"]
                normalization_version = processing["normalization_version"]
            else:
                pipeline = run_normalization_pipeline(document["content_document"])
                normalized_content = pipeline["normalized_content"]
                normalization_version = DEFAULT_NORMALIZATION_VERSION
            return recalculate_document_quality_with_cursor(
                cur,
                document,
                document["content_document"],
                normalized_content,
                normalization_version,
            )


def list_document_review_annotations(cur, project_slug: str, document_id: str):
    """Return human review annotations for one document."""
    ensure_business_tables(cur)
    cur.execute(
        f"""
        SELECT
            annotation_id,
            document_id,
            project_slug,
            target_type,
            target_id,
            section_path,
            severity,
            status,
            note,
            created_by,
            created_at,
            updated_at
        FROM public.{DOCUMENT_REVIEW_ANNOTATION_TABLE}
        WHERE project_slug = %s
          AND document_id = %s
        ORDER BY created_at DESC, annotation_id DESC;
        """,
        (project_slug, document_id),
    )
    rows = cur.fetchall()
    return [
        {
            "annotation_id": row[0],
            "document_id": row[1],
            "project_slug": row[2],
            "target_type": row[3],
            "target_id": row[4] or "",
            "section_path": row[5] or "",
            "severity": row[6],
            "status": row[7],
            "note": row[8] or "",
            "created_by": row[9] or "",
            "created_at": to_iso_or_none(row[10]),
            "updated_at": to_iso_or_none(row[11]),
        }
        for row in rows
    ]


def list_document_section_exclusions(cur, project_slug: str, document_id: str):
    """Return excluded sections for one document."""
    ensure_business_tables(cur)
    cur.execute(
        f"""
        SELECT
            exclusion_id,
            document_id,
            project_slug,
            section_path,
            section_title,
            reason,
            excluded_by,
            created_at,
            updated_at
        FROM public.{DOCUMENT_SECTION_EXCLUSION_TABLE}
        WHERE project_slug = %s
          AND document_id = %s
        ORDER BY section_path;
        """,
        (project_slug, document_id),
    )
    rows = cur.fetchall()
    return [
        {
            "exclusion_id": row[0],
            "document_id": row[1],
            "project_slug": row[2],
            "section_path": row[3],
            "section_title": row[4] or "",
            "reason": row[5] or "",
            "excluded_by": row[6] or "",
            "created_at": to_iso_or_none(row[7]),
            "updated_at": to_iso_or_none(row[8]),
        }
        for row in rows
    ]


def load_project_excluded_section_paths(cur, project_slug: str):
    """Return excluded section paths indexed by document id for a project."""
    ensure_business_tables(cur)
    cur.execute(
        f"""
        SELECT document_id, section_path
        FROM public.{DOCUMENT_SECTION_EXCLUSION_TABLE}
        WHERE project_slug = %s;
        """,
        (project_slug,),
    )
    excluded_paths = {}
    for document_id, section_path in cur.fetchall():
        excluded_paths.setdefault(document_id, set()).add(section_path)
    return excluded_paths


def list_document_review_items(project_slug: str = "", limit: int = 500):
    """Return documents available for human review."""
    requested_slug = (project_slug or "").strip()
    safe_limit = max(1, min(1000, int(limit or 500)))

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            if requested_slug:
                project_slugs = [find_project_by_slug(cur, requested_slug)["slug"]]
            else:
                project_slugs = list_project_slugs(cur)

            review_items = []
            for slug in project_slugs:
                project = find_project_by_slug(cur, slug)
                shard_table = f"{slug}_shard"
                chunk_table = f"{slug}_chunk"
                if not table_exists(cur, shard_table):
                    continue

                chunk_join = (
                    sql.SQL("LEFT JOIN {} AS c ON c.shard_id = s.uuid").format(
                        sql.Identifier("public", chunk_table)
                    )
                    if table_exists(cur, chunk_table)
                    else sql.SQL("")
                )
                chunk_count_expression = (
                    sql.SQL("COUNT(DISTINCT c.uuid)::int")
                    if table_exists(cur, chunk_table)
                    else sql.SQL("0")
                )

                cur.execute(
                    sql.SQL(
                        f"""
                        SELECT
                            s.uuid,
                            s.title_document,
                            s.source_document,
                            s.url_document,
                            s.autor_document,
                            s.content_document,
                            p.approval_status,
                            p.quality_score,
                            p.normalization_version,
                            p.updated_at,
                            {{}},
                            COUNT(DISTINCT a.annotation_id)::int AS annotation_count,
                            COUNT(DISTINCT e.exclusion_id)::int AS exclusion_count
                        FROM {{}} AS s
                        {{}}
                        LEFT JOIN public.{DOCUMENT_PROCESSING_TABLE} AS p
                            ON p.document_id = s.uuid
                           AND p.project_slug = %s
                        LEFT JOIN public.{DOCUMENT_REVIEW_ANNOTATION_TABLE} AS a
                            ON a.document_id = s.uuid
                           AND a.project_slug = %s
                        LEFT JOIN public.{DOCUMENT_SECTION_EXCLUSION_TABLE} AS e
                            ON e.document_id = s.uuid
                           AND e.project_slug = %s
                        GROUP BY
                            s.uuid,
                            s.title_document,
                            s.source_document,
                            s.url_document,
                            s.autor_document,
                            s.content_document,
                            p.approval_status,
                            p.quality_score,
                            p.normalization_version,
                            p.updated_at
                        ORDER BY s.uuid DESC;
                        """
                    ).format(
                        chunk_count_expression,
                        sql.Identifier("public", shard_table),
                        chunk_join,
                    ),
                    (slug, slug, slug),
                )

                for row in cur.fetchall():
                    review_items.append(
                        {
                            "document_id": row[0],
                            "project_slug": slug,
                            "project_name": project["name"],
                            "title_document": row[1] or "",
                            "source_document": row[2] or "",
                            "url_document": row[3] or "",
                            "autor_document": row[4] or "",
                            "content_preview": shorten_text(row[5] or "", 180),
                            "approval_status": row[6] or "pending",
                            "quality_score": float(row[7]) if row[7] is not None else compute_quality_score(row[5] or ""),
                            "normalization_version": row[8] or "",
                            "processing_updated_at": to_iso_or_none(row[9]),
                            "chunk_count": int(row[10] or 0),
                            "annotation_count": int(row[11] or 0),
                            "exclusion_count": int(row[12] or 0),
                        }
                    )

            review_items.sort(key=lambda item: (item["project_slug"], item["document_id"]), reverse=True)
            return review_items[:safe_limit]


def get_document_review_chunks(cur, project_slug: str, document_id: str, excluded_section_paths):
    """Return all chunks for one document, including excluded ones."""
    chunk_table = f"{project_slug}_chunk"
    if not table_exists(cur, chunk_table):
        return []

    cur.execute(
        sql.SQL(
            f"""
            SELECT
                c.uuid,
                c.shard_id,
                c.source_document,
                c.url_document,
                c.title_document,
                c.content_document,
                c.autor_document,
                m.section_title,
                m.section_path,
                m.previous_document_id,
                m.previous_chunk_id,
                m.next_chunk_id,
                m.quality_score,
                m.chunk_type,
                m.chunking_method,
                m.llm_config_id,
                m.llm_profile_type,
                m.llm_audit_session_id,
                m.metadata,
                m.summary_short,
                m.document_position_ratio,
                m.zone_type,
                m.strict_zone
            FROM {{}} AS c
            LEFT JOIN public.{CHUNK_METADATA_TABLE} AS m
                ON m.chunk_id = c.uuid
               AND m.project_slug = %s
            WHERE c.shard_id = %s
            ORDER BY c.shard_id,
                     m.document_position_ratio NULLS LAST,
                     c.uuid;
            """
        ).format(sql.Identifier("public", chunk_table)),
        (project_slug, document_id),
    )

    chunks = []
    for row in cur.fetchall():
        section_title = row[7] or row[4] or "Section"
        section_path = row[8] or section_title
        content_document = row[5] or ""
        chunks.append(
            {
                "uuid": row[0],
                "shard_id": row[1] or "",
                "source_document": row[2] or "",
                "url_document": row[3] or "",
                "title_document": row[4] or "",
                "content_document": content_document,
                "content_preview": shorten_text(content_document, 220),
                "autor_document": row[6] or "",
                "quality_score": float(row[12]) if row[12] is not None else compute_quality_score(content_document),
                "excluded": section_path in excluded_section_paths,
                "metadata": {
                    "section_title": section_title,
                    "section_path": section_path,
                    "previous_document_id": row[9],
                    "previous_chunk_id": row[10],
                    "next_chunk_id": row[11],
                    "chunk_type": row[13] or "markdown",
                    "chunking_method": row[14] or "deterministic",
                    "llm_config_id": row[15] or "",
                    "llm_profile_type": row[16] or "",
                    "llm_audit_session_id": row[17] or "",
                    "extra": row[18] or {},
                    "summary_short": row[19] or build_summary_short(content_document),
                    "document_position_ratio": float(row[20]) if row[20] is not None else 0.0,
                    "zone_type": row[21] or "text",
                    "strict_zone": bool(row[22]),
                },
            }
        )
    return chunks


def get_document_review_payload(project_slug: str, document_id: str):
    """Return source, normalized content, chunks, and review metadata."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            project = find_project_by_slug(cur, document["project_slug"])
            processing = get_document_processing_record(cur, document["uuid"])

            fallback_pipeline = run_normalization_pipeline(document["content_document"])
            normalized_content = fallback_pipeline["normalized_content"]
            if processing:
                if not processing.get("raw_content"):
                    processing["raw_content"] = document["content_document"]
                if not processing.get("normalized_content"):
                    processing["normalized_content"] = normalized_content
                if not processing.get("rendered_text"):
                    processing["rendered_text"] = fallback_pipeline["rendered_text"]
                if not processing.get("structured_content"):
                    processing["structured_content"] = build_structured_content(
                        processing["normalized_content"],
                        heading_max_level=6,
                        extracted_metadata=fallback_pipeline["extracted_metadata"],
                    )
                if not processing.get("normalization_config"):
                    processing["normalization_config"] = fallback_pipeline["normalization_config"]
            else:
                processing = {
                    "document_id": document["uuid"],
                    "project_slug": document["project_slug"],
                    "normalization_version": DEFAULT_NORMALIZATION_VERSION,
                    "raw_content": document["content_document"],
                    "normalized_content": normalized_content,
                    "rendered_text": fallback_pipeline["rendered_text"],
                    "structured_content": build_structured_content(
                        normalized_content,
                        heading_max_level=6,
                        extracted_metadata=fallback_pipeline["extracted_metadata"],
                    ),
                    "normalization_config": fallback_pipeline["normalization_config"],
                    "detected_language": fallback_pipeline["detected_language"],
                    "content_type": fallback_pipeline["content_type"],
                    "extracted_metadata": fallback_pipeline["extracted_metadata"],
                    "approval_status": "pending",
                    "approval_comment": "",
                    "approved_by": "",
                    "approved_at": None,
                    "quality_score": compute_quality_score(normalized_content),
                    "created_at": None,
                    "updated_at": None,
                }

            exclusions = list_document_section_exclusions(cur, document["project_slug"], document["uuid"])
            excluded_section_paths = {item["section_path"] for item in exclusions}
            chunks = get_document_review_chunks(
                cur,
                document["project_slug"],
                document["uuid"],
                excluded_section_paths,
            )
            annotations = list_document_review_annotations(cur, document["project_slug"], document["uuid"])
            quality_observations = list_document_quality_observations(
                cur,
                document["project_slug"],
                document["uuid"],
                normalization_hash_version=quality_normalization_hash_version(
                    processing["normalization_version"]
                ),
            )

    quality_firewall = _quality_summary(quality_observations)

    sections = processing.get("structured_content", {}).get("sections", [])
    chunk_metadata = [
        {
            "chunk_id": chunk["uuid"],
            "quality_score": chunk["quality_score"],
            "excluded": chunk["excluded"],
            **chunk["metadata"],
        }
        for chunk in chunks
    ]
    section_options = []
    seen_section_paths = set()
    for section in sections:
        section_path = section.get("section_path") or section.get("section_title") or ""
        if not section_path or section_path in seen_section_paths:
            continue
        seen_section_paths.add(section_path)
        section_options.append(
            {
                "section_path": section_path,
                "section_title": section.get("section_title") or section_path,
            }
        )
    for chunk in chunks:
        section_path = chunk["metadata"].get("section_path") or ""
        if not section_path or section_path in seen_section_paths:
            continue
        seen_section_paths.add(section_path)
        section_options.append(
            {
                "section_path": section_path,
                "section_title": chunk["metadata"].get("section_title") or section_path,
            }
        )
    metadata = {
        "document": {
            key: value
            for key, value in document.items()
            if key not in {"content_document"}
        },
        "processing": {
            key: value
            for key, value in processing.items()
            if key not in {"normalized_content", "rendered_text", "structured_content"}
        },
        "chunk_metadata": chunk_metadata,
        "annotations": annotations,
        "exclusions": exclusions,
        "quality_firewall": quality_firewall,
    }

    return {
        "project": project,
        "document": document,
        "processing": processing,
        "sections": sections,
        "section_options": section_options,
        "chunks": chunks,
        "annotations": annotations,
        "exclusions": exclusions,
        "quality_firewall": quality_firewall,
        "metadata": metadata,
    }


def add_document_review_annotation(project_slug: str, document_id: str, payload, reviewer: str = ""):
    """Create a human review anomaly annotation."""
    target_type = (payload.get("target_type") or "document").strip().lower()
    if target_type not in {"document", "section", "chunk"}:
        raise ValueError("Le champ 'target_type' doit etre: document, section ou chunk.")

    severity = (payload.get("severity") or "medium").strip().lower()
    if severity not in {"low", "medium", "high"}:
        raise ValueError("Le champ 'severity' doit etre: low, medium ou high.")

    status = (payload.get("status") or "open").strip().lower()
    if status not in {"open", "resolved"}:
        raise ValueError("Le champ 'status' doit etre: open ou resolved.")

    note = (payload.get("note") or "").strip()
    if not note:
        raise ValueError("Le commentaire d'anomalie est obligatoire.")

    created_by = (payload.get("created_by") or reviewer or "").strip()
    target_id = (payload.get("target_id") or "").strip()
    section_path = (payload.get("section_path") or "").strip()
    annotation_id = f"ann_{uuid4().hex}"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            cur.execute(
                f"""
                INSERT INTO public.{DOCUMENT_REVIEW_ANNOTATION_TABLE} (
                    annotation_id,
                    document_id,
                    project_slug,
                    target_type,
                    target_id,
                    section_path,
                    severity,
                    status,
                    note,
                    created_by,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now());
                """,
                (
                    annotation_id,
                    document["uuid"],
                    document["project_slug"],
                    target_type,
                    target_id or None,
                    section_path or None,
                    severity,
                    status,
                    note,
                    created_by or None,
                ),
            )

    return annotation_id


def delete_document_review_annotation(project_slug: str, document_id: str, annotation_id: str):
    """Delete one review annotation."""
    target_annotation_id = (annotation_id or "").strip()
    if not target_annotation_id:
        raise ValueError("Identifiant d'annotation obligatoire.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            cur.execute(
                f"""
                DELETE FROM public.{DOCUMENT_REVIEW_ANNOTATION_TABLE}
                WHERE annotation_id = %s
                  AND document_id = %s
                  AND project_slug = %s;
                """,
                (target_annotation_id, document["uuid"], document["project_slug"]),
            )
            if cur.rowcount == 0:
                raise ValueError("Annotation introuvable.")


def add_document_section_exclusion(project_slug: str, document_id: str, payload, reviewer: str = ""):
    """Create or update a section exclusion for one document."""
    section_path = (payload.get("section_path") or "").strip()
    if not section_path:
        raise ValueError("La section a exclure est obligatoire.")

    section_title = (payload.get("section_title") or section_path.split(" > ")[-1] or "").strip()
    reason = (payload.get("reason") or "").strip()
    excluded_by = (payload.get("excluded_by") or reviewer or "").strip()
    exclusion_id = f"exc_{uuid4().hex}"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            cur.execute(
                f"""
                INSERT INTO public.{DOCUMENT_SECTION_EXCLUSION_TABLE} (
                    exclusion_id,
                    document_id,
                    project_slug,
                    section_path,
                    section_title,
                    reason,
                    excluded_by,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (document_id, project_slug, section_path)
                DO UPDATE SET
                    section_title = EXCLUDED.section_title,
                    reason = EXCLUDED.reason,
                    excluded_by = EXCLUDED.excluded_by,
                    updated_at = now()
                RETURNING exclusion_id;
                """,
                (
                    exclusion_id,
                    document["uuid"],
                    document["project_slug"],
                    section_path,
                    section_title or None,
                    reason or None,
                    excluded_by or None,
                ),
            )
            saved_exclusion_id = cur.fetchone()[0]

    return saved_exclusion_id


def delete_document_section_exclusion(project_slug: str, document_id: str, exclusion_id: str):
    """Delete one section exclusion."""
    target_exclusion_id = (exclusion_id or "").strip()
    if not target_exclusion_id:
        raise ValueError("Identifiant d'exclusion obligatoire.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            cur.execute(
                f"""
                DELETE FROM public.{DOCUMENT_SECTION_EXCLUSION_TABLE}
                WHERE exclusion_id = %s
                  AND document_id = %s
                  AND project_slug = %s;
                """,
                (target_exclusion_id, document["uuid"], document["project_slug"]),
            )
            if cur.rowcount == 0:
                raise ValueError("Exclusion introuvable.")


def collect_project_chunks(cur, project_slug: str, quality_min: float = 0.0, include_excluded: bool = False):
    """Run collect project chunks."""
    _ = find_project_by_slug(cur, project_slug)
    ensure_business_tables(cur)
    chunk_table = f"{project_slug}_chunk"
    if not table_exists(cur, chunk_table):
        raise ValueError(f"La table '{chunk_table}' est introuvable.")
    excluded_paths_by_document = (
        {}
        if include_excluded
        else load_project_excluded_section_paths(cur, project_slug)
    )

    cur.execute(
        sql.SQL(
            f"""
            SELECT
                c.uuid,
                c.shard_id,
                c.source_document,
                c.url_document,
                c.title_document,
                c.content_document,
                c.autor_document,
                m.section_title,
                m.section_path,
                m.previous_document_id,
                m.previous_chunk_id,
                m.next_chunk_id,
                m.quality_score,
                p.approval_status,
                m.chunk_type,
                m.chunking_method,
                m.llm_config_id,
                m.llm_profile_type,
                m.summary_short,
                m.document_position_ratio,
                m.zone_type,
                m.strict_zone
            FROM {{}} c
            LEFT JOIN public.{CHUNK_METADATA_TABLE} m
                ON m.chunk_id = c.uuid
               AND m.project_slug = %s
            LEFT JOIN public.{DOCUMENT_PROCESSING_TABLE} p
                ON p.document_id = c.shard_id
               AND p.project_slug = %s
            ORDER BY c.shard_id,
                     m.document_position_ratio NULLS LAST,
                     c.uuid;
            """
        ).format(sql.Identifier("public", chunk_table)),
        (project_slug, project_slug),
    )
    rows = cur.fetchall()

    chunks = []
    for row in rows:
        chunk_id = row[0]
        shard_id = row[1] or ""
        source_document = row[2] or ""
        url_document = row[3] or ""
        title_document = row[4] or ""
        content_document = row[5] or ""
        autor_document = row[6] or ""
        section_title = row[7] or title_document or "Section"
        section_path = row[8] or section_title
        previous_document_id = row[9]
        previous_chunk_id = row[10]
        next_chunk_id = row[11]
        quality_score = float(row[12]) if row[12] is not None else compute_quality_score(content_document)
        approval_status = row[13] or "pending"
        chunk_type = row[14] or "markdown"
        chunking_method = row[15] or "deterministic"
        llm_config_id = row[16] or ""
        llm_profile_type = row[17] or ""
        summary_short = row[18] or build_summary_short(content_document)
        document_position_ratio = float(row[19]) if row[19] is not None else 0.0
        zone_type = row[20] or "text"
        strict_zone = bool(row[21])
        is_excluded = section_path in excluded_paths_by_document.get(shard_id, set())

        if row[12] is None:
            cur.execute(
                f"""
                INSERT INTO public.{CHUNK_METADATA_TABLE} (
                    chunk_id,
                    project_slug,
                    shard_id,
                    document_id,
                    section_title,
                    section_path,
                    previous_document_id,
                    previous_chunk_id,
                    next_chunk_id,
                    quality_score,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (chunk_id)
                DO UPDATE SET
                    quality_score = EXCLUDED.quality_score,
                    updated_at = now();
                """,
                (
                    chunk_id,
                    project_slug,
                    shard_id,
                    shard_id,
                    section_title,
                    section_path,
                    previous_document_id,
                    previous_chunk_id,
                    next_chunk_id,
                    quality_score,
                ),
            )

        if is_excluded and not include_excluded:
            continue
        if quality_score < quality_min:
            continue

        chunks.append(
            {
                "uuid": chunk_id,
                "shard_id": shard_id,
                "source_document": source_document,
                "url_document": url_document,
                "title_document": title_document,
                "content_document": content_document,
                "autor_document": autor_document,
                "quality_score": quality_score,
                "approval_status": approval_status,
                "excluded": is_excluded,
                "metadata": {
                    "section_title": section_title,
                    "section_path": section_path,
                    "previous_document_id": previous_document_id,
                    "previous_chunk_id": previous_chunk_id,
                    "next_chunk_id": next_chunk_id,
                    "chunk_type": chunk_type,
                    "chunking_method": chunking_method,
                    "llm_config_id": llm_config_id,
                    "llm_profile_type": llm_profile_type,
                    "summary_short": summary_short,
                    "document_position_ratio": document_position_ratio,
                    "zone_type": zone_type,
                    "strict_zone": strict_zone,
                },
            }
        )

    return chunks


def import_documents_for_project(project_slug: str, documents):
    """Import a batch of source documents into a project's shard table.

    Each valid input document is inserted as a shard row and registered in
    `document_registry` for fast `document_id -> project_slug` resolution.
    """
    if not isinstance(documents, list) or not documents:
        raise ValueError("Le payload doit contenir une liste 'documents' non vide.")

    imported_documents = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            table_names = ensure_project_tables_exist(cur, project_slug)
            ensure_business_tables(cur)
            shard_table = table_names["shard_table"]
            lock_project_corpus_mutation(cur, project_slug)

            for index, document_payload in enumerate(documents):
                if not isinstance(document_payload, dict):
                    raise ValueError(f"Document a l'index {index} invalide: objet JSON attendu.")

                source_document = (document_payload.get("source_document") or "").strip()
                url_document = (document_payload.get("url_document") or "").strip()
                title_document = (document_payload.get("title_document") or "").strip()
                content_document = (
                    document_payload.get("content_document")
                    or document_payload.get("content")
                    or ""
                ).strip()
                autor_document = (document_payload.get("autor_document") or "").strip()

                if not content_document:
                    raise ValueError(
                        f"Le document a l'index {index} est invalide: 'content_document' est obligatoire."
                    )

                shard_uuid = str(uuid4())
                cur.execute(
                    sql.SQL(
                        """
                        INSERT INTO {} (
                            uuid,
                            project_id,
                            source_document,
                            url_document,
                            title_document,
                            content_document,
                            autor_document
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                        """
                    ).format(sql.Identifier("public", shard_table)),
                    (
                        shard_uuid,
                        project["uuid"],
                        source_document or None,
                        url_document or None,
                        title_document or None,
                        content_document,
                        autor_document or None,
                    ),
                )
                upsert_document_registry_record(cur, shard_uuid, project_slug)

                pipeline = run_normalization_pipeline(content_document)
                initial_quality_score = compute_quality_score(
                    pipeline["normalized_content"]
                )
                _ = upsert_document_processing_record(
                    cur,
                    document_id=shard_uuid,
                    project_slug=project_slug,
                    raw_content=content_document,
                    quality_score=initial_quality_score,
                    approval_status="pending",
                )
                quality = recalculate_document_quality_with_cursor(
                    cur,
                    {
                        "uuid": shard_uuid,
                        "project_slug": project_slug,
                    },
                    content_document,
                    pipeline["normalized_content"],
                    DEFAULT_NORMALIZATION_VERSION,
                )

                imported_documents.append(
                    {
                        "document_id": shard_uuid,
                        "title_document": title_document,
                        "source_document": source_document,
                        "quality_score": quality["score"],
                        "sha256_raw": quality["sha256_raw"],
                        "sha256_normalized": quality["sha256_normalized"],
                    }
                )

    return {
        "project_slug": project_slug,
        "imported_count": len(imported_documents),
        "documents": imported_documents,
    }


def normalize_document_by_id(
    document_id: str,
    project_slug: str = "",
    normalization_version: str = "",
    normalization_options=None,
):
    """Normalize one document and persist its processing snapshot.

    The function computes normalized/structured forms plus a quality score, then
    upserts the corresponding record in `document_processing`.
    """
    version = (normalization_version or DEFAULT_NORMALIZATION_VERSION).strip() or DEFAULT_NORMALIZATION_VERSION
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            lock_project_corpus_mutation(cur, document["project_slug"])
            pipeline = run_normalization_pipeline(
                document["content_document"],
                normalization_options,
            )
            normalized_content = pipeline["normalized_content"]
            structured_content = build_structured_content(
                normalized_content,
                heading_max_level=pipeline["normalization_config"]["heading_max_level"],
                extracted_metadata=pipeline["extracted_metadata"],
            )
            quality_score = compute_quality_score(normalized_content)

            processing = upsert_document_processing_record(
                cur,
                document_id=document["uuid"],
                project_slug=document["project_slug"],
                normalization_version=version,
                raw_content=pipeline["raw_content"],
                normalized_content=normalized_content,
                rendered_text=pipeline["rendered_text"],
                structured_content=structured_content,
                normalization_config=pipeline["normalization_config"],
                detected_language=pipeline["detected_language"],
                content_type=pipeline["content_type"],
                extracted_metadata=pipeline["extracted_metadata"],
                quality_score=quality_score,
            )
            quality = recalculate_document_quality_with_cursor(
                cur,
                document,
                document["content_document"],
                normalized_content,
                version,
            )

    return {
        "document_id": document["uuid"],
        "project_slug": document["project_slug"],
        "normalization_version": version,
        "quality_score": quality["score"],
        "quality": quality,
        "raw_content": pipeline["raw_content"],
        "normalized_content": normalized_content,
        "rendered_text": pipeline["rendered_text"],
        "structured_content": processing["structured_content"],
        "normalization_config": processing["normalization_config"],
        "detected_language": processing["detected_language"],
        "content_type": processing["content_type"],
        "extracted_metadata": processing["extracted_metadata"],
        "approval_status": processing["approval_status"],
    }


def chunk_project_for_api(project_slug: str, payload):
    """Run chunk generation for a project and return API-friendly output.

    Chunk options are parsed and validated before invoking the core chunking
    service.
    """
    options = parse_chunk_options(payload)
    generated_items = chunkify_project_shards(project_slug, options)
    return {
        "project_slug": project_slug,
        "generated_chunks": len(generated_items),
        "items": generated_items,
        "options": options,
    }


def build_dataset_for_project(project_slug: str, payload):
    """Build a dataset snapshot from filtered project chunks.

    Applies quality and approval filters, enforces a limit, estimates token
    volume, and stores build metadata in `dataset_build`.
    """
    quality_min = parse_float_field(payload.get("quality_min"), "quality_min", default=0.0)
    quality_min = max(0.0, min(1.0, quality_min))
    limit = parse_int_field(payload.get("limit"), "limit", default=2000)
    limit = max(1, limit)
    approved_only = parse_bool_field(payload.get("approved_only"), default=False)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            _ = find_project_by_slug(cur, project_slug)
            chunks = collect_project_chunks(cur, project_slug, quality_min=quality_min)
            total_after_quality_filter = len(chunks)

            if approved_only:
                chunks = [chunk for chunk in chunks if chunk["approval_status"] == "approved"]
            total_after_approval_filter = len(chunks)

            selected_chunks = chunks[:limit]
            total_tokens_estimated = sum(
                estimate_tokens(chunk["content_document"], DEFAULT_CHUNK_OPTIONS)
                for chunk in selected_chunks
            )
            avg_quality_score = (
                round(
                    sum(chunk["quality_score"] for chunk in selected_chunks) / float(len(selected_chunks)),
                    4,
                )
                if selected_chunks
                else 0.0
            )

            stats = {
                "selected_chunks": len(selected_chunks),
                "total_after_quality_filter": total_after_quality_filter,
                "total_after_approval_filter": total_after_approval_filter,
                "avg_quality_score": avg_quality_score,
                "estimated_tokens": total_tokens_estimated,
            }
            options = {
                "quality_min": quality_min,
                "approved_only": approved_only,
                "limit": limit,
            }
            items_preview = [
                {
                    "chunk_id": chunk["uuid"],
                    "document_id": chunk["shard_id"],
                    "quality_score": chunk["quality_score"],
                }
                for chunk in selected_chunks[:25]
            ]
            build_id = (
                f"ds_{project_slug}_{now_utc().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
            )

            cur.execute(
                f"""
                INSERT INTO public.{DATASET_BUILD_TABLE} (
                    build_id,
                    project_slug,
                    status,
                    quality_min,
                    options,
                    stats,
                    items_preview,
                    completed_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now());
                """,
                (
                    build_id,
                    project_slug,
                    "completed",
                    quality_min,
                    Json(options),
                    Json(stats),
                    Json(items_preview),
                ),
            )

    return {
        "build_id": build_id,
        "project_slug": project_slug,
        "status": "completed",
        "options": options,
        "stats": stats,
        "items_preview": items_preview,
    }


def get_dataset_build_by_id(build_id: str):
    """Return dataset build by id."""
    build_identifier = (build_id or "").strip()
    if not build_identifier:
        raise ValueError("Le champ 'build_id' est obligatoire.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            cur.execute(
                f"""
                SELECT
                    build_id,
                    project_slug,
                    status,
                    quality_min,
                    options,
                    stats,
                    items_preview,
                    created_at,
                    updated_at,
                    completed_at
                FROM public.{DATASET_BUILD_TABLE}
                WHERE build_id = %s;
                """,
                (build_identifier,),
            )
            row = cur.fetchone()

    if not row:
        raise ValueError(f"Dataset build introuvable: '{build_identifier}'.")

    return {
        "build_id": row[0],
        "project_slug": row[1],
        "status": row[2],
        "quality_min": float(row[3]) if row[3] is not None else 0.0,
        "options": row[4] or {},
        "stats": row[5] or {},
        "items_preview": row[6] or [],
        "created_at": to_iso_or_none(row[7]),
        "updated_at": to_iso_or_none(row[8]),
        "completed_at": to_iso_or_none(row[9]),
    }


def list_chunks_for_api(project_slug: str, quality_min: float, limit: int, offset: int):
    """Return chunks for api."""
    if not project_slug:
        raise ValueError("Le parametre query 'project' est obligatoire.")

    safe_quality_min = max(0.0, min(1.0, quality_min))
    safe_limit = max(1, min(10000, limit))
    safe_offset = max(0, offset)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            chunks = collect_project_chunks(cur, project_slug, quality_min=safe_quality_min)

    total = len(chunks)
    paginated_chunks = chunks[safe_offset : safe_offset + safe_limit]
    return {
        "project_slug": project_slug,
        "quality_min": safe_quality_min,
        "offset": safe_offset,
        "limit": safe_limit,
        "total": total,
        "count": len(paginated_chunks),
        "items": paginated_chunks,
    }


def get_document_lineage(document_id: str, project_slug: str = ""):
    """Return document lineage with processing and chunk navigation metadata.

    Combines `document_processing` with chunk metadata links
    (`previous_*`/`next_*`) for traceability.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, project_slug)
            processing = get_document_processing_record(cur, document["uuid"])

            chunk_table = f"{document['project_slug']}_chunk"
            if table_exists(cur, chunk_table):
                cur.execute(
                    sql.SQL(
                        f"""
                        SELECT
                            c.uuid,
                            c.title_document,
                            c.content_document,
                            m.section_path,
                            m.previous_document_id,
                            m.previous_chunk_id,
                            m.next_chunk_id,
                            m.quality_score,
                            m.summary_short,
                            m.document_position_ratio,
                            m.chunk_type,
                            m.zone_type,
                            m.strict_zone
                        FROM {{}} c
                        LEFT JOIN public.{CHUNK_METADATA_TABLE} m
                            ON m.chunk_id = c.uuid
                           AND m.project_slug = %s
                        WHERE c.shard_id = %s
                        ORDER BY m.document_position_ratio NULLS LAST, c.uuid;
                        """
                    ).format(sql.Identifier("public", chunk_table)),
                    (document["project_slug"], document["uuid"]),
                )
                chunk_rows = cur.fetchall()
            else:
                chunk_rows = []

            chunks = []
            for row in chunk_rows:
                quality_score = float(row[7]) if row[7] is not None else compute_quality_score(row[2] or "")
                chunks.append(
                    {
                        "chunk_id": row[0],
                        "title_document": row[1] or "",
                        "content_preview": shorten_text(row[2] or "", 180),
                        "section_path": row[3] or "",
                        "previous_document_id": row[4],
                        "previous_chunk_id": row[5],
                        "next_chunk_id": row[6],
                        "quality_score": quality_score,
                        "summary_short": row[8] or build_summary_short(row[2] or ""),
                        "document_position_ratio": float(row[9]) if row[9] is not None else 0.0,
                        "chunk_type": row[10] or "markdown",
                        "zone_type": row[11] or "text",
                        "strict_zone": bool(row[12]),
                    }
                )

            for index, chunk in enumerate(chunks):
                if not chunk["previous_chunk_id"] and index > 0:
                    chunk["previous_chunk_id"] = chunks[index - 1]["chunk_id"]
                if not chunk["next_chunk_id"] and index < len(chunks) - 1:
                    chunk["next_chunk_id"] = chunks[index + 1]["chunk_id"]

            previous_document_id = next(
                (
                    chunk["previous_document_id"]
                    for chunk in chunks
                    if chunk.get("previous_document_id")
                ),
                None,
            )

            cur.execute(
                f"""
                SELECT DISTINCT document_id
                FROM public.{CHUNK_METADATA_TABLE}
                WHERE project_slug = %s
                  AND previous_document_id = %s
                ORDER BY document_id;
                """,
                (document["project_slug"], document["uuid"]),
            )
            next_document_ids = [row[0] for row in cur.fetchall()]

    return {
        "document": {
            "document_id": document["uuid"],
            "project_slug": document["project_slug"],
            "title_document": document["title_document"],
            "source_document": document["source_document"],
            "url_document": document["url_document"],
            "autor_document": document["autor_document"],
        },
        "processing": processing
        or {
            "approval_status": "pending",
            "quality_score": None,
            "normalization_version": None,
        },
        "lineage": {
            "previous_document_id": previous_document_id,
            "next_document_ids": next_document_ids,
            "chunk_count": len(chunks),
            "chunks": chunks,
        },
    }


def approve_document_by_id(document_id: str, payload):
    """Set approval status and reviewer metadata for a document.

    Supports `pending`, `approved`, and `rejected` states while preserving an
    existing quality score when one is already available.
    """
    requested_status = (payload.get("status") or "approved").strip().lower()
    if requested_status not in {"pending", "approved", "rejected"}:
        raise ValueError("Le champ 'status' doit etre: pending, approved ou rejected.")

    approval_comment = (payload.get("comment") or payload.get("approval_comment") or "").strip()
    approved_by = (payload.get("approved_by") or "").strip()
    preferred_project_slug = (payload.get("project_slug") or payload.get("project") or "").strip()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            document = find_document_record(cur, document_id, preferred_project_slug)
            existing_processing = get_document_processing_record(cur, document["uuid"])
            quality_score = (
                existing_processing["quality_score"]
                if existing_processing and existing_processing.get("quality_score") is not None
                else compute_quality_score(document["content_document"])
            )

            processing = upsert_document_processing_record(
                cur,
                document_id=document["uuid"],
                project_slug=document["project_slug"],
                approval_status=requested_status,
                approval_comment=approval_comment if approval_comment else None,
                approved_by=approved_by if approved_by else None,
                approved_at=now_utc() if requested_status in {"approved", "rejected"} else None,
                quality_score=quality_score,
            )

    return {
        "document_id": document["uuid"],
        "project_slug": document["project_slug"],
        "status": processing["approval_status"],
        "approval_comment": processing["approval_comment"],
        "approved_by": processing["approved_by"],
        "approved_at": processing["approved_at"],
        "quality_score": processing["quality_score"],
    }


MCP_PROTOCOL_VERSION = "2024-11-05"


def mcp_tools_catalog():
    """Return the MCP tool catalog exposed by this server.

    Each tool includes a stable name, description, and JSON input schema.
    """
    return [
        {
            "name": "floppy.import_documents",
            "description": "Importer des documents dans un projet.",
            "inputSchema": {
                "type": "object",
                "required": ["project_slug", "documents"],
                "properties": {
                    "project_slug": {"type": "string"},
                    "documents": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
            },
        },
        {
            "name": "floppy.normalize_document",
            "description": "Normaliser un document et calculer son score qualite.",
            "inputSchema": {
                "type": "object",
                "required": ["document_id"],
                "properties": {
                    "document_id": {"type": "string"},
                    "project_slug": {"type": "string"},
                    "normalization_version": {"type": "string"},
                    "normalization_options": {
                        "type": "object",
                        "properties": {
                            "enabled_stages": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "heading_max_level": {"type": "integer"},
                            "preserve_code_blocks": {"type": "boolean"},
                        },
                    },
                },
            },
        },
        {
            "name": "floppy.chunk_project",
            "description": "Generer des chunks pour un projet.",
            "inputSchema": {
                "type": "object",
                "required": ["project_slug"],
                "properties": {
                    "project_slug": {"type": "string"},
                    "chunkMaxTokens": {"type": "integer"},
                    "chunkOverlapTokens": {"type": "integer"},
                    "hardMaxTokens": {"type": "integer"},
                    "headingMaxLevel": {"type": "integer"},
                    "tokenEstimator": {"type": "string"},
                    "charsPerToken": {"type": "integer"},
                    "codeAware": {"type": "boolean"},
                    "tableAware": {"type": "boolean"},
                    "mergeSmallParagraphs": {"type": "boolean"},
                    "smallParagraphMinTokens": {"type": "integer"},
                    "strictZoneTypes": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["code", "table", "strict"],
                        },
                    },
                },
            },
        },
        {
            "name": "floppy.build_dataset",
            "description": "Construire un dataset a partir des chunks filtres.",
            "inputSchema": {
                "type": "object",
                "required": ["project_slug"],
                "properties": {
                    "project_slug": {"type": "string"},
                    "quality_min": {"type": "number"},
                    "approved_only": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "floppy.get_dataset_build",
            "description": "Consulter un dataset build.",
            "inputSchema": {
                "type": "object",
                "required": ["build_id"],
                "properties": {
                    "build_id": {"type": "string"},
                },
            },
        },
        {
            "name": "floppy.search_chunks",
            "description": "Lister les chunks avec filtre de qualite.",
            "inputSchema": {
                "type": "object",
                "required": ["project_slug"],
                "properties": {
                    "project_slug": {"type": "string"},
                    "quality_min": {"type": "number"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
            },
        },
        {
            "name": "floppy.get_document_lineage",
            "description": "Afficher la lineage d'un document (chunks, liens precedent/suivant).",
            "inputSchema": {
                "type": "object",
                "required": ["document_id"],
                "properties": {
                    "document_id": {"type": "string"},
                    "project_slug": {"type": "string"},
                },
            },
        },
        {
            "name": "floppy.approve_document",
            "description": "Approuver ou rejeter un document.",
            "inputSchema": {
                "type": "object",
                "required": ["document_id"],
                "properties": {
                    "document_id": {"type": "string"},
                    "project_slug": {"type": "string"},
                    "status": {"type": "string"},
                    "comment": {"type": "string"},
                    "approved_by": {"type": "string"},
                },
            },
        },
    ]


def execute_mcp_tool(tool_name: str, arguments):
    """Dispatch a validated MCP tool call to the corresponding service.

    Unknown tool names raise `ValueError` with a normalized, client-safe
    message.
    """
    args = arguments if isinstance(arguments, dict) else {}

    if tool_name == "floppy.import_documents":
        parsed = validate_operation_payload("import_documents", args)
        return import_documents_for_project(parsed["project_slug"], parsed["documents"])

    if tool_name == "floppy.normalize_document":
        parsed = validate_operation_payload("normalize_document", args)
        return normalize_document_by_id(
            document_id=parsed["document_id"],
            project_slug=parsed.get("project_slug", ""),
            normalization_version=parsed.get("normalization_version", ""),
            normalization_options=parsed.get("normalization_options", {}),
        )

    if tool_name == "floppy.chunk_project":
        parsed = validate_operation_payload("chunk_project", args)
        chunk_payload = {
            key: value
            for key, value in parsed.items()
            if key in DEFAULT_CHUNK_OPTIONS
        }
        return chunk_project_for_api(parsed["project_slug"], chunk_payload)

    if tool_name == "floppy.build_dataset":
        parsed = validate_operation_payload("build_dataset", args)
        return build_dataset_for_project(parsed["project_slug"], parsed)

    if tool_name == "floppy.get_dataset_build":
        parsed = validate_operation_payload("get_dataset_build", args)
        return get_dataset_build_by_id(parsed["build_id"])

    if tool_name == "floppy.search_chunks":
        parsed = validate_operation_payload("search_chunks", args)
        return list_chunks_for_api(
            project_slug=parsed["project_slug"],
            quality_min=parsed.get("quality_min", 0.0),
            limit=parsed.get("limit", 100),
            offset=parsed.get("offset", 0),
        )

    if tool_name == "floppy.get_document_lineage":
        parsed = validate_operation_payload("get_document_lineage", args)
        return get_document_lineage(
            document_id=parsed["document_id"],
            project_slug=parsed.get("project_slug", ""),
        )

    if tool_name == "floppy.approve_document":
        parsed = validate_operation_payload("approve_document", args)
        return approve_document_by_id(parsed["document_id"], parsed)

    raise ValueError(f"Outil MCP inconnu: '{tool_name}'.")


def mcp_response_payload(request_id, result=None, error=None):
    """Handle the mcp response payload request."""
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
    }
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def mcp_tool_result_payload(data, is_error: bool = False):
    """Handle the mcp tool result payload request."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(data, ensure_ascii=False),
            }
        ],
        "structuredContent": data,
        "isError": is_error,
    }


def resolve_return_url(return_to: str, default_endpoint: str, project_slug: str):
    """Resolve return url."""
    allowed_no_args = {
        "home",
        "admin_dashboard",
        "projects_shards",
        "projects_chunks",
        "projects_document_vision",
        "projects_train",
    }
    allowed_with_slug = {
        "project_shard_list",
        "project_shard_new",
        "project_train_list",
        "project_train_new",
        "project_chunk_list",
        "project_chunk_new",
        "project_chat_list",
        "project_chat_dashboard",
    }

    endpoint = return_to if return_to in (allowed_no_args | allowed_with_slug) else default_endpoint
    if endpoint in allowed_with_slug:
        return url_for(endpoint, project_slug=project_slug)
    return url_for(endpoint)
