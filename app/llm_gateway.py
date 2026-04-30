"""LLM configuration, connectivity, and audit helpers."""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

from psycopg2.extras import Json
from flask import current_app, has_app_context

from db import get_db_connection


LLM_CONFIG_TABLE = "llm_config"
LLM_AUDIT_SESSION_TABLE = "llm_audit_session"
LLM_AUDIT_EXCHANGE_TABLE = "llm_audit_exchange"
DEFAULT_LLM_PROVIDER = "ollama"
SUPPORTED_PROVIDERS = {
    "ollama",
    "litellm",
    "openai",
    "lmstudio",
    "openai_compatible",
    "custom",
}
DEFAULT_PROVIDER_URLS = {
    "ollama": "http://localhost:11434/v1/chat/completions",
    "litellm": "http://localhost:4000/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "lmstudio": "http://localhost:1234/v1/chat/completions",
    "openai_compatible": "",
    "custom": "",
}
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 360
LLM_PROFILE_TYPES = (
    "general",
    "chunk",
    "trunk",
    "ocr",
    "chat",
    "embedding",
    "webchat",
    "quiz",
    "agent",
    "custom",
)
LOGGER = logging.getLogger("floppy_ai")
LLM_CONFIG_SELECT_COLUMNS = """
    id, config_id, provider, api_url, api_key, model, timeout_seconds, enabled,
    created_at, updated_at, name, is_default, max_tokens, retries, json_mode, notes,
    profile_type
"""


def now_iso():
    """Return a UTC timestamp for display payloads."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_debug_bool(value):
    """Parse debug-like environment values without importing app services."""
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "oui", "on"}


def is_llm_debug_logging_enabled():
    """Return whether raw LLM exchanges should be written to application logs."""
    if has_app_context():
        return bool(current_app.debug or current_app.config.get("DEBUG"))
    return parse_debug_bool(os.getenv("FLASK_DEBUG")) or parse_debug_bool(os.getenv("APP_DEBUG"))


def safe_json_for_log(payload):
    """Serialize payloads for debug logs without failing on unusual values."""
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return repr(payload)


def log_llm_debug_exchange(direction, config, session_id, purpose, attempt, payload):
    """Log one raw LLM request/response payload when Flask debug is active."""
    if not is_llm_debug_logging_enabled():
        return
    LOGGER.info(
        "llm_%s session_id=%s config_id=%s provider=%s model=%s purpose=%s attempt=%s payload=%s",
        direction,
        session_id,
        config.get("config_id", ""),
        config.get("provider", ""),
        config.get("model", ""),
        purpose,
        attempt,
        safe_json_for_log(payload),
    )


def normalize_provider(raw_provider):
    """Normalize the LLM provider name."""
    provider = str(raw_provider or DEFAULT_LLM_PROVIDER).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        return "custom"
    return provider


def normalize_timeout(raw_timeout, default=90):
    """Normalize timeout seconds."""
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        timeout = default
    return min(max(timeout, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


def normalize_retries(raw_retries, default=1):
    """Normalize retry attempts."""
    try:
        retries = int(raw_retries)
    except (TypeError, ValueError):
        retries = default
    return min(max(retries, 1), 5)


def normalize_max_tokens(raw_max_tokens, default=800):
    """Normalize max token count."""
    try:
        max_tokens = int(raw_max_tokens)
    except (TypeError, ValueError):
        max_tokens = default
    return min(max(max_tokens, 64), 8000)


def default_api_url_for_provider(provider):
    """Return the default chat endpoint for a provider."""
    return DEFAULT_PROVIDER_URLS.get(normalize_provider(provider), "")


def mask_secret(value):
    """Mask a secret while keeping enough signal for operators."""
    secret = str(value or "").strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "********"
    return f"{secret[:4]}...{secret[-4:]}"


def normalize_config_id(raw_config_id):
    """Normalize a persistent LLM configuration identifier."""
    cleaned = str(raw_config_id or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", cleaned)
    cleaned = cleaned.strip("-_")
    return cleaned[:80]


def new_config_id(provider, model):
    """Build a stable-looking unique id for a new LLM configuration."""
    provider_part = normalize_provider(provider)
    model_part = normalize_config_id(model) or "model"
    return normalize_config_id(f"{provider_part}-{model_part}-{uuid4().hex[:8]}")


def normalize_config_name(raw_name, provider, model):
    """Normalize a human-readable configuration name."""
    name = str(raw_name or "").strip()
    if name:
        return name[:120]
    provider_label = normalize_provider(provider)
    model_label = str(model or "").strip() or "modele"
    return f"{provider_label} / {model_label}"[:120]


def normalize_profile_type(raw_profile_type):
    """Normalize the module profile attached to one LLM configuration."""
    profile_type = str(raw_profile_type or "general").strip().lower()
    profile_type = re.sub(r"[^a-z0-9_-]+", "_", profile_type).strip("_")
    return profile_type or "general"


def normalize_checkbox(payload, key, default=False):
    """Normalize HTML form checkboxes and boolean payload values."""
    value = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes", "oui"}


def serialize_llm_config_row(row, redact_key=False):
    """Serialize one row from public.llm_config."""
    provider = normalize_provider(row[2])
    api_url = (row[3] or default_api_url_for_provider(provider)).strip()
    model = (row[5] or "").strip()
    config = {
        "id": int(row[0]),
        "config_id": row[1],
        "provider": provider,
        "api_url": api_url,
        "api_key": (row[4] or "").strip(),
        "model": model,
        "timeout_seconds": normalize_timeout(row[6]),
        "enabled": bool(row[7]),
        "created_at": row[8].isoformat(timespec="seconds") if row[8] else None,
        "updated_at": row[9].isoformat(timespec="seconds") if row[9] else None,
        "name": row[10] or normalize_config_name("", provider, model),
        "is_default": bool(row[11]),
        "max_tokens": normalize_max_tokens(row[12]),
        "retries": normalize_retries(row[13]),
        "json_mode": bool(row[14]),
        "notes": row[15] or "",
        "profile_type": normalize_profile_type(row[16] if len(row) > 16 else "general"),
        "source": "database",
        "configured": bool(api_url and model and row[7]),
    }
    if redact_key:
        config["api_key_masked"] = mask_secret(config.get("api_key"))
        config["api_key_configured"] = bool(config.get("api_key"))
        config.pop("api_key", None)
    return config


def ensure_llm_tables(cur):
    """Create LLM configuration and audit tables when missing."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.llm_config (
            id integer PRIMARY KEY,
            provider text NOT NULL DEFAULT 'ollama',
            api_url text NOT NULL,
            api_key text,
            model text NOT NULL,
            timeout_seconds integer NOT NULL DEFAULT 90,
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS config_id text;")
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS name text;")
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS is_default boolean NOT NULL DEFAULT false;")
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS max_tokens integer NOT NULL DEFAULT 800;")
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS retries integer NOT NULL DEFAULT 1;")
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS json_mode boolean NOT NULL DEFAULT false;")
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS notes text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_config ADD COLUMN IF NOT EXISTS profile_type text NOT NULL DEFAULT 'general';")
    cur.execute(
        """
        UPDATE public.llm_config
        SET profile_type = 'general'
        WHERE profile_type IS NULL OR profile_type = '';
        """
    )
    cur.execute(
        """
        UPDATE public.llm_config
        SET config_id = CASE WHEN id = 1 THEN 'default' ELSE CONCAT('config-', id::text) END
        WHERE config_id IS NULL OR config_id = '';
        """
    )
    cur.execute(
        """
        UPDATE public.llm_config
        SET name = CONCAT(provider, ' / ', model)
        WHERE name IS NULL OR name = '';
        """
    )
    cur.execute(
        """
        UPDATE public.llm_config
        SET is_default = true
        WHERE id = (
            SELECT id FROM public.llm_config
            WHERE enabled = true
            ORDER BY enabled DESC, updated_at DESC, id ASC
            LIMIT 1
        )
        AND NOT EXISTS (SELECT 1 FROM public.llm_config WHERE is_default = true);
        """
    )
    cur.execute(
        """
        WITH chosen AS (
            SELECT id
            FROM public.llm_config
            WHERE is_default = true
            ORDER BY enabled DESC, updated_at DESC, id ASC
            LIMIT 1
        )
        UPDATE public.llm_config
        SET is_default = false
        WHERE is_default = true
          AND id <> COALESCE((SELECT id FROM chosen), -1);
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS llm_config_config_id_uq
        ON public.llm_config(config_id);
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS llm_config_one_default_uq
        ON public.llm_config((is_default))
        WHERE is_default = true;
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.llm_audit_session (
            session_id text PRIMARY KEY,
            purpose text NOT NULL,
            provider text NOT NULL,
            model text NOT NULL,
            api_url text NOT NULL,
            status text NOT NULL DEFAULT 'started',
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_message text,
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz
        );
        """
    )
    cur.execute("ALTER TABLE public.llm_audit_session ADD COLUMN IF NOT EXISTS config_id text;")
    cur.execute("ALTER TABLE public.llm_audit_session ADD COLUMN IF NOT EXISTS config_name text;")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.llm_audit_exchange (
            exchange_id text PRIMARY KEY,
            session_id text NOT NULL REFERENCES public.llm_audit_session(session_id) ON DELETE CASCADE,
            sequence_no integer NOT NULL DEFAULT 1,
            request_payload jsonb NOT NULL,
            response_payload jsonb,
            error_message text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS llm_audit_exchange_session_idx
        ON public.llm_audit_exchange(session_id, sequence_no);
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS llm_audit_session_started_idx
        ON public.llm_audit_session(started_at DESC);
        """
    )


def env_llm_config():
    """Build LLM configuration from environment variables."""
    provider = normalize_provider(
        os.getenv("LLM_PROVIDER") or os.getenv("AGENTAI_LLM_PROVIDER") or DEFAULT_LLM_PROVIDER
    )
    api_url = (
        os.getenv("LLM_API_URL")
        or os.getenv("AGENTAI_LLM_API_URL")
        or default_api_url_for_provider(provider)
    ).strip()
    model = (
        os.getenv("LLM_MODEL")
        or os.getenv("AGENTAI_LLM_MODEL")
        or ""
    ).strip()
    api_key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("AGENTAI_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    timeout_seconds = normalize_timeout(
        os.getenv("LLM_TIMEOUT") or os.getenv("AGENTAI_LLM_TIMEOUT") or "90"
    )
    max_tokens = normalize_max_tokens(
        os.getenv("LLM_MAX_TOKENS") or os.getenv("AGENTAI_LLM_MAX_TOKENS") or "800"
    )
    retries = normalize_retries(
        os.getenv("LLM_RETRIES") or os.getenv("AGENTAI_LLM_RETRIES") or "1"
    )
    json_mode = normalize_checkbox(
        {"json_mode": os.getenv("LLM_JSON_MODE") or os.getenv("AGENTAI_LLM_JSON_MODE")},
        "json_mode",
        default=False,
    )
    return {
        "config_id": "environment",
        "name": "Environment fallback",
        "provider": provider,
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "max_tokens": max_tokens,
        "retries": retries,
        "json_mode": json_mode,
        "enabled": True,
        "is_default": True,
        "notes": "",
        "profile_type": "general",
        "source": "environment",
        "configured": bool(api_url and model),
        "created_at": None,
        "updated_at": None,
    }


def get_llm_config_by_id(config_id, redact_key=False):
    """Load one persisted LLM configuration by config_id."""
    cleaned_config_id = normalize_config_id(config_id)
    if not cleaned_config_id:
        return None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                f"""
                SELECT {LLM_CONFIG_SELECT_COLUMNS}
                FROM public.llm_config
                WHERE config_id = %s;
                """,
                (cleaned_config_id,),
            )
            row = cur.fetchone()
        conn.commit()
    return serialize_llm_config_row(row, redact_key=redact_key) if row else None


def list_llm_configs(redact_key=True):
    """List all persisted LLM configurations."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                f"""
                SELECT {LLM_CONFIG_SELECT_COLUMNS}
                FROM public.llm_config
                ORDER BY is_default DESC, enabled DESC, lower(name) ASC, updated_at DESC;
                """
            )
            rows = cur.fetchall()
        conn.commit()
    return [serialize_llm_config_row(row, redact_key=redact_key) for row in rows]


def persisted_llm_config(config_id=""):
    """Load the active persisted LLM configuration from the database."""
    cleaned_config_id = normalize_config_id(config_id)
    if cleaned_config_id:
        config = get_llm_config_by_id(cleaned_config_id, redact_key=False)
        return config if config and config.get("enabled") else None

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                f"""
                SELECT {LLM_CONFIG_SELECT_COLUMNS}
                FROM public.llm_config
                WHERE enabled = true
                ORDER BY is_default DESC, updated_at DESC, id ASC
                LIMIT 1;
                """
            )
            row = cur.fetchone()
        conn.commit()

    return serialize_llm_config_row(row, redact_key=False) if row else None


def effective_llm_config(redact_key=False, config_id=""):
    """Return the active LLM configuration with environment fallback."""
    cleaned_config_id = normalize_config_id(config_id)
    db_error = None
    try:
        config = persisted_llm_config(config_id=cleaned_config_id)
    except Exception as exc:
        config = None
        db_error = str(exc)

    if not config:
        if cleaned_config_id:
            config = {
                "config_id": cleaned_config_id,
                "name": cleaned_config_id,
                "provider": "",
                "api_url": "",
                "api_key": "",
                "model": "",
                "timeout_seconds": 90,
                "max_tokens": 800,
                "retries": 1,
                "json_mode": False,
                "enabled": False,
                "is_default": False,
                "notes": "",
                "profile_type": "general",
                "source": "database",
                "configured": False,
                "created_at": None,
                "updated_at": None,
            }
        else:
            config = env_llm_config()
    else:
        env_config = env_llm_config()
        if not config.get("api_key") and env_config.get("api_key"):
            config["api_key"] = env_config["api_key"]

    config["db_error"] = db_error
    if redact_key:
        config["api_key_masked"] = mask_secret(config.get("api_key"))
        config["api_key_configured"] = bool(config.get("api_key"))
        config.pop("api_key", None)
    return config


def save_llm_config(payload):
    """Persist one admin-managed LLM configuration."""
    provider = normalize_provider(payload.get("provider"))
    api_url = (payload.get("api_url") or default_api_url_for_provider(provider)).strip()
    api_key = (payload.get("api_key") or "").strip()
    model = (payload.get("model") or "").strip()
    config_id = normalize_config_id(payload.get("config_id"))
    name = normalize_config_name(payload.get("name"), provider, model)
    timeout_seconds = normalize_timeout(payload.get("timeout_seconds"))
    max_tokens = normalize_max_tokens(payload.get("max_tokens"))
    retries = normalize_retries(payload.get("retries"))
    json_mode = normalize_checkbox(payload, "json_mode", default=False)
    enabled = normalize_checkbox(payload, "enabled", default=False)
    is_default = normalize_checkbox(payload, "is_default", default=False)
    notes = str(payload.get("notes") or "").strip()
    profile_type = normalize_profile_type(payload.get("profile_type"))

    if not api_url:
        raise ValueError("Le champ api_url est obligatoire.")
    if not model:
        raise ValueError("Le champ model est obligatoire.")
    if is_default and not enabled:
        raise ValueError("La configuration par defaut doit etre active.")
    if not config_id:
        config_id = new_config_id(provider, model)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute("SELECT COUNT(*) FROM public.llm_config;")
            total_configs = int(cur.fetchone()[0])
            if total_configs == 0:
                if not enabled:
                    raise ValueError("La premiere configuration LLM doit etre active.")
                is_default = True

            cur.execute(
                "SELECT id, api_key, is_default FROM public.llm_config WHERE config_id = %s;",
                (config_id,),
            )
            existing = cur.fetchone()
            if is_default:
                cur.execute("UPDATE public.llm_config SET is_default = false;")

            if existing:
                resolved_api_key = api_key if api_key else (existing[1] or "")
                cur.execute(
                    """
                    UPDATE public.llm_config
                    SET name = %s,
                        provider = %s,
                        api_url = %s,
                        api_key = %s,
                        model = %s,
                        timeout_seconds = %s,
                        enabled = %s,
                        is_default = %s,
                        max_tokens = %s,
                        retries = %s,
                        json_mode = %s,
                        notes = %s,
                        profile_type = %s,
                        updated_at = now()
                    WHERE config_id = %s;
                    """,
                    (
                        name,
                        provider,
                        api_url,
                        resolved_api_key,
                        model,
                        timeout_seconds,
                        enabled,
                        is_default,
                        max_tokens,
                        retries,
                        json_mode,
                        notes,
                        profile_type,
                        config_id,
                    ),
                )
            else:
                cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM public.llm_config;")
                next_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO public.llm_config
                        (
                            id, config_id, name, provider, api_url, api_key, model,
                            timeout_seconds, enabled, is_default, max_tokens,
                            retries, json_mode, notes, profile_type, updated_at
                        )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now());
                    """,
                    (
                        next_id,
                        config_id,
                        name,
                        provider,
                        api_url,
                        api_key,
                        model,
                        timeout_seconds,
                        enabled,
                        is_default,
                        max_tokens,
                        retries,
                        json_mode,
                        notes,
                        profile_type,
                    ),
                )
            cur.execute("SELECT COUNT(*) FROM public.llm_config WHERE is_default = true;")
            if int(cur.fetchone()[0]) == 0:
                cur.execute(
                    """
                    UPDATE public.llm_config
                    SET is_default = true
                    WHERE id = (
                        SELECT id FROM public.llm_config
                        WHERE enabled = true
                        ORDER BY enabled DESC, updated_at DESC, id ASC
                        LIMIT 1
                    );
                    """
                )
        conn.commit()
    return get_llm_config_by_id(config_id, redact_key=True)


def set_default_llm_config(config_id):
    """Set one persisted LLM configuration as the default."""
    cleaned_config_id = normalize_config_id(config_id)
    if not cleaned_config_id:
        raise ValueError("Configuration LLM introuvable.")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                "SELECT enabled FROM public.llm_config WHERE config_id = %s;",
                (cleaned_config_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Configuration LLM introuvable.")
            if not row[0]:
                raise ValueError("Une configuration desactivee ne peut pas devenir la configuration par defaut.")
            cur.execute("UPDATE public.llm_config SET is_default = false;")
            cur.execute(
                """
                UPDATE public.llm_config
                SET is_default = true, updated_at = now()
                WHERE config_id = %s;
                """,
                (cleaned_config_id,),
            )
        conn.commit()
    return get_llm_config_by_id(cleaned_config_id, redact_key=True)


def delete_llm_config(config_id):
    """Delete one persisted LLM configuration."""
    cleaned_config_id = normalize_config_id(config_id)
    if not cleaned_config_id:
        raise ValueError("Configuration LLM introuvable.")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute("SELECT COUNT(*) FROM public.llm_config;")
            total_configs = int(cur.fetchone()[0])
            if total_configs <= 1:
                raise ValueError("Impossible de supprimer la derniere configuration LLM.")
            cur.execute(
                "SELECT is_default FROM public.llm_config WHERE config_id = %s;",
                (cleaned_config_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Configuration LLM introuvable.")
            was_default = bool(row[0])
            cur.execute(
                "DELETE FROM public.llm_config WHERE config_id = %s;",
                (cleaned_config_id,),
            )
            if was_default:
                cur.execute(
                    """
                    UPDATE public.llm_config
                    SET is_default = true
                    WHERE id = (
                        SELECT id FROM public.llm_config
                        WHERE enabled = true
                        ORDER BY enabled DESC, updated_at DESC, id ASC
                        LIMIT 1
                    );
                    """
                )
        conn.commit()
    return True


def is_ollama_native_config(config):
    """Return whether the configured endpoint is Ollama native chat."""
    provider = normalize_provider(config.get("provider"))
    api_url = str(config.get("api_url") or "")
    parsed = urllib.parse.urlparse(api_url)
    return provider == "ollama" and parsed.path.rstrip("/") == "/api/chat"


def status_url_for_config(config):
    """Return the best lightweight status endpoint for a config."""
    api_url = str(config.get("api_url") or "").strip()
    parsed = urllib.parse.urlparse(api_url)
    if is_ollama_native_config(config):
        return urllib.parse.urlunparse(parsed._replace(path="/api/tags", query="", fragment=""))

    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")] + "/models"
    elif path.endswith("/responses"):
        path = path[: -len("/responses")] + "/models"
    elif path.endswith("/completions"):
        path = path[: -len("/completions")] + "/models"
    else:
        path = path + "/models"
    return urllib.parse.urlunparse(parsed._replace(path=path, query="", fragment=""))


def headers_for_config(config):
    """Build HTTP headers for a configured LLM provider."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    api_key = (config.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def llm_connection_status(config_id=""):
    """Return public-safe LLM connectivity status."""
    config = effective_llm_config(redact_key=True, config_id=config_id)
    payload = {
        "configured": bool(config.get("configured")),
        "connected": False,
        "config_id": config.get("config_id", ""),
        "name": config.get("name", ""),
        "provider": config.get("provider", ""),
        "model": config.get("model", ""),
        "api_url": config.get("api_url", ""),
        "source": config.get("source", ""),
        "checked_at": now_iso(),
        "error": "",
        "db_error": config.get("db_error"),
    }
    if not payload["configured"]:
        payload["status_label"] = "deconnecte"
        payload["error"] = "Configuration LLM incomplete."
        return payload

    private_config = effective_llm_config(redact_key=False, config_id=config_id)
    request = urllib.request.Request(
        status_url_for_config(private_config),
        headers=headers_for_config(private_config),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload["connected"] = 200 <= response.status < 300
            payload["status_label"] = "connecte" if payload["connected"] else "deconnecte"
            if not payload["connected"]:
                payload["error"] = f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        payload["status_label"] = "deconnecte"
        payload["error"] = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        payload["status_label"] = "deconnecte"
        payload["error"] = str(exc.reason)
    except Exception as exc:
        payload["status_label"] = "deconnecte"
        payload["error"] = str(exc)
    return payload


def build_chat_request_payload(config, messages, temperature, max_tokens=None, json_mode=False):
    """Build provider-specific chat request JSON."""
    if is_ollama_native_config(config):
        options = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = normalize_max_tokens(max_tokens)
        payload = {
            "model": config["model"],
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if json_mode:
            payload["format"] = "json"
        return payload

    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = normalize_max_tokens(max_tokens)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def build_legacy_chat_request_payload(config, messages, temperature):
    """Build chat payload without advanced options for legacy callers."""
    if is_ollama_native_config(config):
        return {
            "model": config["model"],
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
    return build_chat_request_payload(config, messages, temperature)


def execute_llm_request(config, request_payload):
    """Execute the provider request and return parsed JSON."""
    request = urllib.request.Request(
        config["api_url"],
        data=json.dumps(request_payload).encode("utf-8"),
        headers=headers_for_config(config),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=config["timeout_seconds"]) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_llm_response(config, raw_response):
    """Normalize provider responses to OpenAI chat-completions shape."""
    if not is_ollama_native_config(config):
        return raw_response

    message = raw_response.get("message", {}) if isinstance(raw_response, dict) else {}
    content = message.get("content", "")
    return {
        "choices": [
            {
                "message": {
                    "role": message.get("role", "assistant"),
                    "content": content,
                }
            }
        ],
        "provider_response": raw_response,
    }


def extract_chat_completion_content(llm_payload):
    """Extract assistant text from a normalized chat-completions payload."""
    try:
        content = llm_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Format de reponse LLM non supporte.") from exc

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        content = "\n".join(parts)

    if not isinstance(content, str) or not content.strip():
        raise ValueError("La reponse LLM est vide.")
    return content.strip()


def stringify_reasoning_part(value):
    """Return readable reasoning text from provider-specific fields."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            part = stringify_reasoning_part(item)
            if part:
                parts.append(part)
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "summary", "reasoning", "reasoning_content", "thinking"):
            part = stringify_reasoning_part(value.get(key))
            if part:
                return part
        if value.get("type") in {"reasoning", "reasoning_text", "thinking"}:
            return json.dumps(value, ensure_ascii=False, indent=2)
        return ""
    return str(value).strip()


def extract_chat_completion_reasoning(llm_payload):
    """Extract optional reasoning text from common LLM response shapes."""
    if not isinstance(llm_payload, dict):
        return ""

    candidate_paths = [
        ("reasoning",),
        ("reasoning_content",),
        ("thinking",),
        ("choices", 0, "message", "reasoning"),
        ("choices", 0, "message", "reasoning_content"),
        ("choices", 0, "message", "thinking"),
        ("choices", 0, "message", "thoughts"),
        ("provider_response", "message", "reasoning"),
        ("provider_response", "message", "reasoning_content"),
        ("provider_response", "message", "thinking"),
    ]
    for path in candidate_paths:
        current = llm_payload
        try:
            for key in path:
                current = current[key]
        except (KeyError, IndexError, TypeError):
            continue
        reasoning = stringify_reasoning_part(current)
        if reasoning:
            return reasoning[:12000]

    output_items = llm_payload.get("output")
    if isinstance(output_items, list):
        parts = []
        for item in output_items:
            if isinstance(item, dict) and item.get("type") in {"reasoning", "reasoning_text"}:
                part = stringify_reasoning_part(
                    item.get("summary") or item.get("content") or item.get("text")
                )
                if part:
                    parts.append(part)
        if parts:
            return "\n".join(parts).strip()[:12000]
    return ""


def start_llm_audit_session(config, purpose, metadata):
    """Create an audit session before any LLM exchange."""
    session_id = uuid4().hex
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                """
                INSERT INTO public.llm_audit_session
                    (session_id, purpose, provider, model, api_url, status, metadata, config_id, config_name)
                VALUES (%s, %s, %s, %s, %s, 'started', %s, %s, %s);
                """,
                (
                    session_id,
                    purpose,
                    config.get("provider", ""),
                    config.get("model", ""),
                    config.get("api_url", ""),
                    Json(metadata or {}),
                    config.get("config_id", ""),
                    config.get("name", ""),
                ),
            )
        conn.commit()
    return session_id


def record_llm_exchange(session_id, request_payload, response_payload=None, error_message=""):
    """Record one LLM exchange in the audit journal."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                """
                SELECT COALESCE(MAX(sequence_no), 0) + 1
                FROM public.llm_audit_exchange
                WHERE session_id = %s;
                """,
                (session_id,),
            )
            sequence_no = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO public.llm_audit_exchange
                    (exchange_id, session_id, sequence_no, request_payload, response_payload, error_message)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    uuid4().hex,
                    session_id,
                    sequence_no,
                    Json(request_payload),
                    Json(response_payload) if response_payload is not None else None,
                    error_message or None,
                ),
            )
        conn.commit()


def finish_llm_audit_session(session_id, status, error_message=""):
    """Close an audit session."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                """
                UPDATE public.llm_audit_session
                SET status = %s, error_message = %s, ended_at = now()
                WHERE session_id = %s;
                """,
                (status, error_message or None, session_id),
            )
        conn.commit()


def normalize_runtime_config(config):
    """Normalize an explicit LLM runtime configuration."""
    resolved = dict(config or {})
    resolved["config_id"] = normalize_config_id(resolved.get("config_id")) or "runtime"
    resolved["name"] = normalize_config_name(
        resolved.get("name"),
        resolved.get("provider"),
        resolved.get("model"),
    )
    resolved["provider"] = normalize_provider(resolved.get("provider"))
    resolved["api_url"] = str(resolved.get("api_url") or "").strip()
    resolved["api_key"] = str(resolved.get("api_key") or "").strip()
    resolved["model"] = str(resolved.get("model") or "").strip()
    resolved["timeout_seconds"] = normalize_timeout(resolved.get("timeout_seconds"))
    resolved["max_tokens"] = normalize_max_tokens(resolved.get("max_tokens"))
    resolved["retries"] = normalize_retries(resolved.get("retries"))
    resolved["json_mode"] = bool(resolved.get("json_mode"))
    resolved["profile_type"] = normalize_profile_type(resolved.get("profile_type"))
    enabled = resolved.get("enabled", True)
    if isinstance(enabled, bool):
        resolved["enabled"] = enabled
    else:
        resolved["enabled"] = str(enabled).strip().lower() not in {"0", "false", "off", "no", "non"}
    resolved["configured"] = bool(
        resolved["enabled"] and resolved["api_url"] and resolved["model"]
    )
    return resolved


def audit_request_payload(request_payload, attempt, max_attempts):
    """Return request payload augmented with audit-only retry metadata."""
    if max_attempts <= 1:
        return request_payload
    return {
        **request_payload,
        "_audit": {
            "attempt": attempt,
            "max_attempts": max_attempts,
        },
    }


def call_llm_chat_completion_with_config(
    config,
    messages,
    purpose="general",
    metadata=None,
    temperature=0.2,
    max_tokens=None,
    retries=None,
    json_mode=None,
):
    """Call a provided LLM config and persist the complete exchange in audit tables."""
    config = normalize_runtime_config(config)
    if not config.get("configured"):
        raise ValueError("Configuration LLM incomplete.")

    try:
        session_id = start_llm_audit_session(config, purpose, metadata or {})
    except Exception as exc:
        raise ValueError(
            "Journal d'audit LLM inaccessible; appel LLM annule pour eviter un echange non trace."
        ) from exc

    resolved_max_tokens = normalize_max_tokens(
        max_tokens if max_tokens is not None else config.get("max_tokens")
    )
    resolved_retries = normalize_retries(
        retries if retries is not None else config.get("retries")
    )
    resolved_json_mode = bool(config.get("json_mode")) if json_mode is None else bool(json_mode)

    request_payload = build_chat_request_payload(
        config,
        messages,
        temperature,
        max_tokens=resolved_max_tokens,
        json_mode=resolved_json_mode,
    )
    max_attempts = normalize_retries(resolved_retries)
    last_error_message = ""
    for attempt in range(1, max_attempts + 1):
        stored_request_payload = audit_request_payload(request_payload, attempt, max_attempts)
        try:
            log_llm_debug_exchange(
                "request",
                config,
                session_id,
                purpose,
                attempt,
                stored_request_payload,
            )
            raw_response = execute_llm_request(config, request_payload)
            log_llm_debug_exchange(
                "response",
                config,
                session_id,
                purpose,
                attempt,
                raw_response,
            )
            record_llm_exchange(session_id, stored_request_payload, raw_response)
            finish_llm_audit_session(session_id, "success")
            normalized = normalize_llm_response(config, raw_response)
            normalized["_audit_session_id"] = session_id
            normalized["_attempt_count"] = attempt
            return normalized
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error_message = f"Erreur LLM HTTP {exc.code}: {detail[:500]}"
            log_llm_debug_exchange(
                "error",
                config,
                session_id,
                purpose,
                attempt,
                {"error": last_error_message},
            )
            record_llm_exchange(
                session_id,
                stored_request_payload,
                error_message=last_error_message,
            )
            if attempt == max_attempts:
                finish_llm_audit_session(session_id, "error", last_error_message)
                raise ValueError(last_error_message) from exc
        except urllib.error.URLError as exc:
            last_error_message = f"Endpoint LLM inaccessible: {exc.reason}"
            log_llm_debug_exchange(
                "error",
                config,
                session_id,
                purpose,
                attempt,
                {"error": last_error_message},
            )
            record_llm_exchange(
                session_id,
                stored_request_payload,
                error_message=last_error_message,
            )
            if attempt == max_attempts:
                finish_llm_audit_session(session_id, "error", last_error_message)
                raise ValueError(last_error_message) from exc
        except json.JSONDecodeError as exc:
            last_error_message = "La reponse LLM n'est pas un JSON valide."
            log_llm_debug_exchange(
                "error",
                config,
                session_id,
                purpose,
                attempt,
                {"error": last_error_message},
            )
            record_llm_exchange(
                session_id,
                stored_request_payload,
                error_message=last_error_message,
            )
            if attempt == max_attempts:
                finish_llm_audit_session(session_id, "error", last_error_message)
                raise ValueError(last_error_message) from exc
        except Exception as exc:
            last_error_message = str(exc)
            log_llm_debug_exchange(
                "error",
                config,
                session_id,
                purpose,
                attempt,
                {"error": last_error_message},
            )
            record_llm_exchange(
                session_id,
                stored_request_payload,
                error_message=last_error_message,
            )
            if attempt == max_attempts:
                finish_llm_audit_session(session_id, "error", last_error_message)
                raise

    finish_llm_audit_session(session_id, "error", last_error_message)
    raise ValueError(last_error_message or "Erreur LLM inconnue.")


def call_llm_chat_completion(
    messages,
    purpose="general",
    metadata=None,
    temperature=0.2,
    config_id="",
    max_tokens=None,
    retries=None,
    json_mode=None,
):
    """Call the active LLM and persist the complete exchange in audit tables."""
    config = effective_llm_config(redact_key=False, config_id=config_id)
    return call_llm_chat_completion_with_config(
        config,
        messages,
        purpose=purpose,
        metadata=metadata,
        temperature=temperature,
        max_tokens=max_tokens,
        retries=retries,
        json_mode=json_mode,
    )


def test_llm_config(config_id, prompt=""):
    """Run a small chat completion against one persisted LLM config."""
    cleaned_config_id = normalize_config_id(config_id)
    if not cleaned_config_id:
        raise ValueError("Configuration LLM introuvable.")

    config = get_llm_config_by_id(cleaned_config_id, redact_key=False)
    if not config:
        raise ValueError("Configuration LLM introuvable.")
    if not config.get("configured"):
        raise ValueError("Configuration LLM incomplete ou inactive.")

    test_prompt = str(prompt or "").strip() or (
        "Reponds en une phrase courte pour confirmer que cette configuration LLM fonctionne."
    )
    llm_payload = call_llm_chat_completion_with_config(
        config,
        [
            {
                "role": "system",
                "content": "Tu es un test de configuration. Reponds tres brievement.",
            },
            {"role": "user", "content": test_prompt},
        ],
        purpose="llm_config_test",
        metadata={"tested_config_id": cleaned_config_id},
        temperature=0,
        max_tokens=96,
        retries=1,
        json_mode=False,
    )
    content = extract_chat_completion_content(llm_payload)
    return {
        "config_id": cleaned_config_id,
        "name": config.get("name", ""),
        "provider": config.get("provider", ""),
        "model": config.get("model", ""),
        "content": content,
        "audit_session_id": llm_payload.get("_audit_session_id", ""),
    }


def llm_audit_stats():
    """Return aggregate LLM audit statistics."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                """
                SELECT
                    COUNT(*)::int,
                    COUNT(*) FILTER (WHERE status = 'success')::int,
                    COUNT(*) FILTER (WHERE status = 'error')::int,
                    MAX(started_at)
                FROM public.llm_audit_session;
                """
            )
            session_row = cur.fetchone()
            cur.execute("SELECT COUNT(*)::int FROM public.llm_audit_exchange;")
            exchange_count = int(cur.fetchone()[0])
        conn.commit()
    return {
        "session_count": int(session_row[0]) if session_row else 0,
        "success_count": int(session_row[1]) if session_row else 0,
        "error_count": int(session_row[2]) if session_row else 0,
        "last_started_at": session_row[3].isoformat(timespec="seconds") if session_row and session_row[3] else None,
        "exchange_count": exchange_count,
    }


def list_llm_audit_sessions(limit=100):
    """List recent LLM audit sessions."""
    safe_limit = min(max(int(limit or 100), 1), 500)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                """
                SELECT
                    s.session_id,
                    s.purpose,
                    s.provider,
                    s.model,
                    s.status,
                    s.started_at,
                    s.ended_at,
                    s.error_message,
                    s.config_id,
                    s.config_name,
                    COUNT(e.exchange_id)::int AS exchange_count
                FROM public.llm_audit_session s
                LEFT JOIN public.llm_audit_exchange e ON e.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.started_at DESC
                LIMIT %s;
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()
        conn.commit()
    return [
        {
            "session_id": row[0],
            "purpose": row[1],
            "provider": row[2],
            "model": row[3],
            "status": row[4],
            "started_at": row[5].isoformat(timespec="seconds") if row[5] else "",
            "ended_at": row[6].isoformat(timespec="seconds") if row[6] else "",
            "error_message": row[7] or "",
            "config_id": row[8] or "",
            "config_name": row[9] or "",
            "exchange_count": int(row[10]),
        }
        for row in rows
    ]


def get_llm_audit_session(session_id):
    """Return one LLM audit session with all exchanges."""
    requested_session_id = (session_id or "").strip()
    if not requested_session_id:
        raise ValueError("Session LLM introuvable.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_tables(cur)
            cur.execute(
                """
                SELECT session_id, purpose, provider, model, api_url, status, metadata,
                       error_message, started_at, ended_at, config_id, config_name
                FROM public.llm_audit_session
                WHERE session_id = %s;
                """,
                (requested_session_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Session LLM introuvable: {requested_session_id}")

            cur.execute(
                """
                SELECT exchange_id, sequence_no, request_payload, response_payload, error_message, created_at
                FROM public.llm_audit_exchange
                WHERE session_id = %s
                ORDER BY sequence_no ASC;
                """,
                (requested_session_id,),
            )
            exchange_rows = cur.fetchall()
        conn.commit()

    return {
        "session_id": row[0],
        "purpose": row[1],
        "provider": row[2],
        "model": row[3],
        "api_url": row[4],
        "status": row[5],
        "metadata": row[6] or {},
        "metadata_json": json.dumps(row[6] or {}, ensure_ascii=False, indent=2),
        "error_message": row[7] or "",
        "started_at": row[8].isoformat(timespec="seconds") if row[8] else "",
        "ended_at": row[9].isoformat(timespec="seconds") if row[9] else "",
        "config_id": row[10] or "",
        "config_name": row[11] or "",
        "exchanges": [
            {
                "exchange_id": item[0],
                "sequence_no": int(item[1]),
                "request_payload": item[2] or {},
                "request_json": json.dumps(item[2] or {}, ensure_ascii=False, indent=2),
                "response_payload": item[3] or {},
                "response_json": json.dumps(item[3] or {}, ensure_ascii=False, indent=2),
                "error_message": item[4] or "",
                "created_at": item[5].isoformat(timespec="seconds") if item[5] else "",
            }
            for item in exchange_rows
        ],
    }
