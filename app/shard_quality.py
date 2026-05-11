"""Shard quality analysis module using configured LLMs and local metrics."""

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from psycopg2 import sql
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
from services import (
    ensure_project_tables_exist,
    find_project_by_slug,
    list_projects_shards,
    shorten_text,
)


SHARD_QUALITY_CONFIG_TABLE = "shard_quality_config"
SHARD_QUALITY_RUN_TABLE = "shard_quality_run"
DEFAULT_SYSTEM_PROMPT = """Tu es un analyste de qualite documentaire specialise dans les corpus RAG.
Tu evalues la coherence intellectuelle d'un shard Markdown.
Analyse strictement le contenu fourni selon ces piliers:
1. promesse et realisation du titre;
2. signalement de l'information importante: gras, italique, citations;
3. architecture des idees: intertitres, listes, progression logique;
4. presence, equilibre et profondeur des concepts fondamentaux.
Retourne uniquement un JSON valide, sans Markdown autour."""
DEFAULT_ANALYSIS_PROMPT = """Evalue le shard suivant.
Retourne uniquement un JSON valide avec cette forme:
{
  "overall_score": 0,
  "verdict": "court verdict",
  "summary": "synthese de la qualite du shard",
  "promise_realization": {
    "score": 0,
    "findings": ["constat"],
    "issues": ["probleme"],
    "recommendations": ["action"]
  },
  "important_information_signaling": {
    "score": 0,
    "findings": ["constat"],
    "issues": ["probleme"],
    "recommendations": ["action"]
  },
  "ideas_architecture": {
    "score": 0,
    "findings": ["constat"],
    "issues": ["probleme"],
    "recommendations": ["action"]
  },
  "fundamental_concepts": {
    "score": 0,
    "findings": ["constat"],
    "issues": ["probleme"],
    "recommendations": ["action"]
  },
  "key_concepts": ["concept"],
  "strengths": ["point fort"],
  "weaknesses": ["point faible"],
  "priority_actions": ["action prioritaire"],
  "rag_readiness": "pret|a_corriger|insuffisant",
  "chunking_advice": "conseil de decoupage"
}
Les scores doivent etre entre 0 et 100."""
STOPWORDS = {
    "alors",
    "avec",
    "avoir",
    "comme",
    "dans",
    "des",
    "donc",
    "elle",
    "elles",
    "entre",
    "est",
    "etre",
    "eux",
    "ils",
    "les",
    "leur",
    "leurs",
    "mais",
    "nous",
    "par",
    "pas",
    "plus",
    "pour",
    "que",
    "qui",
    "sont",
    "sur",
    "une",
    "vous",
    "the",
    "and",
    "for",
    "that",
    "with",
}


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


def normalize_max_input_chars(raw_value):
    """Normalize shard input size limit for LLM analysis."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 30000
    return min(max(value, 2000), 200000)


def clamp_score(value):
    """Clamp a score between 0 and 100."""
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = 0
    return min(max(score, 0), 100)


def ensure_shard_quality_tables(cur):
    """Create shard quality config and run tables."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.shard_quality_config (
            id integer PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            llm_config_id text NOT NULL DEFAULT '',
            temperature numeric(4, 2) NOT NULL DEFAULT 0.1,
            max_tokens integer NOT NULL DEFAULT 2200,
            max_input_chars integer NOT NULL DEFAULT 30000,
            system_prompt text NOT NULL DEFAULT '',
            analysis_prompt text NOT NULL DEFAULT '',
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.shard_quality_config ADD COLUMN IF NOT EXISTS llm_config_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.shard_quality_config ADD COLUMN IF NOT EXISTS temperature numeric(4, 2) NOT NULL DEFAULT 0.1;")
    cur.execute("ALTER TABLE public.shard_quality_config ADD COLUMN IF NOT EXISTS max_tokens integer NOT NULL DEFAULT 2200;")
    cur.execute("ALTER TABLE public.shard_quality_config ADD COLUMN IF NOT EXISTS max_input_chars integer NOT NULL DEFAULT 30000;")
    cur.execute("ALTER TABLE public.shard_quality_config ADD COLUMN IF NOT EXISTS system_prompt text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.shard_quality_config ADD COLUMN IF NOT EXISTS analysis_prompt text NOT NULL DEFAULT '';")
    cur.execute(
        """
        INSERT INTO public.shard_quality_config
            (id, enabled, llm_config_id, temperature, max_tokens, max_input_chars, system_prompt, analysis_prompt)
        VALUES (1, true, '', 0.1, 2200, 30000, %s, %s)
        ON CONFLICT (id) DO NOTHING;
        """,
        (DEFAULT_SYSTEM_PROMPT, DEFAULT_ANALYSIS_PROMPT),
    )
    cur.execute(
        """
        UPDATE public.shard_quality_config
        SET system_prompt = %s
        WHERE id = 1 AND (system_prompt IS NULL OR system_prompt = '');
        """,
        (DEFAULT_SYSTEM_PROMPT,),
    )
    cur.execute(
        """
        UPDATE public.shard_quality_config
        SET analysis_prompt = %s
        WHERE id = 1 AND (analysis_prompt IS NULL OR analysis_prompt = '');
        """,
        (DEFAULT_ANALYSIS_PROMPT,),
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.shard_quality_run (
            run_id text PRIMARY KEY,
            project_slug text NOT NULL,
            shard_id text NOT NULL,
            actor text NOT NULL DEFAULT '',
            title_document text NOT NULL DEFAULT '',
            content_sha256 text NOT NULL DEFAULT '',
            local_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            analysis_result jsonb NOT NULL DEFAULT '{}'::jsonb,
            overall_score integer NOT NULL DEFAULT 0,
            audit_session_id text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'completed',
            error_message text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS shard_quality_run_project_shard_created_idx
        ON public.shard_quality_run(project_slug, shard_id, created_at DESC);
        """
    )


def serialize_config(row):
    """Serialize one config row."""
    return {
        "enabled": bool(row[0]) if row else True,
        "llm_config_id": row[1] if row and row[1] else "",
        "temperature": float(row[2]) if row else 0.1,
        "max_tokens": normalize_max_tokens(row[3] if row else 2200, default=2200),
        "max_input_chars": normalize_max_input_chars(row[4] if row else 30000),
        "system_prompt": row[5] if row and row[5] else DEFAULT_SYSTEM_PROMPT,
        "analysis_prompt": row[6] if row and row[6] else DEFAULT_ANALYSIS_PROMPT,
        "updated_at": row[7].isoformat(timespec="seconds") if row and row[7] else "",
    }


def get_shard_quality_config():
    """Return persisted Shard Quality configuration."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_shard_quality_tables(cur)
            cur.execute(
                """
                SELECT enabled, llm_config_id, temperature, max_tokens,
                       max_input_chars, system_prompt, analysis_prompt, updated_at
                FROM public.shard_quality_config
                WHERE id = 1;
                """
            )
            row = cur.fetchone()
        conn.commit()
    return serialize_config(row)


def save_shard_quality_config(payload):
    """Persist Shard Quality configuration."""
    enabled = normalize_bool(payload.get("enabled"), default=False)
    llm_config_id = normalize_config_id(payload.get("llm_config_id"))
    temperature = normalize_temperature(payload.get("temperature"), default=0.1)
    max_tokens = normalize_max_tokens(payload.get("max_tokens"), default=2200)
    max_input_chars = normalize_max_input_chars(payload.get("max_input_chars"))
    system_prompt = normalize_text(payload.get("system_prompt"), limit=10000)
    analysis_prompt = normalize_text(payload.get("analysis_prompt"), limit=12000)
    if not system_prompt:
        raise ValueError("Le prompt systeme Shard Quality est obligatoire.")
    if not analysis_prompt:
        raise ValueError("Le prompt d'analyse Shard Quality est obligatoire.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_shard_quality_tables(cur)
            cur.execute(
                """
                UPDATE public.shard_quality_config
                SET enabled = %s,
                    llm_config_id = %s,
                    temperature = %s,
                    max_tokens = %s,
                    max_input_chars = %s,
                    system_prompt = %s,
                    analysis_prompt = %s,
                    updated_at = now()
                WHERE id = 1;
                """,
                (
                    enabled,
                    llm_config_id,
                    temperature,
                    max_tokens,
                    max_input_chars,
                    system_prompt,
                    analysis_prompt,
                ),
            )
        conn.commit()
    return get_shard_quality_config()


def runtime_config_for_shard_quality(config=None):
    """Resolve the LLM runtime configuration selected for Shard Quality."""
    active_config = config or get_shard_quality_config()
    return effective_llm_config(
        redact_key=False,
        config_id=active_config.get("llm_config_id", ""),
    )


def shard_quality_status(config=None):
    """Return module availability status."""
    active_config = config or get_shard_quality_config()
    try:
        runtime = runtime_config_for_shard_quality(active_config)
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


def require_shard_quality_available(config=None):
    """Return config and runtime or raise a public-safe error."""
    active_config = config or get_shard_quality_config()
    if not active_config.get("enabled"):
        raise ValueError("Shard Quality est desactive.")
    runtime = runtime_config_for_shard_quality(active_config)
    if not runtime.get("configured"):
        raise ValueError("Configuration LLM Shard Quality incomplete.")
    return active_config, runtime


def text_words(text):
    """Return normalized words from text."""
    return re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][A-Za-zÀ-ÖØ-öø-ÿ0-9_-]{2,}", text.lower())


def top_concepts(text, limit=12):
    """Compute a simple top concept list from content."""
    counts = {}
    for word in text_words(text):
        if word in STOPWORDS or len(word) < 4:
            continue
        counts[word] = counts.get(word, 0) + 1
    return [
        {"term": term, "count": count}
        for term, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def count_markdown_tables(text):
    """Count probable Markdown table separators."""
    return len(re.findall(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", text, flags=re.MULTILINE))


def compute_local_metrics(shard):
    """Compute deterministic structure metrics for one shard."""
    content = shard.get("content_document") or ""
    title = shard.get("title_document") or ""
    words = text_words(content)
    title_words = {
        word for word in text_words(title)
        if word not in STOPWORDS and len(word) >= 4
    }
    content_words = set(words)
    title_terms_found = sorted(title_words.intersection(content_words))
    title_coverage = (len(title_terms_found) / len(title_words)) if title_words else 0
    lines = content.splitlines()
    metrics = {
        "char_count": len(content),
        "word_count": len(words),
        "line_count": len(lines),
        "heading_count": len(re.findall(r"^\s{0,3}#{1,6}\s+\S+", content, flags=re.MULTILINE)),
        "bold_count": len(re.findall(r"(\*\*|__)(?=\S)(.+?[*_]*)(?<=\S)\1", content, flags=re.DOTALL)),
        "italic_count": len(re.findall(r"(?<!\*)\*(?!\*)(?=\S)(.+?)(?<=\S)\*(?!\*)", content, flags=re.DOTALL)),
        "quote_count": len(re.findall(r"^\s*>+\s+\S+", content, flags=re.MULTILINE)),
        "list_item_count": len(re.findall(r"^\s*(?:[-*+]|\d+\.)\s+\S+", content, flags=re.MULTILINE)),
        "table_count": count_markdown_tables(content),
        "title_word_count": len(title_words),
        "title_terms_found": title_terms_found,
        "title_coverage": round(title_coverage, 3),
        "top_concepts": top_concepts(content),
    }
    return metrics


def get_shard_record(project_slug, shard_id):
    """Return project and shard details."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            project = find_project_by_slug(cur, project_slug)
            table_names = ensure_project_tables_exist(cur, project_slug, include_chat=False)
            shard_table = table_names["shard_table"]
            chunk_table = table_names["chunk_table"]
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = %s
                );
                """,
                (chunk_table,),
            )
            chunk_table_exists = bool(cur.fetchone()[0])
            if chunk_table_exists:
                cur.execute(
                    sql.SQL(
                        """
                    SELECT
                        s.uuid, s.source_document, s.url_document,
                        s.title_document, s.content_document, s.autor_document,
                        COUNT(c.uuid)::int AS chunk_count
                    FROM {} AS s
                    LEFT JOIN {} AS c ON c.shard_id = s.uuid
                    WHERE s.uuid = %s
                    GROUP BY
                        s.uuid, s.source_document, s.url_document,
                        s.title_document, s.content_document, s.autor_document;
                    """
                    ).format(
                        sql.Identifier("public", shard_table),
                        sql.Identifier("public", chunk_table),
                    ),
                    (shard_id,),
                )
            else:
                cur.execute(
                    sql.SQL(
                        """
                    SELECT
                        uuid, source_document, url_document,
                        title_document, content_document, autor_document,
                        0 AS chunk_count
                    FROM {}
                    WHERE uuid = %s;
                    """
                    ).format(sql.Identifier("public", shard_table)),
                    (shard_id,),
                )
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise ValueError("Shard introuvable pour ce projet.")
    shard = {
        "uuid": row[0],
        "source_document": row[1] or "",
        "url_document": row[2] or "",
        "title_document": row[3] or "",
        "content_document": row[4] or "",
        "autor_document": row[5] or "",
        "chunk_count": int(row[6] or 0),
    }
    return project, shard


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
    raise ValueError("La reponse Shard Quality n'est pas un JSON objet valide.")


def normalize_text_list(value, limit=500):
    """Normalize a LLM field into a list of strings."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [normalize_text(item, limit=limit) for item in values if normalize_text(item, limit=limit)]


def normalize_axis(value):
    """Normalize a scoring axis returned by the LLM."""
    raw = value if isinstance(value, dict) else {}
    return {
        "score": clamp_score(raw.get("score")),
        "findings": normalize_text_list(raw.get("findings"), limit=600),
        "issues": normalize_text_list(raw.get("issues"), limit=600),
        "recommendations": normalize_text_list(raw.get("recommendations"), limit=600),
    }


def normalize_analysis(parsed):
    """Normalize Shard Quality JSON into a stable shape."""
    return {
        "overall_score": clamp_score(parsed.get("overall_score")),
        "verdict": normalize_text(parsed.get("verdict"), limit=300),
        "summary": normalize_text(parsed.get("summary"), limit=1800),
        "promise_realization": normalize_axis(parsed.get("promise_realization")),
        "important_information_signaling": normalize_axis(parsed.get("important_information_signaling")),
        "ideas_architecture": normalize_axis(parsed.get("ideas_architecture")),
        "fundamental_concepts": normalize_axis(parsed.get("fundamental_concepts")),
        "key_concepts": normalize_text_list(parsed.get("key_concepts"), limit=120),
        "strengths": normalize_text_list(parsed.get("strengths"), limit=600),
        "weaknesses": normalize_text_list(parsed.get("weaknesses"), limit=600),
        "priority_actions": normalize_text_list(parsed.get("priority_actions"), limit=600),
        "rag_readiness": normalize_text(parsed.get("rag_readiness"), limit=80),
        "chunking_advice": normalize_text(parsed.get("chunking_advice"), limit=1000),
        "raw_json": parsed,
    }


def should_retry_without_json_mode(exc):
    """Return whether a LLM error is likely caused by JSON mode support."""
    message = str(exc or "").lower()
    return any(
        marker in message
        for marker in ("response_format", "json mode", "json_schema", "unsupported", "not support", "400")
    )


def call_shard_quality_llm(config, runtime, messages, metadata):
    """Call a LLM for shard quality, retrying without JSON mode if required."""
    try:
        return call_llm_chat_completion_with_config(
            runtime,
            messages,
            purpose="shard_quality_analysis",
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
            purpose="shard_quality_analysis",
            metadata=fallback_metadata,
            temperature=config["temperature"],
            max_tokens=config["max_tokens"],
            retries=1,
            json_mode=False,
        )


def build_analysis_prompt(config, project, shard, metrics, extra_instructions):
    """Build the LLM prompt for one shard."""
    content = shard.get("content_document") or ""
    max_chars = int(config.get("max_input_chars") or 30000)
    truncated = content[:max_chars]
    truncation_note = ""
    if len(content) > max_chars:
        truncation_note = f"\nLe contenu a ete tronque a {max_chars} caracteres pour l'analyse LLM."
    return "\n\n".join(
        part
        for part in [
            config["analysis_prompt"],
            extra_instructions,
            f"Projet: {project.get('name')} / {project.get('slug')}",
            f"Shard UUID: {shard.get('uuid')}",
            f"Titre: {shard.get('title_document') or '-'}",
            f"Source: {shard.get('source_document') or '-'}",
            f"URL: {shard.get('url_document') or '-'}",
            f"Metriques locales JSON:\n{json.dumps(metrics, ensure_ascii=False)}",
            f"Contenu du shard:{truncation_note}\n\n{truncated}",
        ]
        if part
    )


def store_shard_quality_run(project_slug, shard, actor, metrics, analysis, audit_session_id):
    """Persist one Shard Quality analysis run."""
    run_id = uuid4().hex
    content = shard.get("content_document") or ""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_shard_quality_tables(cur)
            cur.execute(
                """
                INSERT INTO public.shard_quality_run
                    (
                        run_id, project_slug, shard_id, actor, title_document,
                        content_sha256, local_metrics, analysis_result,
                        overall_score, audit_session_id
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    run_id,
                    project_slug,
                    shard.get("uuid") or "",
                    actor or "",
                    shard.get("title_document") or "",
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    Json(metrics or {}),
                    Json(analysis or {}),
                    int((analysis or {}).get("overall_score") or 0),
                    audit_session_id or "",
                ),
            )
        conn.commit()
    return run_id


def serialize_run(row):
    """Serialize one Shard Quality run row."""
    analysis = row[6] or {}
    return {
        "run_id": row[0],
        "project_slug": row[1],
        "shard_id": row[2],
        "actor": row[3] or "",
        "title_document": row[4] or "",
        "overall_score": int(row[5] or 0),
        "verdict": analysis.get("verdict", "") if isinstance(analysis, dict) else "",
        "summary": analysis.get("summary", "") if isinstance(analysis, dict) else "",
        "audit_session_id": row[7] or "",
        "created_at": row[8].isoformat(timespec="seconds") if row[8] else "",
        "preview": shorten_text(analysis.get("summary", "") if isinstance(analysis, dict) else "", 220),
    }


def list_shard_quality_runs(project_slug, shard_id="", limit=25):
    """List recent Shard Quality analyses."""
    safe_limit = min(max(int(limit or 25), 1), 200)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_shard_quality_tables(cur)
            if shard_id:
                cur.execute(
                    """
                    SELECT run_id, project_slug, shard_id, actor, title_document,
                           overall_score, analysis_result, audit_session_id, created_at
                    FROM public.shard_quality_run
                    WHERE project_slug = %s AND shard_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (project_slug, shard_id, safe_limit),
                )
            else:
                cur.execute(
                    """
                    SELECT run_id, project_slug, shard_id, actor, title_document,
                           overall_score, analysis_result, audit_session_id, created_at
                    FROM public.shard_quality_run
                    WHERE project_slug = %s
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (project_slug, safe_limit),
                )
            rows = cur.fetchall()
        conn.commit()
    return [serialize_run(row) for row in rows]


def analyze_shard_quality(project_slug, shard_id, payload, actor="admin"):
    """Analyze one shard and persist the LLM quality result."""
    config, runtime = require_shard_quality_available()
    project, shard = get_shard_record(project_slug, shard_id)
    if not (shard.get("content_document") or "").strip():
        raise ValueError("Le shard est vide.")
    metrics = compute_local_metrics(shard)
    extra_instructions = normalize_text(payload.get("extra_instructions"), limit=3000)
    messages = [
        {"role": "system", "content": config["system_prompt"]},
        {
            "role": "user",
            "content": build_analysis_prompt(config, project, shard, metrics, extra_instructions),
        },
    ]
    metadata = {
        "project_slug": project_slug,
        "shard_id": shard_id,
        "actor": actor,
        "title_document": shard.get("title_document", ""),
        "content_sha256": hashlib.sha256((shard.get("content_document") or "").encode("utf-8")).hexdigest(),
    }
    llm_payload = call_shard_quality_llm(config, runtime, messages, metadata)
    content = extract_chat_completion_content(llm_payload)
    analysis = normalize_analysis(parse_json_object(content))
    analysis["audit_session_id"] = llm_payload.get("_audit_session_id", "")
    analysis["generated_at"] = now_iso()
    analysis["local_metrics"] = metrics
    run_id = store_shard_quality_run(
        project_slug,
        shard,
        actor,
        metrics,
        analysis,
        analysis["audit_session_id"],
    )
    analysis["run_id"] = run_id
    return analysis


def get_shard_quality_payload(project_slug, shard_id):
    """Return page payload for one shard quality screen."""
    project, shard = get_shard_record(project_slug, shard_id)
    config = get_shard_quality_config()
    return {
        "project": project,
        "shard": shard,
        "metrics": compute_local_metrics(shard),
        "config": config,
        "status": shard_quality_status(config),
        "runs": list_shard_quality_runs(project_slug, shard_id),
    }


def get_shard_quality_index_payload():
    """Return projects and status for the Shard Quality index."""
    config = get_shard_quality_config()
    return {
        "projects": list_projects_shards(),
        "config": config,
        "status": shard_quality_status(config),
    }
