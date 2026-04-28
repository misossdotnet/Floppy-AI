"""QuizBot public flow, administration, and persistence helpers."""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from psycopg2.extras import Json

from db import get_db_connection
from llm_gateway import (
    SUPPORTED_PROVIDERS,
    call_llm_chat_completion_with_config,
    default_api_url_for_provider,
    effective_llm_config,
    extract_chat_completion_content,
    mask_secret,
    normalize_config_id,
    normalize_max_tokens,
    normalize_provider as normalize_llm_provider,
    normalize_retries,
    normalize_timeout,
)


QUIZBOT_CONFIG_TABLE = "quizbot_config"
QUIZBOT_TOPIC_TABLE = "quizbot_topic"
QUIZBOT_SESSION_TABLE = "quizbot_session"
QUIZBOT_AUDIT_TABLE = "quizbot_audit_event"
QUIZBOT_PROVIDERS = set(SUPPORTED_PROVIDERS)
QUIZBOT_RATINGS = {"good", "neutral", "bad"}
LOGGER = logging.getLogger("floppy_ai")
PUBLIC_LLM_ERROR_MESSAGE = (
    "Le moteur LLM ne repond pas pour le moment. Reessayez dans quelques instants."
)
DEFAULT_QUESTION_SYSTEM_PROMPT = (
    "Tu es QuizBot, un moteur de quiz pedagogique. Genere une seule question courte "
    "en francais pour le sujet demande. Retourne uniquement un JSON avec les cles "
    "question, expected_answer, hint et badge."
)
DEFAULT_CORRECTION_SYSTEM_PROMPT = (
    "Tu es QuizBot, correcteur pedagogique. Corrige la reponse de l'utilisateur "
    "avec bienveillance. Retourne uniquement un JSON avec les cles is_correct, "
    "explanation, expected_answer, learning_tip, score et badge."
)
DEFAULT_TOPICS = [
    (
        "culture_generale",
        "Culture generale",
        "Questions courtes sur des connaissances generales.",
        "debutant",
        "Eviter les questions pieges et privilegier les faits stables.",
    ),
    (
        "cybersecurite",
        "Cybersecurite",
        "Principes de securite informatique, hygiene numerique et menaces.",
        "intermediaire",
        "Favoriser les bonnes pratiques defensives.",
    ),
    (
        "python",
        "Python",
        "Syntaxe, bibliotheque standard et bonnes pratiques Python.",
        "intermediaire",
        "La question doit pouvoir etre repondue sans executer de code.",
    ),
    (
        "linux",
        "Linux",
        "Commandes, systeme de fichiers et administration Linux.",
        "intermediaire",
        "Rester sur des commandes communes et non destructives.",
    ),
    (
        "postgresql",
        "PostgreSQL",
        "SQL, schemas, index et exploitation PostgreSQL.",
        "intermediaire",
        "Demander une notion precise et verifier la comprehension.",
    ),
    (
        "histoire",
        "Histoire",
        "Questions historiques generales et dates importantes.",
        "debutant",
        "Eviter les sujets ambigus ou controverses sans contexte.",
    ),
    (
        "jeux_video",
        "Jeux video",
        "Culture et histoire du jeu video.",
        "debutant",
        "Question courte, accessible et factuelle.",
    ),
    (
        "devops",
        "DevOps",
        "CI/CD, observabilite, conteneurs et pratiques d'exploitation.",
        "intermediaire",
        "Relier la question a un usage concret.",
    ),
]


class QuizbotUnavailableError(ValueError):
    """Public-safe exception for unavailable QuizBot dependencies."""


def now_iso():
    """Return UTC timestamp as ISO text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_bool(value, default=False):
    """Normalize form booleans."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes", "oui"}


def normalize_provider(raw_provider):
    """Normalize QuizBot provider."""
    return normalize_llm_provider(raw_provider)


def normalize_temperature(raw_value, default=0.2):
    """Normalize a temperature value."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0.0), 2.0)


def normalize_rating(raw_rating):
    """Normalize public user rating."""
    rating = str(raw_rating or "").strip().lower()
    if rating not in QUIZBOT_RATINGS:
        raise ValueError("Note QuizBot invalide.")
    return rating


def normalize_topic_id(value):
    """Build a stable topic identifier."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").lower()).strip("_")
    cleaned = cleaned[:80].strip("_")
    if not cleaned:
        cleaned = uuid4().hex
    return cleaned


def trim_text(value, max_length):
    """Trim text to a maximum persisted length."""
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip()


def ensure_quizbot_tables(cur):
    """Create QuizBot tables and seed default topics."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.quizbot_config (
            id integer PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            llm_config_id text NOT NULL DEFAULT '',
            question_llm_config_id text NOT NULL DEFAULT '',
            correction_llm_config_id text NOT NULL DEFAULT '',
            provider text NOT NULL DEFAULT 'ollama'
                CHECK (provider IN ('ollama', 'litellm', 'openai', 'lmstudio', 'openai_compatible', 'custom')),
            api_url text NOT NULL,
            api_key text,
            question_model text NOT NULL DEFAULT '',
            correction_model text NOT NULL DEFAULT '',
            temperature numeric(4, 2) NOT NULL DEFAULT 0.2,
            max_tokens integer NOT NULL DEFAULT 800,
            timeout_seconds integer NOT NULL DEFAULT 90,
            retry_count integer NOT NULL DEFAULT 1,
            strict_json boolean NOT NULL DEFAULT true,
            question_system_prompt text NOT NULL DEFAULT '',
            correction_system_prompt text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.quizbot_config ADD COLUMN IF NOT EXISTS llm_config_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.quizbot_config ADD COLUMN IF NOT EXISTS question_llm_config_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.quizbot_config ADD COLUMN IF NOT EXISTS correction_llm_config_id text NOT NULL DEFAULT '';")
    cur.execute(
        """
        UPDATE public.quizbot_config
        SET question_llm_config_id = llm_config_id
        WHERE question_llm_config_id = '' AND llm_config_id <> '';
        """
    )
    cur.execute(
        """
        UPDATE public.quizbot_config
        SET correction_llm_config_id = question_llm_config_id
        WHERE correction_llm_config_id = '' AND question_llm_config_id <> '';
        """
    )
    cur.execute("ALTER TABLE public.quizbot_config DROP CONSTRAINT IF EXISTS quizbot_config_provider_check;")
    cur.execute(
        """
        ALTER TABLE public.quizbot_config
        ADD CONSTRAINT quizbot_config_provider_check
        CHECK (provider IN ('ollama', 'litellm', 'openai', 'lmstudio', 'openai_compatible', 'custom'));
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.quizbot_topic (
            topic_id text PRIMARY KEY,
            label text NOT NULL,
            description text NOT NULL DEFAULT '',
            level text NOT NULL DEFAULT '',
            instructions text NOT NULL DEFAULT '',
            active boolean NOT NULL DEFAULT true,
            archived boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.quizbot_session (
            session_id text PRIMARY KEY,
            topic_id text REFERENCES public.quizbot_topic(topic_id) ON DELETE SET NULL,
            topic_label text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'started',
            question_text text NOT NULL DEFAULT '',
            expected_answer text NOT NULL DEFAULT '',
            user_answer text NOT NULL DEFAULT '',
            correction jsonb NOT NULL DEFAULT '{}'::jsonb,
            is_correct boolean,
            rating text CHECK (rating IN ('good', 'neutral', 'bad')),
            comment text NOT NULL DEFAULT '',
            error_message text,
            question_model text NOT NULL DEFAULT '',
            correction_model text NOT NULL DEFAULT '',
            generation_audit_session_id text NOT NULL DEFAULT '',
            correction_audit_session_id text NOT NULL DEFAULT '',
            generation_duration_ms integer,
            correction_duration_ms integer,
            user_agent text NOT NULL DEFAULT '',
            ip_address text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS quizbot_session_created_idx
        ON public.quizbot_session(created_at DESC);
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS quizbot_session_topic_idx
        ON public.quizbot_session(topic_id, created_at DESC);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.quizbot_audit_event (
            event_id text PRIMARY KEY,
            session_id text,
            actor text NOT NULL DEFAULT 'system',
            event_type text NOT NULL,
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS quizbot_audit_created_idx
        ON public.quizbot_audit_event(created_at DESC);
        """
    )
    seed_quizbot_defaults(cur)


def seed_quizbot_defaults(cur):
    """Insert one config row and default topics when missing."""
    llm_config_id = normalize_config_id(os.getenv("QUIZBOT_LLM_CONFIG_ID"))
    raw_provider = os.getenv("QUIZBOT_LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "ollama"
    provider = normalize_provider(raw_provider)
    can_reuse_global_url = str(raw_provider or "").strip().lower() in QUIZBOT_PROVIDERS
    api_url = (
        os.getenv("QUIZBOT_LLM_API_URL")
        or (os.getenv("LLM_API_URL") if can_reuse_global_url else "")
        or (os.getenv("AGENTAI_LLM_API_URL") if can_reuse_global_url else "")
        or default_api_url_for_provider(provider)
    ).strip()
    question_model = (
        os.getenv("QUIZBOT_QUESTION_MODEL")
        or os.getenv("QUIZBOT_LLM_MODEL")
        or os.getenv("LLM_MODEL")
        or os.getenv("AGENTAI_LLM_MODEL")
        or ""
    ).strip()
    correction_model = (
        os.getenv("QUIZBOT_CORRECTION_MODEL")
        or os.getenv("QUIZBOT_LLM_MODEL")
        or os.getenv("LLM_MODEL")
        or os.getenv("AGENTAI_LLM_MODEL")
        or question_model
    ).strip()
    api_key = (
        os.getenv("QUIZBOT_LLM_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("AGENTAI_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    cur.execute(
        """
        INSERT INTO public.quizbot_config (
            id,
            enabled,
            llm_config_id,
            question_llm_config_id,
            correction_llm_config_id,
            provider,
            api_url,
            api_key,
            question_model,
            correction_model,
            temperature,
            max_tokens,
            timeout_seconds,
            retry_count,
            strict_json,
            question_system_prompt,
            correction_system_prompt
        )
        VALUES (1, true, %s, %s, %s, %s, %s, %s, %s, %s, 0.2, 800, 90, 1, true, %s, %s)
        ON CONFLICT (id) DO NOTHING;
        """,
        (
            llm_config_id,
            llm_config_id,
            llm_config_id,
            provider,
            api_url,
            api_key,
            question_model,
            correction_model,
            DEFAULT_QUESTION_SYSTEM_PROMPT,
            DEFAULT_CORRECTION_SYSTEM_PROMPT,
        ),
    )
    for topic in DEFAULT_TOPICS:
        cur.execute(
            """
            INSERT INTO public.quizbot_topic
                (topic_id, label, description, level, instructions, active, archived)
            VALUES (%s, %s, %s, %s, %s, true, false)
            ON CONFLICT (topic_id) DO NOTHING;
            """,
            topic,
        )


def serialize_config(row, redact_key=False):
    """Serialize a QuizBot config row."""
    if not row:
        return None
    payload = {
        "enabled": bool(row[0]),
        "llm_config_id": row[1] or "",
        "question_llm_config_id": row[2] or row[1] or "",
        "correction_llm_config_id": row[3] or row[2] or row[1] or "",
        "provider": row[4],
        "api_url": row[5] or "",
        "api_key": row[6] or "",
        "question_model": row[7] or "",
        "correction_model": row[8] or "",
        "temperature": float(row[9]) if row[9] is not None else 0.2,
        "max_tokens": int(row[10]) if row[10] is not None else 800,
        "timeout_seconds": int(row[11]) if row[11] is not None else 90,
        "retry_count": int(row[12]) if row[12] is not None else 1,
        "strict_json": bool(row[13]),
        "question_system_prompt": row[14] or DEFAULT_QUESTION_SYSTEM_PROMPT,
        "correction_system_prompt": row[15] or DEFAULT_CORRECTION_SYSTEM_PROMPT,
        "updated_at": row[16].isoformat(timespec="seconds") if row[16] else "",
    }
    payload["configured"] = bool(payload["enabled"])
    if redact_key:
        payload["api_key_masked"] = mask_secret(payload.get("api_key"))
        payload["api_key_configured"] = bool(payload.get("api_key"))
        payload.pop("api_key", None)
    return payload


def get_quizbot_config(redact_key=False):
    """Return persisted QuizBot LLM configuration."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                SELECT enabled, llm_config_id, question_llm_config_id, correction_llm_config_id,
                       provider, api_url, api_key, question_model, correction_model,
                       temperature, max_tokens, timeout_seconds, retry_count, strict_json,
                       question_system_prompt, correction_system_prompt, updated_at
                FROM public.quizbot_config
                WHERE id = 1;
                """
            )
            row = cur.fetchone()
        conn.commit()
    return serialize_config(row, redact_key=redact_key)


def save_quizbot_config(payload, actor="admin"):
    """Persist QuizBot LLM configuration."""
    payload = payload if isinstance(payload, dict) else {}
    current = get_quizbot_config(redact_key=False) or {}
    uses_registry_selection = (
        "question_llm_config_id" in payload or "correction_llm_config_id" in payload
    )

    legacy_llm_config_id = normalize_config_id(
        payload.get("llm_config_id")
        if "llm_config_id" in payload
        else current.get("llm_config_id", "")
    )
    question_llm_config_id = normalize_config_id(
        payload.get("question_llm_config_id")
        if "question_llm_config_id" in payload
        else current.get("question_llm_config_id", "") or legacy_llm_config_id
    )
    correction_llm_config_id = normalize_config_id(
        payload.get("correction_llm_config_id")
        if "correction_llm_config_id" in payload
        else current.get("correction_llm_config_id", "")
    ) or question_llm_config_id
    llm_config_id = (
        question_llm_config_id
        if uses_registry_selection
        else question_llm_config_id or legacy_llm_config_id
    )

    provider = normalize_provider(
        payload.get("provider") if "provider" in payload else current.get("provider", "ollama")
    )
    api_url = (
        payload.get("api_url")
        if "api_url" in payload
        else current.get("api_url") or default_api_url_for_provider(provider)
    )
    api_url = (api_url or default_api_url_for_provider(provider)).strip()
    api_key = (
        payload.get("api_key")
        if "api_key" in payload and payload.get("api_key")
        else current.get("api_key", "")
    )
    api_key = (api_key or "").strip()
    if uses_registry_selection:
        question_model = (payload.get("question_model") or "").strip()
        correction_model = (payload.get("correction_model") or "").strip()
    else:
        question_model = (
            payload.get("question_model")
            if "question_model" in payload
            else current.get("question_model", "")
        )
        question_model = (question_model or "").strip()
        correction_model = (
            payload.get("correction_model")
            if "correction_model" in payload
            else current.get("correction_model", "")
        )
        correction_model = (correction_model or question_model).strip()
    question_system_prompt = trim_text(
        (
            payload.get("question_system_prompt")
            if "question_system_prompt" in payload
            else current.get("question_system_prompt")
        )
        or DEFAULT_QUESTION_SYSTEM_PROMPT,
        12000,
    )
    correction_system_prompt = trim_text(
        (
            payload.get("correction_system_prompt")
            if "correction_system_prompt" in payload
            else current.get("correction_system_prompt")
        )
        or DEFAULT_CORRECTION_SYSTEM_PROMPT,
        12000,
    )
    enabled = normalize_bool(payload.get("enabled"), default=False)
    strict_json = normalize_bool(
        payload.get("strict_json")
        if "strict_json" in payload
        else current.get("strict_json", True),
        default=True,
    )
    temperature = normalize_temperature(
        payload.get("temperature") if "temperature" in payload else current.get("temperature", 0.2)
    )
    max_tokens = normalize_max_tokens(
        payload.get("max_tokens") if "max_tokens" in payload else current.get("max_tokens", 800)
    )
    timeout_seconds = normalize_timeout(
        payload.get("timeout_seconds")
        if "timeout_seconds" in payload
        else current.get("timeout_seconds", 90)
    )
    retry_count = normalize_retries(
        payload.get("retry_count")
        if "retry_count" in payload
        else current.get("retry_count", 1)
    )

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                UPDATE public.quizbot_config
                SET enabled = %s,
                    llm_config_id = %s,
                    question_llm_config_id = %s,
                    correction_llm_config_id = %s,
                    provider = %s,
                    api_url = %s,
                    api_key = %s,
                    question_model = %s,
                    correction_model = %s,
                    temperature = %s,
                    max_tokens = %s,
                    timeout_seconds = %s,
                    retry_count = %s,
                    strict_json = %s,
                    question_system_prompt = %s,
                    correction_system_prompt = %s,
                    updated_at = now()
                WHERE id = 1;
                """,
                (
                    enabled,
                    llm_config_id,
                    question_llm_config_id,
                    correction_llm_config_id,
                    provider,
                    api_url,
                    api_key,
                    question_model,
                    correction_model,
                    temperature,
                    max_tokens,
                    timeout_seconds,
                    retry_count,
                    strict_json,
                    question_system_prompt,
                    correction_system_prompt,
                ),
            )
            record_quizbot_audit(
                cur,
                "config_updated",
                actor=actor,
                details={
                    "llm_config_id": llm_config_id,
                    "question_llm_config_id": question_llm_config_id,
                    "correction_llm_config_id": correction_llm_config_id,
                    "provider": provider,
                    "api_url": api_url,
                    "enabled": enabled,
                },
            )
        conn.commit()
    return get_quizbot_config(redact_key=True)


def serialize_topic(row):
    """Serialize a topic row."""
    return {
        "topic_id": row[0],
        "label": row[1],
        "description": row[2] or "",
        "level": row[3] or "",
        "instructions": row[4] or "",
        "active": bool(row[5]),
        "archived": bool(row[6]),
        "created_at": row[7].isoformat(timespec="seconds") if row[7] else "",
        "updated_at": row[8].isoformat(timespec="seconds") if row[8] else "",
    }


def list_quizbot_topics(include_archived=True):
    """List QuizBot topics."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            if include_archived:
                cur.execute(
                    """
                    SELECT topic_id, label, description, level, instructions, active,
                           archived, created_at, updated_at
                    FROM public.quizbot_topic
                    ORDER BY archived ASC, active DESC, label ASC;
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT topic_id, label, description, level, instructions, active,
                           archived, created_at, updated_at
                    FROM public.quizbot_topic
                    WHERE archived = false
                    ORDER BY active DESC, label ASC;
                    """
                )
            rows = cur.fetchall()
        conn.commit()
    return [serialize_topic(row) for row in rows]


def create_quizbot_topic(payload, actor="admin"):
    """Create a QuizBot topic."""
    label = trim_text(payload.get("label"), 180)
    if not label:
        raise ValueError("Le libelle du sujet est obligatoire.")
    topic_id = normalize_topic_id(payload.get("topic_id") or label)
    description = trim_text(payload.get("description"), 2000)
    level = trim_text(payload.get("level"), 80)
    instructions = trim_text(payload.get("instructions"), 4000)
    active = normalize_bool(payload.get("active"), default=False)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                INSERT INTO public.quizbot_topic
                    (topic_id, label, description, level, instructions, active, archived)
                VALUES (%s, %s, %s, %s, %s, %s, false);
                """,
                (topic_id, label, description, level, instructions, active),
            )
            record_quizbot_audit(
                cur,
                "topic_created",
                actor=actor,
                details={"topic_id": topic_id, "label": label},
            )
        conn.commit()
    return topic_id


def update_quizbot_topic(topic_id, payload, actor="admin"):
    """Update a QuizBot topic."""
    cleaned_topic_id = normalize_topic_id(topic_id)
    label = trim_text(payload.get("label"), 180)
    if not label:
        raise ValueError("Le libelle du sujet est obligatoire.")
    description = trim_text(payload.get("description"), 2000)
    level = trim_text(payload.get("level"), 80)
    instructions = trim_text(payload.get("instructions"), 4000)
    active = normalize_bool(payload.get("active"), default=False)
    archived = normalize_bool(payload.get("archived"), default=False)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                UPDATE public.quizbot_topic
                SET label = %s,
                    description = %s,
                    level = %s,
                    instructions = %s,
                    active = %s,
                    archived = %s,
                    updated_at = now()
                WHERE topic_id = %s;
                """,
                (
                    label,
                    description,
                    level,
                    instructions,
                    active,
                    archived,
                    cleaned_topic_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError("Sujet QuizBot introuvable.")
            record_quizbot_audit(
                cur,
                "topic_updated",
                actor=actor,
                details={"topic_id": cleaned_topic_id, "label": label},
            )
        conn.commit()


def archive_quizbot_topic(topic_id, actor="admin"):
    """Archive a QuizBot topic."""
    cleaned_topic_id = normalize_topic_id(topic_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                UPDATE public.quizbot_topic
                SET active = false, archived = true, updated_at = now()
                WHERE topic_id = %s;
                """,
                (cleaned_topic_id,),
            )
            if cur.rowcount == 0:
                raise ValueError("Sujet QuizBot introuvable.")
            record_quizbot_audit(
                cur,
                "topic_archived",
                actor=actor,
                details={"topic_id": cleaned_topic_id},
            )
        conn.commit()


def delete_quizbot_topic(topic_id, actor="admin"):
    """Delete a QuizBot topic while keeping denormalized session labels."""
    cleaned_topic_id = normalize_topic_id(topic_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                "DELETE FROM public.quizbot_topic WHERE topic_id = %s;",
                (cleaned_topic_id,),
            )
            if cur.rowcount == 0:
                raise ValueError("Sujet QuizBot introuvable.")
            record_quizbot_audit(
                cur,
                "topic_deleted",
                actor=actor,
                details={"topic_id": cleaned_topic_id},
            )
        conn.commit()


def record_quizbot_audit(cur, event_type, actor="system", session_id="", details=None):
    """Record a QuizBot audit event using an existing cursor."""
    cur.execute(
        """
        INSERT INTO public.quizbot_audit_event
            (event_id, session_id, actor, event_type, details)
        VALUES (%s, %s, %s, %s, %s);
        """,
        (
            uuid4().hex,
            session_id or None,
            trim_text(actor or "system", 80),
            trim_text(event_type, 120),
            Json(details or {}),
        ),
    )


def select_random_active_topic(cur):
    """Select one active topic randomly."""
    cur.execute(
        """
        SELECT topic_id, label, description, level, instructions, active,
               archived, created_at, updated_at
        FROM public.quizbot_topic
        WHERE active = true AND archived = false
        ORDER BY random()
        LIMIT 1;
        """
    )
    row = cur.fetchone()
    if not row:
        raise QuizbotUnavailableError("Aucun sujet actif n'est disponible.")
    return serialize_topic(row)


def quizbot_public_status():
    """Return public-safe QuizBot availability."""
    try:
        config = get_quizbot_config(redact_key=True)
        topics = list_quizbot_topics(include_archived=False)
        active_topic_count = len(
            [topic for topic in topics if topic["active"] and not topic["archived"]]
        )
        db_error = ""
    except Exception as exc:
        config = None
        active_topic_count = 0
        db_error = str(exc) or exc.__class__.__name__

    configured = quizbot_runtime_configured(config) if config else False
    enabled = bool(config and config.get("enabled"))
    question_runtime = {}
    correction_runtime = {}
    if config:
        try:
            question_config_id = config.get("question_llm_config_id", "") or config.get("llm_config_id", "")
            correction_config_id = (
                config.get("correction_llm_config_id", "")
                or question_config_id
            )
            question_runtime = runtime_config_for_model(
                config,
                config.get("question_model", ""),
                question_config_id,
            )
            correction_runtime = runtime_config_for_model(
                config,
                config.get("correction_model", ""),
                correction_config_id,
            )
        except Exception:
            question_runtime = {}
            correction_runtime = {}
    return {
        "enabled": enabled,
        "configured": configured,
        "available": bool(enabled and configured and active_topic_count > 0 and not db_error),
        "provider": question_runtime.get("provider", "") or (config.get("provider", "") if config else ""),
        "question_model": question_runtime.get("model", "") or (config.get("question_model", "") if config else ""),
        "correction_model": correction_runtime.get("model", "") or (config.get("correction_model", "") if config else ""),
        "active_topic_count": active_topic_count,
        "db_error": db_error,
    }


def require_quizbot_available():
    """Validate public QuizBot availability."""
    status = quizbot_public_status()
    if status["db_error"]:
        raise QuizbotUnavailableError("QuizBot est temporairement indisponible.")
    if not status["enabled"]:
        raise QuizbotUnavailableError("QuizBot est desactive par l'administration.")
    if not status["configured"]:
        raise QuizbotUnavailableError("QuizBot n'est pas encore configure.")
    if status["active_topic_count"] <= 0:
        raise QuizbotUnavailableError("Aucun sujet actif n'est disponible.")
    return status


def runtime_config_for_model(config, model="", llm_config_id=""):
    """Build a runtime LLM config for one QuizBot model."""
    legacy_model = (model or "").strip()
    selected_config_id = normalize_config_id(llm_config_id) or normalize_config_id(
        config.get("llm_config_id", "")
    )
    try:
        shared_config = effective_llm_config(
            redact_key=False,
            config_id=selected_config_id,
        )
    except Exception:
        shared_config = None

    if shared_config and shared_config.get("configured"):
        runtime = dict(shared_config)
        shared_model = runtime.get("model", "")
        runtime["model"] = shared_model
        runtime["configured"] = bool(
            runtime.get("enabled") and runtime.get("api_url") and runtime.get("model")
        )
        runtime["_uses_shared_config"] = True
        runtime["_shared_model"] = shared_model
        runtime["_ignored_legacy_model"] = legacy_model if legacy_model != shared_model else ""
        return runtime

    fallback_model = legacy_model or config.get("question_model") or config.get("correction_model") or ""
    return {
        "config_id": selected_config_id or "quizbot_legacy",
        "name": "QuizBot legacy fallback",
        "enabled": config["enabled"],
        "provider": config["provider"],
        "api_url": config["api_url"],
        "api_key": config.get("api_key", ""),
        "model": fallback_model,
        "timeout_seconds": config["timeout_seconds"],
        "max_tokens": config["max_tokens"],
        "retries": config["retry_count"],
        "json_mode": config["strict_json"],
        "configured": bool(config["enabled"] and config["api_url"] and fallback_model),
    }


def quizbot_runtime_configured(config):
    """Return whether QuizBot can resolve usable question and correction LLM configs."""
    if not config or not config.get("enabled"):
        return False
    try:
        question_config = runtime_config_for_model(
            config,
            config.get("question_model", ""),
            config.get("question_llm_config_id", "") or config.get("llm_config_id", ""),
        )
        correction_config = runtime_config_for_model(
            config,
            config.get("correction_model") or config.get("question_model", ""),
            config.get("correction_llm_config_id", "")
            or config.get("question_llm_config_id", "")
            or config.get("llm_config_id", ""),
        )
        return bool(question_config.get("configured") and correction_config.get("configured"))
    except Exception:
        return False


def should_retry_without_json(error):
    """Return whether a failed call looks like unsupported strict JSON mode."""
    text = str(error or "").lower()
    return (
        "response_format" in text
        or "json" in text
        or "format" in text
        or "http 400" in text
    )


def should_retry_shared_model(error):
    """Return whether a failed call looks like a model-name mismatch."""
    text = str(error or "").lower()
    return (
        "model" in text
        or "not found" in text
        or "not installed" in text
        or "does not exist" in text
        or "http 404" in text
    )


def clean_llm_json_text(raw_text):
    """Remove common markdown wrappers around JSON."""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_json_object(raw_text):
    """Parse a JSON object from an LLM response."""
    cleaned = clean_llm_json_text(raw_text)
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(cleaned[start : end + 1])
        else:
            raise
    if not isinstance(parsed, dict):
        raise ValueError("La reponse LLM ne contient pas un objet JSON.")
    return parsed


def call_quizbot_llm(config, model, messages, purpose, metadata, llm_config_id=""):
    """Call LLM with QuizBot settings and return content, audit id, duration."""
    start = time.perf_counter()
    primary_runtime = runtime_config_for_model(config, model, llm_config_id)
    attempts = [(primary_runtime, bool(primary_runtime.get("json_mode")), "primary")]
    last_error = None

    for runtime_config, json_mode, attempt_name in attempts:
        try:
            llm_payload = call_llm_chat_completion_with_config(
                runtime_config,
                messages,
                purpose=purpose,
                metadata={
                    **(metadata or {}),
                    "quizbot_attempt": attempt_name,
                    "quizbot_runtime_model": runtime_config.get("model", ""),
                    "quizbot_llm_config_id": runtime_config.get("config_id", ""),
                    "quizbot_json_mode": json_mode,
                },
                temperature=config["temperature"],
                max_tokens=config["max_tokens"],
                retries=None,
                json_mode=json_mode,
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            return (
                extract_chat_completion_content(llm_payload),
                llm_payload.get("_audit_session_id", ""),
                duration_ms,
            )
        except Exception as exc:
            last_error = exc
            if json_mode and should_retry_without_json(exc):
                attempts.append((runtime_config, False, "without_json_mode"))
            shared_model = runtime_config.get("_shared_model", "")
            if (
                shared_model
                and runtime_config.get("model") != shared_model
                and should_retry_shared_model(exc)
            ):
                shared_runtime = dict(runtime_config)
                shared_runtime["model"] = shared_model
                shared_runtime["configured"] = bool(
                    shared_runtime.get("enabled")
                    and shared_runtime.get("api_url")
                    and shared_runtime.get("model")
                )
                attempts.append((shared_runtime, json_mode, "shared_default_model"))
                if json_mode:
                    attempts.append((shared_runtime, False, "shared_default_model_without_json_mode"))

    raise last_error or QuizbotUnavailableError(PUBLIC_LLM_ERROR_MESSAGE)


def build_question_messages(config, topic):
    """Build LLM messages for question generation."""
    user_prompt = (
        f"Sujet: {topic['label']}\n"
        f"Niveau: {topic['level'] or 'general'}\n"
        f"Description: {topic['description'] or '-'}\n"
        f"Consignes: {topic['instructions'] or '-'}\n\n"
        "Genere une question unique, claire et verifiable."
    )
    return [
        {"role": "system", "content": config["question_system_prompt"]},
        {"role": "user", "content": user_prompt},
    ]


def build_correction_messages(config, session_row, user_answer):
    """Build LLM messages for answer correction."""
    user_prompt = (
        f"Sujet: {session_row['topic_label']}\n"
        f"Question: {session_row['question_text']}\n"
        f"Correction attendue connue par le systeme: {session_row['expected_answer'] or '-'}\n"
        f"Reponse utilisateur: {user_answer}\n\n"
        "Corrige la reponse. is_correct doit etre un booleen. "
        "score vaut 1 si correct, 0 sinon."
    )
    return [
        {"role": "system", "content": config["correction_system_prompt"]},
        {"role": "user", "content": user_prompt},
    ]


def normalize_question_payload(raw_content):
    """Normalize generated question data."""
    try:
        parsed = parse_json_object(raw_content)
    except ValueError:
        parsed = {"question": raw_content}
    question = trim_text(
        parsed.get("question") or parsed.get("question_text") or parsed.get("prompt"),
        8000,
    )
    if not question:
        raise ValueError("Question non generee.")
    return {
        "question": question,
        "expected_answer": trim_text(
            parsed.get("expected_answer") or parsed.get("answer") or "",
            8000,
        ),
        "hint": trim_text(parsed.get("hint") or "", 800),
        "badge": trim_text(parsed.get("badge") or "Quiz lance", 120),
        "raw": parsed,
    }


def normalize_correction_payload(raw_content, fallback_expected_answer=""):
    """Normalize correction data."""
    try:
        parsed = parse_json_object(raw_content)
    except ValueError:
        parsed = {
            "is_correct": False,
            "explanation": raw_content,
            "expected_answer": fallback_expected_answer,
            "learning_tip": "",
            "score": 0,
        }
    is_correct = bool(parsed.get("is_correct", parsed.get("correct", False)))
    try:
        score = int(parsed.get("score", 1 if is_correct else 0))
    except (TypeError, ValueError):
        score = 1 if is_correct else 0
    return {
        "is_correct": is_correct,
        "score": max(0, min(score, 1)),
        "explanation": trim_text(parsed.get("explanation") or parsed.get("feedback") or "", 2000),
        "expected_answer": trim_text(
            parsed.get("expected_answer") or parsed.get("correction") or fallback_expected_answer,
            2000,
        ),
        "learning_tip": trim_text(parsed.get("learning_tip") or parsed.get("tip") or "", 1200),
        "badge": trim_text(parsed.get("badge") or ("Bien joue" if is_correct else "A revoir"), 120),
        "raw": parsed,
    }


def start_quiz_session(user_agent="", ip_address=""):
    """Start a public quiz session and generate the first question."""
    require_quizbot_available()
    config = get_quizbot_config(redact_key=False)
    question_llm_config_id = config.get("question_llm_config_id", "") or config.get("llm_config_id", "")
    correction_llm_config_id = (
        config.get("correction_llm_config_id", "")
        or question_llm_config_id
    )
    question_runtime = runtime_config_for_model(
        config,
        config.get("question_model", ""),
        question_llm_config_id,
    )
    correction_runtime = runtime_config_for_model(
        config,
        config.get("correction_model", ""),
        correction_llm_config_id,
    )
    session_id = uuid4().hex
    topic = None
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_quizbot_tables(cur)
                topic = select_random_active_topic(cur)
                cur.execute(
                    """
                    INSERT INTO public.quizbot_session
                        (session_id, topic_id, topic_label, status, question_model,
                         correction_model, user_agent, ip_address)
                    VALUES (%s, %s, %s, 'started', %s, %s, %s, %s);
                    """,
                    (
                        session_id,
                        topic["topic_id"],
                        topic["label"],
                        question_runtime.get("model", ""),
                        correction_runtime.get("model", ""),
                        trim_text(user_agent, 800),
                        trim_text(ip_address, 120),
                    ),
                )
                record_quizbot_audit(
                    cur,
                    "session_started",
                    actor="public",
                    session_id=session_id,
                    details={"topic_id": topic["topic_id"], "topic_label": topic["label"]},
                )
            conn.commit()

        raw_content, audit_session_id, duration_ms = call_quizbot_llm(
            config,
            config.get("question_model", ""),
            build_question_messages(config, topic),
            purpose="quizbot_question",
            metadata={"quizbot_session_id": session_id, "topic_id": topic["topic_id"]},
            llm_config_id=question_llm_config_id,
        )
        question_payload = normalize_question_payload(raw_content)

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_quizbot_tables(cur)
                cur.execute(
                    """
                    UPDATE public.quizbot_session
                    SET status = 'question_generated',
                        question_text = %s,
                        expected_answer = %s,
                        generation_audit_session_id = %s,
                        generation_duration_ms = %s,
                        updated_at = now()
                    WHERE session_id = %s;
                    """,
                    (
                        question_payload["question"],
                        question_payload["expected_answer"],
                        audit_session_id,
                        duration_ms,
                        session_id,
                    ),
                )
                record_quizbot_audit(
                    cur,
                    "question_generated",
                    actor="system",
                    session_id=session_id,
                    details={
                        "audit_session_id": audit_session_id,
                        "duration_ms": duration_ms,
                    },
                )
            conn.commit()

        return {
            "session_id": session_id,
            "topic": {
                "label": topic["label"],
                "level": topic["level"],
            },
            "question": question_payload["question"],
            "hint": question_payload["hint"],
            "badge": question_payload["badge"],
            "score": 0,
            "progress": {"current": 1, "total": 1},
            "created_at": now_iso(),
        }
    except QuizbotUnavailableError:
        raise
    except Exception as exc:
        LOGGER.exception("quizbot_question_error session_id=%s", session_id)
        mark_quizbot_session_error(session_id, str(exc), topic=topic)
        raise QuizbotUnavailableError(PUBLIC_LLM_ERROR_MESSAGE) from exc


def mark_quizbot_session_error(session_id, error_message, topic=None):
    """Mark a public session as errored and audit the raw server-side cause."""
    if not session_id:
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_quizbot_tables(cur)
                cur.execute(
                    """
                    UPDATE public.quizbot_session
                    SET status = 'error',
                        error_message = %s,
                        updated_at = now(),
                        completed_at = COALESCE(completed_at, now())
                    WHERE session_id = %s;
                    """,
                    (trim_text(error_message, 2000), session_id),
                )
                record_quizbot_audit(
                    cur,
                    "session_error",
                    actor="system",
                    session_id=session_id,
                    details={
                        "error": trim_text(error_message, 2000),
                        "topic_id": topic.get("topic_id", "") if topic else "",
                    },
                )
            conn.commit()
    except Exception:
        return


def load_quizbot_session(cur, session_id):
    """Load one QuizBot session for internal processing."""
    cur.execute(
        """
        SELECT
            session_id,
            topic_id,
            topic_label,
            status,
            question_text,
            expected_answer,
            user_answer,
            correction,
            is_correct,
            rating,
            comment,
            error_message,
            question_model,
            correction_model,
            generation_audit_session_id,
            correction_audit_session_id,
            generation_duration_ms,
            correction_duration_ms,
            created_at,
            updated_at,
            completed_at
        FROM public.quizbot_session
        WHERE session_id = %s;
        """,
        (session_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("Session QuizBot introuvable.")
    return {
        "session_id": row[0],
        "topic_id": row[1] or "",
        "topic_label": row[2] or "",
        "status": row[3],
        "question_text": row[4] or "",
        "expected_answer": row[5] or "",
        "user_answer": row[6] or "",
        "correction": row[7] or {},
        "is_correct": row[8],
        "rating": row[9] or "",
        "comment": row[10] or "",
        "error_message": row[11] or "",
        "question_model": row[12] or "",
        "correction_model": row[13] or "",
        "generation_audit_session_id": row[14] or "",
        "correction_audit_session_id": row[15] or "",
        "generation_duration_ms": row[16],
        "correction_duration_ms": row[17],
        "created_at": row[18],
        "updated_at": row[19],
        "completed_at": row[20],
    }


def submit_quiz_answer(payload):
    """Correct a public quiz answer with the LLM."""
    if not isinstance(payload, dict):
        raise ValueError("Payload QuizBot invalide.")
    session_id = (payload.get("session_id") or "").strip()
    user_answer = trim_text(payload.get("answer"), 8000)
    if not session_id:
        raise ValueError("Session QuizBot obligatoire.")
    if not user_answer:
        raise ValueError("La reponse est obligatoire.")

    require_quizbot_available()
    config = get_quizbot_config(redact_key=False)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            session_row = load_quizbot_session(cur, session_id)
        conn.commit()

    if session_row["status"] == "error":
        raise QuizbotUnavailableError("Cette session QuizBot est en erreur.")
    if not session_row["question_text"]:
        raise ValueError("Aucune question n'est disponible pour cette session.")

    try:
        correction_llm_config_id = (
            config.get("correction_llm_config_id", "")
            or config.get("question_llm_config_id", "")
            or config.get("llm_config_id", "")
        )
        raw_content, audit_session_id, duration_ms = call_quizbot_llm(
            config,
            config.get("correction_model", ""),
            build_correction_messages(config, session_row, user_answer),
            purpose="quizbot_correction",
            metadata={"quizbot_session_id": session_id, "topic_id": session_row["topic_id"]},
            llm_config_id=correction_llm_config_id,
        )
        correction_payload = normalize_correction_payload(
            raw_content,
            fallback_expected_answer=session_row["expected_answer"],
        )

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_quizbot_tables(cur)
                cur.execute(
                    """
                    UPDATE public.quizbot_session
                    SET status = 'answered',
                        user_answer = %s,
                        correction = %s,
                        is_correct = %s,
                        correction_audit_session_id = %s,
                        correction_duration_ms = %s,
                        updated_at = now()
                    WHERE session_id = %s;
                    """,
                    (
                        user_answer,
                        Json(correction_payload),
                        correction_payload["is_correct"],
                        audit_session_id,
                        duration_ms,
                        session_id,
                    ),
                )
                record_quizbot_audit(
                    cur,
                    "answer_corrected",
                    actor="system",
                    session_id=session_id,
                    details={
                        "is_correct": correction_payload["is_correct"],
                        "audit_session_id": audit_session_id,
                        "duration_ms": duration_ms,
                    },
                )
            conn.commit()

        return {
            "session_id": session_id,
            "is_correct": correction_payload["is_correct"],
            "score": correction_payload["score"],
            "explanation": correction_payload["explanation"],
            "expected_answer": correction_payload["expected_answer"],
            "learning_tip": correction_payload["learning_tip"],
            "badge": correction_payload["badge"],
            "progress": {"current": 1, "total": 1},
            "created_at": now_iso(),
        }
    except QuizbotUnavailableError:
        raise
    except Exception as exc:
        LOGGER.exception("quizbot_correction_error session_id=%s", session_id)
        mark_quizbot_session_error(session_id, str(exc))
        raise QuizbotUnavailableError(PUBLIC_LLM_ERROR_MESSAGE) from exc


def submit_quiz_feedback(payload):
    """Persist public QuizBot feedback."""
    if not isinstance(payload, dict):
        raise ValueError("Payload QuizBot invalide.")
    session_id = (payload.get("session_id") or "").strip()
    rating = normalize_rating(payload.get("rating"))
    comment = trim_text(payload.get("comment"), 2000)
    if not session_id:
        raise ValueError("Session QuizBot obligatoire.")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                UPDATE public.quizbot_session
                SET status = 'rated',
                    rating = %s,
                    comment = %s,
                    updated_at = now(),
                    completed_at = now()
                WHERE session_id = %s;
                """,
                (rating, comment, session_id),
            )
            if cur.rowcount == 0:
                raise ValueError("Session QuizBot introuvable.")
            record_quizbot_audit(
                cur,
                "session_rated",
                actor="public",
                session_id=session_id,
                details={"rating": rating, "has_comment": bool(comment)},
            )
        conn.commit()
    return {"session_id": session_id, "status": "rated", "created_at": now_iso()}


def short_text(value, max_length=120):
    """Return a short display version of text."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


def get_quizbot_dashboard():
    """Return administration dashboard metrics."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                SELECT
                    COUNT(*)::int,
                    COUNT(*) FILTER (WHERE is_correct = true)::int,
                    COUNT(*) FILTER (WHERE is_correct IS NOT NULL)::int,
                    COUNT(*) FILTER (WHERE status = 'error' OR error_message IS NOT NULL)::int,
                    AVG(COALESCE(generation_duration_ms, 0) + COALESCE(correction_duration_ms, 0))
                        FILTER (
                            WHERE generation_duration_ms IS NOT NULL
                               OR correction_duration_ms IS NOT NULL
                        )
                FROM public.quizbot_session;
                """
            )
            stats_row = cur.fetchone()
            cur.execute(
                """
                SELECT rating, COUNT(*)::int
                FROM public.quizbot_session
                WHERE rating IS NOT NULL
                GROUP BY rating;
                """
            )
            rating_rows = cur.fetchall()
            cur.execute(
                """
                SELECT COALESCE(topic_label, '-'), COUNT(*)::int
                FROM public.quizbot_session
                GROUP BY COALESCE(topic_label, '-')
                ORDER BY COUNT(*) DESC, COALESCE(topic_label, '-') ASC
                LIMIT 5;
                """
            )
            most_rows = cur.fetchall()
            cur.execute(
                """
                SELECT COALESCE(topic_label, '-'), COUNT(*)::int
                FROM public.quizbot_session
                GROUP BY COALESCE(topic_label, '-')
                ORDER BY COUNT(*) ASC, COALESCE(topic_label, '-') ASC
                LIMIT 5;
                """
            )
            least_rows = cur.fetchall()
            cur.execute(
                """
                SELECT COUNT(*)::int
                FROM public.quizbot_topic
                WHERE active = true AND archived = false;
                """
            )
            active_topic_count = int(cur.fetchone()[0])
        conn.commit()

    total_sessions = int(stats_row[0] or 0)
    correct_sessions = int(stats_row[1] or 0)
    answered_sessions = int(stats_row[2] or 0)
    success_rate = (
        round((correct_sessions / answered_sessions) * 100.0, 1)
        if answered_sessions
        else 0.0
    )
    rating_counts = {"good": 0, "neutral": 0, "bad": 0}
    for rating, count in rating_rows:
        if rating in rating_counts:
            rating_counts[rating] = int(count)
    return {
        "total_sessions": total_sessions,
        "answered_sessions": answered_sessions,
        "correct_sessions": correct_sessions,
        "success_rate": success_rate,
        "rating_counts": rating_counts,
        "most_played_topics": [{"label": row[0], "count": int(row[1])} for row in most_rows],
        "least_played_topics": [{"label": row[0], "count": int(row[1])} for row in least_rows],
        "llm_error_count": int(stats_row[3] or 0),
        "avg_llm_response_ms": int(stats_row[4] or 0) if stats_row[4] is not None else 0,
        "active_topic_count": active_topic_count,
    }


def list_quizbot_sessions(limit=100):
    """List recent QuizBot sessions for administration."""
    safe_limit = min(max(int(limit or 100), 1), 500)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                SELECT session_id, topic_label, status, question_text, user_answer,
                       is_correct, rating, error_message, question_model,
                       correction_model, generation_duration_ms, correction_duration_ms,
                       created_at, updated_at, completed_at
                FROM public.quizbot_session
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()
        conn.commit()
    return [
        {
            "session_id": row[0],
            "topic_label": row[1] or "",
            "status": row[2],
            "question_preview": short_text(row[3], 140),
            "answer_preview": short_text(row[4], 100),
            "is_correct": row[5],
            "rating": row[6] or "",
            "error_message": row[7] or "",
            "question_model": row[8] or "",
            "correction_model": row[9] or "",
            "llm_duration_ms": int((row[10] or 0) + (row[11] or 0)),
            "created_at": row[12].isoformat(timespec="seconds") if row[12] else "",
            "updated_at": row[13].isoformat(timespec="seconds") if row[13] else "",
            "completed_at": row[14].isoformat(timespec="seconds") if row[14] else "",
        }
        for row in rows
    ]


def get_quizbot_session_detail(session_id):
    """Return one QuizBot session detail."""
    requested_session_id = (session_id or "").strip()
    if not requested_session_id:
        raise ValueError("Session QuizBot introuvable.")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            session_row = load_quizbot_session(cur, requested_session_id)
            cur.execute(
                """
                SELECT event_id, actor, event_type, details, created_at
                FROM public.quizbot_audit_event
                WHERE session_id = %s
                ORDER BY created_at ASC;
                """,
                (requested_session_id,),
            )
            audit_rows = cur.fetchall()
        conn.commit()

    correction = session_row["correction"] or {}
    return {
        **session_row,
        "created_at": session_row["created_at"].isoformat(timespec="seconds") if session_row["created_at"] else "",
        "updated_at": session_row["updated_at"].isoformat(timespec="seconds") if session_row["updated_at"] else "",
        "completed_at": session_row["completed_at"].isoformat(timespec="seconds") if session_row["completed_at"] else "",
        "correction_json": json.dumps(correction, ensure_ascii=False, indent=2),
        "llm_duration_ms": int(
            (session_row["generation_duration_ms"] or 0)
            + (session_row["correction_duration_ms"] or 0)
        ),
        "audit_events": [
            {
                "event_id": row[0],
                "actor": row[1],
                "event_type": row[2],
                "details": row[3] or {},
                "details_json": json.dumps(row[3] or {}, ensure_ascii=False, indent=2),
                "created_at": row[4].isoformat(timespec="seconds") if row[4] else "",
            }
            for row in audit_rows
        ],
    }


def list_quizbot_audit_events(limit=200):
    """List recent QuizBot audit events."""
    safe_limit = min(max(int(limit or 200), 1), 1000)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_quizbot_tables(cur)
            cur.execute(
                """
                SELECT event_id, session_id, actor, event_type, details, created_at
                FROM public.quizbot_audit_event
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()
        conn.commit()
    return [
        {
            "event_id": row[0],
            "session_id": row[1] or "",
            "actor": row[2],
            "event_type": row[3],
            "details": row[4] or {},
            "details_json": json.dumps(row[4] or {}, ensure_ascii=False, indent=2),
            "created_at": row[5].isoformat(timespec="seconds") if row[5] else "",
        }
        for row in rows
    ]
