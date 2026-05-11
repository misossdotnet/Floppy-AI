"""Project-linked document vision/OCR analysis using configured LLMs."""

import base64
import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from uuid import uuid4

from psycopg2.extras import Json

from db import get_db_connection
from llm_gateway import (
    call_llm_chat_completion_with_config,
    effective_llm_config,
    extract_chat_completion_content,
    normalize_config_id,
    normalize_max_tokens,
    normalize_retries,
)
from services import add_shard_record, find_project_by_slug, shorten_text


DOCUMENT_VISION_CONFIG_TABLE = "document_vision_config"
DOCUMENT_VISION_RUN_TABLE = "document_vision_run"
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
DEFAULT_MAX_FILE_SIZE_MB = 20
DEFAULT_SYSTEM_PROMPT = """Tu es un moteur vision-langage specialise dans l'OCR et la comprehension de documents complexes.
Analyse le document fourni pour en extraire un contenu exploitable en indexation RAG.
Preserve la structure utile: titres, sections, tableaux, listes, libelles, valeurs, notes et avertissements.
Retourne uniquement un JSON valide."""
DEFAULT_EXTRACTION_PROMPT = """Analyse ce document image ou PDF.
Retourne uniquement un JSON valide avec cette forme:
{
  "title": "Titre du document",
  "document_type": "type de document",
  "language": "fr",
  "summary": "Resume court",
  "extracted_markdown": "# Titre\\n\\nContenu structure en Markdown",
  "sections": [
    {"title": "Section", "content": "Contenu interprete"}
  ],
  "tables": [
    {"title": "Tableau", "markdown": "| Colonne | Valeur |\\n| --- | --- |"}
  ],
  "entities": [
    {"label": "Nom", "value": "Valeur", "type": "categorie"}
  ],
  "rag_indexing_notes": [
    "Point important pour chunking ou recherche"
  ],
  "quality_warnings": [
    "Zone illisible, incertitude OCR ou ambiguite"
  ]
}
Le champ extracted_markdown doit contenir le contenu principal complet pret a etre transforme en shard."""


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


def normalize_text(value, limit=20000):
    """Normalize user/admin text values."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def normalize_temperature(raw_value, default=0.1):
    """Normalize a temperature value."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0.0), 2.0)


def normalize_max_file_size_mb(raw_value):
    """Normalize configured max upload size."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_FILE_SIZE_MB
    return min(max(value, 1), 100)


def ensure_document_vision_tables(cur):
    """Create document vision config and analysis history tables."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.document_vision_config (
            id integer PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            llm_config_id text NOT NULL DEFAULT '',
            temperature numeric(4, 2) NOT NULL DEFAULT 0.1,
            max_tokens integer NOT NULL DEFAULT 2500,
            max_file_size_mb integer NOT NULL DEFAULT 20,
            auto_create_shard boolean NOT NULL DEFAULT true,
            system_prompt text NOT NULL DEFAULT '',
            extraction_prompt text NOT NULL DEFAULT '',
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.document_vision_config ADD COLUMN IF NOT EXISTS llm_config_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.document_vision_config ADD COLUMN IF NOT EXISTS temperature numeric(4, 2) NOT NULL DEFAULT 0.1;")
    cur.execute("ALTER TABLE public.document_vision_config ADD COLUMN IF NOT EXISTS max_tokens integer NOT NULL DEFAULT 2500;")
    cur.execute("ALTER TABLE public.document_vision_config ADD COLUMN IF NOT EXISTS max_file_size_mb integer NOT NULL DEFAULT 20;")
    cur.execute("ALTER TABLE public.document_vision_config ADD COLUMN IF NOT EXISTS auto_create_shard boolean NOT NULL DEFAULT true;")
    cur.execute("ALTER TABLE public.document_vision_config ADD COLUMN IF NOT EXISTS system_prompt text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.document_vision_config ADD COLUMN IF NOT EXISTS extraction_prompt text NOT NULL DEFAULT '';")
    cur.execute(
        """
        INSERT INTO public.document_vision_config
            (
                id, enabled, llm_config_id, temperature, max_tokens,
                max_file_size_mb, auto_create_shard, system_prompt, extraction_prompt
            )
        VALUES (1, true, '', 0.1, 2500, 20, true, %s, %s)
        ON CONFLICT (id) DO NOTHING;
        """,
        (DEFAULT_SYSTEM_PROMPT, DEFAULT_EXTRACTION_PROMPT),
    )
    cur.execute(
        """
        UPDATE public.document_vision_config
        SET system_prompt = %s
        WHERE id = 1 AND (system_prompt IS NULL OR system_prompt = '');
        """,
        (DEFAULT_SYSTEM_PROMPT,),
    )
    cur.execute(
        """
        UPDATE public.document_vision_config
        SET extraction_prompt = %s
        WHERE id = 1 AND (extraction_prompt IS NULL OR extraction_prompt = '');
        """,
        (DEFAULT_EXTRACTION_PROMPT,),
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.document_vision_run (
            run_id text PRIMARY KEY,
            project_slug text NOT NULL,
            actor text NOT NULL DEFAULT '',
            filename text NOT NULL DEFAULT '',
            media_type text NOT NULL DEFAULT '',
            file_size integer NOT NULL DEFAULT 0,
            file_sha256 text NOT NULL DEFAULT '',
            prompt_text text NOT NULL DEFAULT '',
            analysis_result jsonb NOT NULL DEFAULT '{}'::jsonb,
            extracted_markdown text NOT NULL DEFAULT '',
            shard_id text NOT NULL DEFAULT '',
            audit_session_id text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'completed',
            error_message text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS document_vision_run_project_created_idx
        ON public.document_vision_run(project_slug, created_at DESC);
        """
    )


def serialize_config(row):
    """Serialize one config row."""
    return {
        "enabled": bool(row[0]) if row else True,
        "llm_config_id": row[1] if row and row[1] else "",
        "temperature": float(row[2]) if row else 0.1,
        "max_tokens": normalize_max_tokens(row[3] if row else 2500, default=2500),
        "max_file_size_mb": normalize_max_file_size_mb(row[4] if row else DEFAULT_MAX_FILE_SIZE_MB),
        "auto_create_shard": bool(row[5]) if row else True,
        "system_prompt": row[6] if row and row[6] else DEFAULT_SYSTEM_PROMPT,
        "extraction_prompt": row[7] if row and row[7] else DEFAULT_EXTRACTION_PROMPT,
        "updated_at": row[8].isoformat(timespec="seconds") if row and row[8] else "",
    }


def get_document_vision_config():
    """Return persisted Document Vision configuration."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_document_vision_tables(cur)
            cur.execute(
                """
                SELECT enabled, llm_config_id, temperature, max_tokens,
                       max_file_size_mb, auto_create_shard,
                       system_prompt, extraction_prompt, updated_at
                FROM public.document_vision_config
                WHERE id = 1;
                """
            )
            row = cur.fetchone()
        conn.commit()
    return serialize_config(row)


def save_document_vision_config(payload):
    """Persist Document Vision configuration."""
    enabled = normalize_bool(payload.get("enabled"), default=False)
    llm_config_id = normalize_config_id(payload.get("llm_config_id"))
    temperature = normalize_temperature(payload.get("temperature"), default=0.1)
    max_tokens = normalize_max_tokens(payload.get("max_tokens"), default=2500)
    max_file_size_mb = normalize_max_file_size_mb(payload.get("max_file_size_mb"))
    auto_create_shard = normalize_bool(payload.get("auto_create_shard"), default=False)
    system_prompt = normalize_text(payload.get("system_prompt"), limit=10000)
    extraction_prompt = normalize_text(payload.get("extraction_prompt"), limit=12000)
    if not system_prompt:
        raise ValueError("Le prompt systeme Document Vision est obligatoire.")
    if not extraction_prompt:
        raise ValueError("Le prompt d'extraction Document Vision est obligatoire.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_document_vision_tables(cur)
            cur.execute(
                """
                UPDATE public.document_vision_config
                SET enabled = %s,
                    llm_config_id = %s,
                    temperature = %s,
                    max_tokens = %s,
                    max_file_size_mb = %s,
                    auto_create_shard = %s,
                    system_prompt = %s,
                    extraction_prompt = %s,
                    updated_at = now()
                WHERE id = 1;
                """,
                (
                    enabled,
                    llm_config_id,
                    temperature,
                    max_tokens,
                    max_file_size_mb,
                    auto_create_shard,
                    system_prompt,
                    extraction_prompt,
                ),
            )
        conn.commit()
    return get_document_vision_config()


def runtime_config_for_document_vision(config=None):
    """Resolve the LLM runtime configuration selected for Document Vision."""
    active_config = config or get_document_vision_config()
    return effective_llm_config(
        redact_key=False,
        config_id=active_config.get("llm_config_id", ""),
    )


def document_vision_status(config=None):
    """Return module availability status."""
    active_config = config or get_document_vision_config()
    try:
        runtime = runtime_config_for_document_vision(active_config)
        db_error = ""
    except Exception as exc:
        runtime = {}
        db_error = str(exc)
    configured = bool(runtime.get("configured"))
    return {
        "enabled": bool(active_config.get("enabled")),
        "configured": configured,
        "available": bool(active_config.get("enabled") and configured and not db_error),
        "llm_config_id": runtime.get("config_id", ""),
        "llm_name": runtime.get("name", ""),
        "provider": runtime.get("provider", ""),
        "model": runtime.get("model", ""),
        "db_error": db_error,
    }


def require_document_vision_available(config=None):
    """Return config and runtime or raise a public-safe error."""
    active_config = config or get_document_vision_config()
    if not active_config.get("enabled"):
        raise ValueError("Document Vision est desactive.")
    runtime = runtime_config_for_document_vision(active_config)
    if not runtime.get("configured"):
        raise ValueError("Configuration LLM Document Vision incomplete.")
    return active_config, runtime


def detect_media_type(filename, uploaded_media_type):
    """Resolve and validate an upload media type."""
    media_type = (uploaded_media_type or "").split(";")[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename or "")
    guessed_type = (guessed or "").lower()
    if not media_type or media_type == "application/octet-stream":
        media_type = guessed_type
    elif media_type not in ALLOWED_MIME_TYPES and guessed_type in ALLOWED_MIME_TYPES:
        media_type = guessed_type
    if media_type not in ALLOWED_MIME_TYPES:
        raise ValueError("Format non supporte. Formats acceptes: PDF, PNG, JPEG, WEBP, GIF.")
    return media_type


def data_url_for_upload(file_bytes, media_type):
    """Build a data URL for a multimodal LLM payload."""
    encoded = base64.b64encode(file_bytes).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def build_multimodal_content(config, filename, media_type, file_bytes, extra_instructions):
    """Build OpenAI-compatible multimodal message content parts."""
    instruction = "\n\n".join(
        part
        for part in [
            config["extraction_prompt"],
            extra_instructions,
            f"Nom du fichier: {filename}",
            f"Type MIME: {media_type}",
        ]
        if part
    )
    data_url = data_url_for_upload(file_bytes, media_type)
    parts = [{"type": "text", "text": instruction}]
    if media_type.startswith("image/"):
        parts.append({"type": "image_url", "image_url": {"url": data_url}})
    else:
        parts.append(
            {
                "type": "file",
                "file": {
                    "filename": filename or "document.pdf",
                    "file_data": data_url,
                },
            }
        )
    return parts


def strip_json_fences(raw_content):
    """Remove common markdown fences around JSON."""
    cleaned = (raw_content or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_json_object(raw_content):
    """Parse a JSON object from a LLM response."""
    cleaned = strip_json_fences(raw_content)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    raise ValueError("La reponse Document Vision n'est pas un JSON objet valide.")


def normalize_text_list(value, limit=500):
    """Normalize a LLM field into a list of strings."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [normalize_text(item, limit=limit) for item in values if normalize_text(item, limit=limit)]


def normalize_analysis_result(parsed):
    """Normalize Document Vision JSON into a stable shape."""
    extracted_markdown = normalize_text(parsed.get("extracted_markdown"), limit=200000)
    if not extracted_markdown:
        sections = parsed.get("sections", [])
        if isinstance(sections, list):
            lines = []
            for section in sections:
                if isinstance(section, dict):
                    title = normalize_text(section.get("title"), limit=200)
                    content = normalize_text(section.get("content"), limit=10000)
                    if title:
                        lines.append(f"## {title}")
                    if content:
                        lines.append(content)
            extracted_markdown = "\n\n".join(lines).strip()
    if not extracted_markdown:
        raise ValueError("Le LLM n'a pas retourne de contenu Markdown exploitable.")

    return {
        "title": normalize_text(parsed.get("title"), limit=240),
        "document_type": normalize_text(parsed.get("document_type"), limit=120),
        "language": normalize_text(parsed.get("language"), limit=40),
        "summary": normalize_text(parsed.get("summary"), limit=2000),
        "extracted_markdown": extracted_markdown,
        "sections": parsed.get("sections") if isinstance(parsed.get("sections"), list) else [],
        "tables": parsed.get("tables") if isinstance(parsed.get("tables"), list) else [],
        "entities": parsed.get("entities") if isinstance(parsed.get("entities"), list) else [],
        "rag_indexing_notes": normalize_text_list(parsed.get("rag_indexing_notes"), limit=500),
        "quality_warnings": normalize_text_list(parsed.get("quality_warnings"), limit=500),
        "raw_json": parsed,
    }


def should_retry_without_json_mode(exc):
    """Return whether a LLM error is likely caused by JSON mode support."""
    message = str(exc or "").lower()
    return any(
        marker in message
        for marker in ("response_format", "json mode", "json_schema", "unsupported", "not support", "400")
    )


def call_document_vision_llm(config, runtime, messages, metadata):
    """Call a multimodal LLM, retrying without JSON mode if required."""
    try:
        return call_llm_chat_completion_with_config(
            runtime,
            messages,
            purpose="document_vision_analysis",
            metadata=metadata,
            temperature=config["temperature"],
            max_tokens=config["max_tokens"],
            retries=normalize_retries(runtime.get("retries")),
            json_mode=True,
        )
    except ValueError as exc:
        if not should_retry_without_json_mode(exc):
            raise
        fallback_metadata = dict(metadata or {})
        fallback_metadata["json_mode_fallback"] = True
        return call_llm_chat_completion_with_config(
            runtime,
            messages,
            purpose="document_vision_analysis",
            metadata=fallback_metadata,
            temperature=config["temperature"],
            max_tokens=config["max_tokens"],
            retries=1,
            json_mode=False,
        )


def create_shard_content(analysis, filename):
    """Build the shard Markdown payload from analysis data."""
    lines = []
    title = analysis.get("title") or filename or "Document Vision"
    lines.append(f"# {title}")
    if analysis.get("summary"):
        lines.append(f"\n## Resume\n\n{analysis['summary']}")
    if analysis.get("document_type") or analysis.get("language"):
        metadata = []
        if analysis.get("document_type"):
            metadata.append(f"- Type: {analysis['document_type']}")
        if analysis.get("language"):
            metadata.append(f"- Langue: {analysis['language']}")
        lines.append("\n## Metadata document\n\n" + "\n".join(metadata))
    lines.append("\n## Contenu extrait\n\n" + analysis["extracted_markdown"])
    if analysis.get("rag_indexing_notes"):
        lines.append("\n## Notes RAG\n\n" + "\n".join(f"- {item}" for item in analysis["rag_indexing_notes"]))
    if analysis.get("quality_warnings"):
        lines.append("\n## Avertissements qualite\n\n" + "\n".join(f"- {item}" for item in analysis["quality_warnings"]))
    return "\n".join(lines).strip()


def store_document_vision_run(
    project_slug,
    actor,
    filename,
    media_type,
    file_bytes,
    prompt_text,
    analysis,
    shard_id,
    audit_session_id,
    status="completed",
    error_message="",
):
    """Persist one Document Vision analysis run."""
    run_id = uuid4().hex
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_document_vision_tables(cur)
            cur.execute(
                """
                INSERT INTO public.document_vision_run
                    (
                        run_id, project_slug, actor, filename, media_type,
                        file_size, file_sha256, prompt_text, analysis_result,
                        extracted_markdown, shard_id, audit_session_id, status,
                        error_message
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    run_id,
                    project_slug,
                    actor or "",
                    filename or "",
                    media_type or "",
                    len(file_bytes or b""),
                    hashlib.sha256(file_bytes or b"").hexdigest(),
                    prompt_text or "",
                    Json(analysis or {}),
                    (analysis or {}).get("extracted_markdown", ""),
                    shard_id or "",
                    audit_session_id or "",
                    status,
                    error_message or "",
                ),
            )
        conn.commit()
    return run_id


def analyze_project_document(project_slug, uploaded_file, payload, actor="admin"):
    """Analyze one uploaded image/PDF and optionally create a project shard."""
    if not uploaded_file or not uploaded_file.filename:
        raise ValueError("Un fichier image ou PDF est obligatoire.")

    config, runtime = require_document_vision_available()
    filename = normalize_text(uploaded_file.filename, limit=260) or "document"
    media_type = detect_media_type(filename, uploaded_file.mimetype)
    file_bytes = uploaded_file.read()
    if not file_bytes:
        raise ValueError("Le fichier est vide.")
    max_size = int(config["max_file_size_mb"]) * 1024 * 1024
    if len(file_bytes) > max_size:
        raise ValueError(f"Fichier trop volumineux. Limite: {config['max_file_size_mb']} Mo.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            find_project_by_slug(cur, project_slug)

    extra_instructions = normalize_text(payload.get("extra_instructions"), limit=4000)
    messages = [
        {"role": "system", "content": config["system_prompt"]},
        {
            "role": "user",
            "content": build_multimodal_content(
                config,
                filename,
                media_type,
                file_bytes,
                extra_instructions,
            ),
        },
    ]
    metadata = {
        "project_slug": project_slug,
        "actor": actor,
        "filename": filename,
        "media_type": media_type,
        "file_size": len(file_bytes),
        "file_sha256": hashlib.sha256(file_bytes).hexdigest(),
    }
    llm_payload = call_document_vision_llm(config, runtime, messages, metadata)
    content = extract_chat_completion_content(llm_payload)
    parsed = parse_json_object(content)
    analysis = normalize_analysis_result(parsed)
    analysis["audit_session_id"] = llm_payload.get("_audit_session_id", "")
    analysis["generated_at"] = now_iso()
    analysis["filename"] = filename
    analysis["media_type"] = media_type

    should_create_shard = normalize_bool(
        payload.get("create_shard"),
        default=bool(config.get("auto_create_shard")),
    )
    shard_id = ""
    if should_create_shard:
        shard_title = normalize_text(payload.get("title_document"), limit=240) or analysis.get("title") or filename
        shard_id = add_shard_record(
            project_slug,
            {
                "source_document": "document_vision",
                "url_document": f"upload://{filename}",
                "title_document": shard_title,
                "content_document": create_shard_content(analysis, filename),
                "autor_document": actor or "document_vision",
            },
        )

    run_id = store_document_vision_run(
        project_slug,
        actor,
        filename,
        media_type,
        file_bytes,
        extra_instructions,
        analysis,
        shard_id,
        analysis["audit_session_id"],
    )
    analysis["run_id"] = run_id
    analysis["shard_id"] = shard_id
    return analysis


def serialize_run(row):
    """Serialize one Document Vision run row."""
    analysis = row[8] or {}
    return {
        "run_id": row[0],
        "project_slug": row[1],
        "actor": row[2] or "",
        "filename": row[3] or "",
        "media_type": row[4] or "",
        "file_size": int(row[5] or 0),
        "status": row[6] or "",
        "shard_id": row[7] or "",
        "title": analysis.get("title", "") if isinstance(analysis, dict) else "",
        "summary": analysis.get("summary", "") if isinstance(analysis, dict) else "",
        "audit_session_id": row[9] or "",
        "created_at": row[10].isoformat(timespec="seconds") if row[10] else "",
        "preview": shorten_text(row[11] or "", 220),
    }


def list_document_vision_runs(project_slug, limit=25):
    """List recent Document Vision analyses for a project."""
    safe_limit = min(max(int(limit or 25), 1), 200)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_document_vision_tables(cur)
            cur.execute(
                """
                SELECT run_id, project_slug, actor, filename, media_type, file_size,
                       status, shard_id, analysis_result, audit_session_id,
                       created_at, extracted_markdown
                FROM public.document_vision_run
                WHERE project_slug = %s
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (project_slug, safe_limit),
            )
            rows = cur.fetchall()
        conn.commit()
    return [serialize_run(row) for row in rows]


def get_document_vision_project_payload(project_slug):
    """Return page payload for a project Document Vision screen."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
    config = get_document_vision_config()
    return {
        "project": project,
        "config": config,
        "status": document_vision_status(config),
        "runs": list_document_vision_runs(project_slug),
        "allowed_mime_types": sorted(ALLOWED_MIME_TYPES),
    }
