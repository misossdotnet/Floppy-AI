"""pgvector schema, embedding configuration, and vectorization helpers."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from psycopg2 import sql

from db import get_db_connection
from llm_gateway import (
    effective_llm_config,
    headers_for_config,
    is_ollama_native_config,
    list_llm_configs,
    normalize_checkbox,
    normalize_config_id,
    normalize_timeout,
)
from services import (
    ensure_project_tables_exist,
    ensure_project_vector_schema,
    find_project_by_slug,
    list_project_slugs,
    table_exists,
)


VECTOR_CONFIG_TABLE = "vectorization_config"
DEFAULT_VECTOR_CONFIG_ID = "default"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
DEFAULT_BATCH_SIZE = 25
TARGET_TYPES = {"shard", "chunk", "train"}


def parse_int(value, default: int, minimum: int, maximum: int) -> int:
    """Parse a bounded integer value."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def normalize_embedding_dimensions(value) -> int:
    """Normalize expected dimensions; zero means detect from the provider."""
    if value is None or str(value).strip().lower() in {"", "0", "auto"}:
        return 0
    return parse_int(value, DEFAULT_EMBEDDING_DIMENSIONS, 1, 16000)


def normalize_batch_size(value) -> int:
    """Normalize vectorization batch size."""
    return parse_int(value, DEFAULT_BATCH_SIZE, 1, 500)


def derive_embedding_api_url(api_url: str) -> str:
    """Derive a likely embeddings endpoint from a chat endpoint."""
    raw_url = str(api_url or "").strip()
    if not raw_url:
        return ""
    parsed = urllib.parse.urlparse(raw_url)
    path = parsed.path.rstrip("/")
    replacements = (
        ("/v1/chat/completions", "/v1/embeddings"),
        ("/chat/completions", "/embeddings"),
        ("/api/chat", "/api/embed"),
        ("/api/generate", "/api/embed"),
        ("/responses", "/embeddings"),
        ("/completions", "/embeddings"),
    )
    for suffix, replacement in replacements:
        if path.endswith(suffix):
            path = path[: -len(suffix)] + replacement
            return urllib.parse.urlunparse(parsed._replace(path=path, query="", fragment=""))
    path = f"{path}/embeddings" if path else "/v1/embeddings"
    return urllib.parse.urlunparse(parsed._replace(path=path, query="", fragment=""))


def ensure_vectorization_tables(cur):
    """Ensure the singleton vectorization configuration table exists."""
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS public.{VECTOR_CONFIG_TABLE} (
            config_id text PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT false,
            llm_config_id text NOT NULL DEFAULT '',
            embedding_api_url text NOT NULL DEFAULT '',
            embedding_model text NOT NULL DEFAULT '',
            embedding_dimensions integer NOT NULL DEFAULT {DEFAULT_EMBEDDING_DIMENSIONS},
            batch_size integer NOT NULL DEFAULT {DEFAULT_BATCH_SIZE},
            notes text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        f"ALTER TABLE public.{VECTOR_CONFIG_TABLE} ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT false;"
    )
    cur.execute(
        f"ALTER TABLE public.{VECTOR_CONFIG_TABLE} ADD COLUMN IF NOT EXISTS llm_config_id text NOT NULL DEFAULT '';"
    )
    cur.execute(
        f"ALTER TABLE public.{VECTOR_CONFIG_TABLE} ADD COLUMN IF NOT EXISTS embedding_api_url text NOT NULL DEFAULT '';"
    )
    cur.execute(
        f"ALTER TABLE public.{VECTOR_CONFIG_TABLE} ADD COLUMN IF NOT EXISTS embedding_model text NOT NULL DEFAULT '';"
    )
    cur.execute(
        f"ALTER TABLE public.{VECTOR_CONFIG_TABLE} ADD COLUMN IF NOT EXISTS embedding_dimensions integer NOT NULL DEFAULT {DEFAULT_EMBEDDING_DIMENSIONS};"
    )
    cur.execute(
        f"ALTER TABLE public.{VECTOR_CONFIG_TABLE} ADD COLUMN IF NOT EXISTS batch_size integer NOT NULL DEFAULT {DEFAULT_BATCH_SIZE};"
    )
    cur.execute(
        f"ALTER TABLE public.{VECTOR_CONFIG_TABLE} ADD COLUMN IF NOT EXISTS notes text NOT NULL DEFAULT '';"
    )


def env_vectorization_config():
    """Return vectorization configuration from environment variables."""
    return {
        "config_id": DEFAULT_VECTOR_CONFIG_ID,
        "enabled": normalize_checkbox({"enabled": os.getenv("VECTOR_ENABLED")}, "enabled", default=False),
        "llm_config_id": normalize_config_id(os.getenv("VECTOR_LLM_CONFIG_ID")),
        "embedding_api_url": str(os.getenv("VECTOR_EMBEDDING_API_URL") or "").strip(),
        "embedding_model": str(os.getenv("VECTOR_EMBEDDING_MODEL") or "").strip(),
        "embedding_dimensions": normalize_embedding_dimensions(os.getenv("VECTOR_EMBEDDING_DIMENSIONS")),
        "batch_size": normalize_batch_size(os.getenv("VECTOR_BATCH_SIZE")),
        "notes": "",
        "source": "environment",
        "created_at": None,
        "updated_at": None,
    }


def embedding_profile_configs(redact_key=True):
    """Return enabled, usable LLM configurations dedicated to embeddings."""
    return [
        config
        for config in list_llm_configs(redact_key=redact_key)
        if config.get("enabled")
        and config.get("configured")
        and config.get("profile_type") == "embedding"
    ]


def fallback_vectorization_config():
    """Resolve environment settings or one unambiguous embedding profile."""
    env_config = env_vectorization_config()
    has_explicit_environment = bool(
        env_config.get("enabled")
        or env_config.get("llm_config_id")
        or env_config.get("embedding_api_url")
        or env_config.get("embedding_model")
    )
    if has_explicit_environment:
        return env_config

    candidates = embedding_profile_configs(redact_key=True)
    if len(candidates) == 1:
        selected = candidates[0]
        return {
            **env_config,
            "enabled": True,
            "llm_config_id": selected["config_id"],
            "embedding_dimensions": 0,
            "source": "llm_profile",
            "auto_selection_error": "",
        }

    error = ""
    if len(candidates) > 1:
        error = (
            "Plusieurs configurations LLM actives ont le profil embedding; "
            "selectionnez-en une explicitement."
        )
    return {**env_config, "auto_selection_error": error}


def serialize_vector_config_row(row):
    """Serialize one vectorization config row."""
    if not row:
        return env_vectorization_config()
    return {
        "config_id": row[0],
        "enabled": bool(row[1]),
        "llm_config_id": row[2] or "",
        "embedding_api_url": row[3] or "",
        "embedding_model": row[4] or "",
        "embedding_dimensions": normalize_embedding_dimensions(row[5]),
        "batch_size": normalize_batch_size(row[6]),
        "notes": row[7] or "",
        "created_at": row[8].isoformat(timespec="seconds") if row[8] else None,
        "updated_at": row[9].isoformat(timespec="seconds") if row[9] else None,
        "source": "database",
    }


def get_vectorization_config():
    """Load the active vectorization configuration."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_vectorization_tables(cur)
                cur.execute(
                    f"""
                    SELECT config_id, enabled, llm_config_id, embedding_api_url,
                           embedding_model, embedding_dimensions, batch_size,
                           notes, created_at, updated_at
                    FROM public.{VECTOR_CONFIG_TABLE}
                    WHERE config_id = %s;
                    """,
                    (DEFAULT_VECTOR_CONFIG_ID,),
                )
                row = cur.fetchone()
            conn.commit()
        return serialize_vector_config_row(row) if row else fallback_vectorization_config()
    except Exception:
        return env_vectorization_config()


def save_vectorization_config(payload):
    """Persist the singleton vectorization configuration."""
    llm_config_id = normalize_config_id(payload.get("llm_config_id"))
    embedding_api_url = str(payload.get("embedding_api_url") or "").strip()
    embedding_model = str(payload.get("embedding_model") or "").strip()
    embedding_dimensions = normalize_embedding_dimensions(payload.get("embedding_dimensions"))
    batch_size = normalize_batch_size(payload.get("batch_size"))
    enabled = normalize_checkbox(payload, "enabled", default=False)
    notes = str(payload.get("notes") or "").strip()

    if enabled and not llm_config_id:
        raise ValueError("Selectionnez une configuration LLM pour la vectorisation.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_vectorization_tables(cur)
            cur.execute(
                f"""
                INSERT INTO public.{VECTOR_CONFIG_TABLE} (
                    config_id,
                    enabled,
                    llm_config_id,
                    embedding_api_url,
                    embedding_model,
                    embedding_dimensions,
                    batch_size,
                    notes,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (config_id)
                DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    llm_config_id = EXCLUDED.llm_config_id,
                    embedding_api_url = EXCLUDED.embedding_api_url,
                    embedding_model = EXCLUDED.embedding_model,
                    embedding_dimensions = EXCLUDED.embedding_dimensions,
                    batch_size = EXCLUDED.batch_size,
                    notes = EXCLUDED.notes,
                    updated_at = now();
                """,
                (
                    DEFAULT_VECTOR_CONFIG_ID,
                    enabled,
                    llm_config_id,
                    embedding_api_url,
                    embedding_model,
                    embedding_dimensions,
                    batch_size,
                    notes,
                ),
            )
        conn.commit()
    return get_vectorization_config()


def resolve_vectorization_runtime_config():
    """Resolve vectorization config and selected LLM config into runtime values."""
    vector_config = get_vectorization_config()
    llm_config = effective_llm_config(
        redact_key=False,
        config_id=vector_config.get("llm_config_id", ""),
    )
    embedding_api_url = vector_config.get("embedding_api_url") or derive_embedding_api_url(
        llm_config.get("api_url", "")
    )
    embedding_model = vector_config.get("embedding_model") or llm_config.get("model", "")
    runtime_config = {
        **llm_config,
        "api_url": embedding_api_url,
        "model": embedding_model,
        "timeout_seconds": normalize_timeout(llm_config.get("timeout_seconds"), default=90),
        "vector_config": vector_config,
        "configured": bool(vector_config.get("enabled") and embedding_api_url and embedding_model),
    }
    return runtime_config


def vectorization_status():
    """Return vectorization configuration status for the admin UI."""
    config = get_vectorization_config()
    runtime = resolve_vectorization_runtime_config()
    return {
        "enabled": bool(config.get("enabled")),
        "configured": bool(runtime.get("configured")),
        "llm_config_id": config.get("llm_config_id", ""),
        "embedding_api_url": runtime.get("api_url", ""),
        "embedding_model": runtime.get("model", ""),
        "embedding_dimensions": config.get("embedding_dimensions"),
        "batch_size": config.get("batch_size"),
        "source": config.get("source", ""),
        "error": (
            ""
            if runtime.get("configured")
            else config.get("auto_selection_error")
            or "Configuration vectorisation incomplete ou inactive."
        ),
    }


def build_embedding_request_payload(runtime_config, text: str):
    """Build provider-specific embedding request payload."""
    api_path = urllib.parse.urlparse(runtime_config.get("api_url", "")).path
    model = runtime_config.get("model", "")
    if api_path.endswith("/api/embed"):
        return {"model": model, "input": text}
    if api_path.endswith("/api/embeddings") or is_ollama_native_config(runtime_config):
        return {"model": model, "prompt": text}
    return {"model": model, "input": text}


def parse_embedding_response(raw_response):
    """Extract an embedding vector from common provider response formats."""
    if isinstance(raw_response, dict):
        data = raw_response.get("data")
        if isinstance(data, list) and data:
            first_item = data[0]
            if isinstance(first_item, dict) and isinstance(first_item.get("embedding"), list):
                return first_item["embedding"]
        embeddings = raw_response.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            first_embedding = embeddings[0]
            if isinstance(first_embedding, list):
                return first_embedding
        embedding = raw_response.get("embedding")
        if isinstance(embedding, list):
            return embedding
    raise ValueError("La reponse embedding ne contient pas de vecteur exploitable.")


def normalize_embedding_vector(raw_embedding):
    """Validate and normalize an embedding vector."""
    if not isinstance(raw_embedding, list) or not raw_embedding:
        raise ValueError("Embedding vide ou invalide.")
    vector = []
    for value in raw_embedding:
        try:
            vector.append(float(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("Embedding non numerique.") from exc
    return vector


def embedding_to_pgvector_literal(embedding):
    """Serialize an embedding vector for PostgreSQL pgvector input."""
    return "[" + ",".join(f"{value:.9g}" for value in embedding) + "]"


def execute_embedding_request(runtime_config, text: str):
    """Call the configured embedding endpoint and return a vector."""
    request_payload = build_embedding_request_payload(runtime_config, text)
    request = urllib.request.Request(
        runtime_config["api_url"],
        data=json.dumps(request_payload).encode("utf-8"),
        headers=headers_for_config(runtime_config),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=runtime_config["timeout_seconds"]) as response:
        raw_response = json.loads(response.read().decode("utf-8"))
    return normalize_embedding_vector(parse_embedding_response(raw_response))


def validate_embedding_dimensions(embedding, expected_dimensions: int):
    """Validate embedding dimensions against config."""
    if expected_dimensions and len(embedding) != expected_dimensions:
        raise ValueError(
            f"Dimension embedding inattendue: {len(embedding)} au lieu de {expected_dimensions}."
        )


def test_vectorization_config(text: str = ""):
    """Run a single embedding request against the configured vectorizer."""
    runtime_config = resolve_vectorization_runtime_config()
    if not runtime_config.get("configured"):
        raise ValueError("Configuration vectorisation incomplete ou inactive.")
    sample_text = str(text or "").strip() or "Test de vectorisation Floppy-AI."
    embedding = execute_embedding_request(runtime_config, sample_text)
    expected_dimensions = runtime_config["vector_config"].get("embedding_dimensions") or len(embedding)
    validate_embedding_dimensions(embedding, expected_dimensions)
    return {
        "ok": True,
        "embedding_dimensions": len(embedding),
        "embedding_model": runtime_config.get("model", ""),
        "embedding_api_url": runtime_config.get("api_url", ""),
        "preview": embedding[:8],
    }


def test_embedding_llm_config(config, text: str = ""):
    """Test one embedding-profile LLM config without requiring a vector row."""
    runtime_config = {
        **config,
        "api_url": derive_embedding_api_url(config.get("api_url", "")),
        "model": config.get("model", ""),
        "timeout_seconds": normalize_timeout(config.get("timeout_seconds"), default=90),
    }
    if not runtime_config.get("api_url") or not runtime_config.get("model"):
        raise ValueError("Configuration embeddings incomplete ou inactive.")
    sample_text = str(text or "").strip() or "Test de vectorisation Floppy-AI."
    embedding = execute_embedding_request(runtime_config, sample_text)
    return {
        "ok": True,
        "embedding_dimensions": len(embedding),
        "embedding_model": runtime_config["model"],
        "embedding_api_url": runtime_config["api_url"],
        "preview": embedding[:8],
    }


def vector_text_for_row(target_type: str, row) -> str:
    """Build the text sent to the embedding endpoint for one row."""
    if target_type in {"shard", "chunk"}:
        parts = [
            row.get("title_document", ""),
            row.get("source_document", ""),
            row.get("url_document", ""),
            row.get("autor_document", ""),
            row.get("content_document", ""),
        ]
    else:
        parts = [
            row.get("system_content", ""),
            row.get("user_content", ""),
            row.get("assistant_content", ""),
            row.get("metatags", ""),
        ]
    return "\n\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


def select_vectorization_rows(cur, table_name: str, target_type: str, limit: int, missing_only: bool):
    """Select rows to vectorize for a project-scoped data table."""
    where_clause = sql.SQL("WHERE embedding IS NULL") if missing_only else sql.SQL("")
    if target_type in {"shard", "chunk"}:
        cur.execute(
            sql.SQL(
                """
                SELECT uuid, source_document, url_document, title_document, content_document, autor_document
                FROM {}
                {}
                ORDER BY uuid
                LIMIT %s;
                """
            ).format(sql.Identifier("public", table_name), where_clause),
            (limit,),
        )
        return [
            {
                "uuid": row[0],
                "source_document": row[1] or "",
                "url_document": row[2] or "",
                "title_document": row[3] or "",
                "content_document": row[4] or "",
                "autor_document": row[5] or "",
            }
            for row in cur.fetchall()
        ]

    cur.execute(
        sql.SQL(
            """
            SELECT uuid, system_content, user_content, assistant_content, metatags
            FROM {}
            {}
            ORDER BY uuid
            LIMIT %s;
            """
        ).format(sql.Identifier("public", table_name), where_clause),
        (limit,),
    )
    return [
        {
            "uuid": row[0],
            "system_content": row[1] or "",
            "user_content": row[2] or "",
            "assistant_content": row[3] or "",
            "metatags": row[4] or "",
        }
        for row in cur.fetchall()
    ]


def update_vector_success(cur, table_name: str, row_id: str, embedding, runtime_config):
    """Persist one successful embedding vector."""
    cur.execute(
        sql.SQL(
            """
            UPDATE {}
            SET embedding = %s::vector,
                embedding_model = %s,
                embedding_config_id = %s,
                embedding_dimensions = %s,
                embedding_status = 'embedded',
                embedding_error = NULL,
                embedding_updated_at = now()
            WHERE uuid = %s;
            """
        ).format(sql.Identifier("public", table_name)),
        (
            embedding_to_pgvector_literal(embedding),
            runtime_config.get("model", ""),
            runtime_config.get("vector_config", {}).get("llm_config_id", ""),
            len(embedding),
            row_id,
        ),
    )


def update_vector_error(cur, table_name: str, row_id: str, error_message: str):
    """Persist one vectorization error on a data row."""
    cur.execute(
        sql.SQL(
            """
            UPDATE {}
            SET embedding_status = 'error',
                embedding_error = %s,
                embedding_updated_at = now()
            WHERE uuid = %s;
            """
        ).format(sql.Identifier("public", table_name)),
        (error_message[:1000], row_id),
    )


def normalize_target_types(payload):
    """Normalize selected vectorization targets from form or API payload."""
    raw_targets = payload.get("targets") or payload.get("target_types") or []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    targets = [str(item).strip().lower() for item in raw_targets if str(item).strip()]
    selected = [item for item in targets if item in TARGET_TYPES]
    return selected or ["shard", "chunk", "train"]


def vectorize_project_data(project_slug: str, payload):
    """Vectorize shard/chunk/train rows for one project."""
    runtime_config = resolve_vectorization_runtime_config()
    if not runtime_config.get("configured"):
        raise ValueError("Configuration vectorisation incomplete ou inactive.")

    targets = normalize_target_types(payload)
    missing_only = normalize_checkbox(payload, "missing_only", default=True)
    limit = parse_int(payload.get("limit"), runtime_config["vector_config"].get("batch_size"), 1, 1000)
    expected_dimensions = runtime_config["vector_config"].get("embedding_dimensions") or 0

    processed = 0
    embedded = 0
    errors = []
    per_target = {}

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            table_names = ensure_project_tables_exist(cur, project["slug"], include_chat=False)
            ensure_project_vector_schema(cur, project["slug"])
            target_tables = {
                "shard": table_names["shard_table"],
                "chunk": table_names["chunk_table"],
                "train": table_names["train_table"],
            }

            for target_type in targets:
                remaining_limit = max(0, limit - processed)
                if remaining_limit <= 0:
                    break
                table_name = target_tables[target_type]
                rows = select_vectorization_rows(
                    cur,
                    table_name,
                    target_type,
                    remaining_limit,
                    missing_only=missing_only,
                )
                per_target[target_type] = {"selected": len(rows), "embedded": 0, "errors": 0}
                for row in rows:
                    processed += 1
                    row_text = vector_text_for_row(target_type, row)
                    if not row_text:
                        message = "Texte vide: vectorisation ignoree."
                        update_vector_error(cur, table_name, row["uuid"], message)
                        errors.append({"target": target_type, "uuid": row["uuid"], "error": message})
                        per_target[target_type]["errors"] += 1
                        continue
                    try:
                        embedding = execute_embedding_request(runtime_config, row_text)
                        if not expected_dimensions:
                            expected_dimensions = len(embedding)
                        validate_embedding_dimensions(embedding, expected_dimensions)
                    except Exception as exc:
                        message = str(exc) or exc.__class__.__name__
                        update_vector_error(cur, table_name, row["uuid"], message)
                        errors.append({"target": target_type, "uuid": row["uuid"], "error": message})
                        per_target[target_type]["errors"] += 1
                        continue
                    update_vector_success(cur, table_name, row["uuid"], embedding, runtime_config)
                    embedded += 1
                    per_target[target_type]["embedded"] += 1
        conn.commit()

    return {
        "project_slug": project_slug,
        "processed": processed,
        "embedded": embedded,
        "errors": errors,
        "per_target": per_target,
        "embedding_model": runtime_config.get("model", ""),
        "embedding_dimensions": expected_dimensions,
    }


def project_vector_table_status(cur, table_name: str):
    """Return vector status counters for one table."""
    if not table_exists(cur, table_name):
        return {"total": 0, "embedded": 0, "missing": 0, "errors": 0}
    cur.execute(
        sql.SQL(
            """
            SELECT
                COUNT(*)::int,
                COUNT(embedding)::int,
                COUNT(*) FILTER (WHERE embedding IS NULL)::int,
                COUNT(*) FILTER (WHERE embedding_status = 'error')::int
            FROM {};
            """
        ).format(sql.Identifier("public", table_name))
    )
    row = cur.fetchone()
    total = int(row[0] or 0)
    embedded = int(row[1] or 0)
    return {
        "total": total,
        "embedded": embedded,
        "missing": int(row[2] or 0),
        "errors": int(row[3] or 0),
    }


def list_project_vector_status():
    """Return per-project pgvector status for shard/chunk/train tables."""
    projects = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for project_slug in list_project_slugs(cur):
                project = find_project_by_slug(cur, project_slug)
                table_names = ensure_project_tables_exist(cur, project_slug, include_chat=False)
                ensure_project_vector_schema(cur, project_slug)
                projects.append(
                    {
                        "uuid": project["uuid"],
                        "name": project["name"],
                        "slug": project["slug"],
                        "shard_table": table_names["shard_table"],
                        "chunk_table": table_names["chunk_table"],
                        "train_table": table_names["train_table"],
                        "shard": project_vector_table_status(cur, table_names["shard_table"]),
                        "chunk": project_vector_table_status(cur, table_names["chunk_table"]),
                        "train": project_vector_table_status(cur, table_names["train_table"]),
                    }
                )
        conn.commit()
    return projects


def get_vectorization_admin_payload():
    """Build the admin vectorization payload."""
    config = get_vectorization_config()
    return {
        "config": config,
        "status": vectorization_status(),
        "llm_configs": embedding_profile_configs(redact_key=True),
        "projects": list_project_vector_status(),
    }
