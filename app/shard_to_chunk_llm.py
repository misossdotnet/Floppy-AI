"""LLM-assisted Shard-To-Chunk workflow."""

import json
import re
from uuid import uuid4

from psycopg2 import sql
from psycopg2.extras import Json

from db import get_db_connection
from llm_gateway import (
    call_llm_chat_completion_with_config,
    effective_llm_config,
    extract_chat_completion_content,
    list_llm_configs,
    normalize_max_tokens,
)
from services import (
    CHUNK_METADATA_TABLE,
    compute_quality_score,
    ensure_business_tables,
    ensure_project_tables_exist,
    list_projects,
    table_exists,
)


CHUNK_TYPE_OPTIONS = (
    "markdown",
    "code",
    "plain_text",
    "html",
    "json",
    "qa",
    "table",
)

SPLIT_PROFILE_OPTIONS = (
    "semantic_markdown",
    "semantic_code",
    "ocr_cleanup",
    "qa_dialogue",
    "generic",
)


def normalize_choice(value, allowed_values, default):
    """Normalize one form choice."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed_values else default


def normalize_bool(value, default=False):
    """Normalize checkbox-like values."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes", "oui"}


def normalize_int(value, default, minimum, maximum):
    """Normalize bounded integer values."""
    try:
        integer = int(value)
    except (TypeError, ValueError):
        integer = default
    return min(max(integer, minimum), maximum)


def clean_llm_json_text(raw_text):
    """Remove common markdown wrappers around JSON."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_chunk_json(raw_text):
    """Parse LLM chunk JSON with a small repair pass."""
    cleaned = clean_llm_json_text(raw_text)
    try:
        payload = json.loads(cleaned)
    except ValueError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("La reponse LLM ne contient pas de JSON exploitable.")
        payload = json.loads(cleaned[start : end + 1])
    if isinstance(payload, list):
        payload = {"chunks": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("chunks"), list):
        raise ValueError("La reponse LLM doit contenir un tableau 'chunks'.")
    return payload


def split_source_windows(source_text, window_chars, max_windows):
    """Split source text into bounded LLM windows."""
    text = (source_text or "").strip()
    if not text:
        return []
    windows = []
    start = 0
    while start < len(text) and len(windows) < max_windows:
        end = min(len(text), start + window_chars)
        window = text[start:end].strip()
        if window:
            windows.append(window)
        if end >= len(text):
            break
        overlap = min(1200, max(0, window_chars // 20))
        start = max(end - overlap, start + 1)
    return windows


def build_llm_chunk_messages(shard, window_text, options, window_index, window_count):
    """Build the JSON-only prompt for LLM-assisted chunking."""
    title = shard.get("title_document") or shard.get("source_document") or shard["uuid"]
    system_prompt = (
        "Tu es un module Shard-To-Chunk pour une base de donnees RAG. "
        "Decoupe le shard fourni en chunks exploitables sans inventer de contenu. "
        "Conserve la langue, les faits, le code et les titres utiles. "
        "Reponds uniquement avec un objet JSON valide."
    )
    user_prompt = f"""
Contexte:
- document_id: {shard["uuid"]}
- titre: {title}
- source: {shard.get("source_document") or "-"}
- fenetre: {window_index + 1}/{window_count}
- profil_decoupage: {options["split_profile"]}
- chunk_type_cible: {options["chunk_type"]}
- taille_cible_tokens: {options["target_tokens"]}
- nombre_max_chunks_pour_cette_fenetre: {options["max_chunks_per_window"]}

Schema JSON attendu:
{{
  "chunks": [
    {{
      "title": "titre court du chunk",
      "section_path": "chemin logique ou titre de section",
      "chunk_type": "{options["chunk_type"]}",
      "content": "contenu complet du chunk",
      "metadata": {{"reason": "raison courte du decoupage"}}
    }}
  ]
}}

Contraintes:
- ne renvoie pas de markdown hors JSON;
- chaque content doit etre autonome et non vide;
- ne resume pas: conserve le contenu utile du shard;
- ne duplique pas inutilement les chunks;
- si le contenu est du code, garde les blocs de code intacts.

Shard:
{window_text}
""".strip()
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def normalize_llm_chunks(raw_chunks, shard, options, window_index):
    """Normalize raw LLM chunk objects into DB-ready dictionaries."""
    normalized = []
    for index, raw_chunk in enumerate(raw_chunks):
        if not isinstance(raw_chunk, dict):
            continue
        content = str(raw_chunk.get("content") or raw_chunk.get("text") or "").strip()
        if not content:
            continue
        title = str(
            raw_chunk.get("title")
            or raw_chunk.get("section_title")
            or shard.get("title_document")
            or f"Chunk {index + 1}"
        ).strip()[:240]
        section_path = str(raw_chunk.get("section_path") or title).strip()[:500]
        chunk_type = normalize_choice(raw_chunk.get("chunk_type"), CHUNK_TYPE_OPTIONS, options["chunk_type"])
        metadata = raw_chunk.get("metadata") if isinstance(raw_chunk.get("metadata"), dict) else {}
        metadata = {
            **metadata,
            "source": "llm",
            "split_profile": options["split_profile"],
            "chunk_type": chunk_type,
            "window_index": window_index,
        }
        normalized.append(
            {
                "chunk_id": str(uuid4()),
                "title": title,
                "section_path": section_path,
                "chunk_type": chunk_type,
                "content": content,
                "metadata": metadata,
            }
        )
    return normalized


def get_project_shards(project_slug):
    """Return selectable shards for a project."""
    slug = (project_slug or "").strip()
    if not slug:
        return []
    shard_table = f"{slug}_shard"
    chunk_table = f"{slug}_chunk"
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_project_tables_exist(cur, slug)
            if not table_exists(cur, shard_table):
                return []
            cur.execute(
                sql.SQL(
                    """
                    SELECT
                        s.uuid,
                        s.source_document,
                        s.title_document,
                        s.content_document,
                        COUNT(c.uuid)::int AS chunk_count
                    FROM {} s
                    LEFT JOIN {} c
                      ON c.shard_id = s.uuid
                    GROUP BY s.uuid, s.source_document, s.title_document, s.content_document
                    ORDER BY s.title_document NULLS LAST, s.uuid;
                    """
                ).format(
                    sql.Identifier("public", shard_table),
                    sql.Identifier("public", chunk_table),
                )
            )
            rows = cur.fetchall()
        conn.commit()
    return [
        {
            "uuid": row[0],
            "source_document": row[1] or "",
            "title_document": row[2] or "",
            "content_length": len(row[3] or ""),
            "chunk_count": int(row[4] or 0),
        }
        for row in rows
    ]


def get_shard_to_chunk_payload(selected_project="", selected_shard=""):
    """Build the admin payload for LLM-assisted Shard-To-Chunk."""
    projects = list_projects()
    project_slug = (selected_project or "").strip()
    if not project_slug and projects:
        project_slug = projects[0]["slug"]
    shards = get_project_shards(project_slug) if project_slug else []
    shard_id = (selected_shard or "").strip()
    if not shard_id and shards:
        shard_id = shards[0]["uuid"]
    llm_configs = list_llm_configs(redact_key=True)
    return {
        "projects": projects,
        "selected_project": project_slug,
        "shards": shards,
        "selected_shard": shard_id,
        "llm_configs": llm_configs,
        "split_profiles": SPLIT_PROFILE_OPTIONS,
        "chunk_types": CHUNK_TYPE_OPTIONS,
        "defaults": {
            "split_profile": "semantic_markdown",
            "chunk_type": "markdown",
            "target_tokens": 450,
            "source_window_chars": 30000,
            "max_windows": 4,
            "max_chunks_per_window": 12,
            "output_max_tokens": 4000,
        },
    }


def load_shard(cur, project_slug, shard_id):
    """Load one shard row."""
    shard_table = f"{project_slug}_shard"
    cur.execute(
        sql.SQL(
            """
            SELECT uuid, project_id, source_document, url_document, title_document,
                   content_document, autor_document
            FROM {}
            WHERE uuid = %s;
            """
        ).format(sql.Identifier("public", shard_table)),
        (shard_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("Shard introuvable pour ce projet.")
    return {
        "uuid": row[0],
        "project_id": row[1],
        "source_document": row[2] or "",
        "url_document": row[3] or "",
        "title_document": row[4] or "",
        "content_document": row[5] or "",
        "autor_document": row[6] or "",
    }


def parse_generation_options(payload):
    """Normalize generation form payload."""
    return {
        "llm_config_id": str(payload.get("llm_config_id") or "").strip(),
        "split_profile": normalize_choice(
            payload.get("split_profile"),
            SPLIT_PROFILE_OPTIONS,
            "semantic_markdown",
        ),
        "chunk_type": normalize_choice(payload.get("chunk_type"), CHUNK_TYPE_OPTIONS, "markdown"),
        "target_tokens": normalize_int(payload.get("target_tokens"), 450, 80, 2000),
        "source_window_chars": normalize_int(payload.get("source_window_chars"), 30000, 1000, 200000),
        "max_windows": normalize_int(payload.get("max_windows"), 4, 1, 25),
        "max_chunks_per_window": normalize_int(payload.get("max_chunks_per_window"), 12, 1, 40),
        "output_max_tokens": normalize_max_tokens(payload.get("output_max_tokens") or 4000),
        "replace_existing": normalize_bool(payload.get("replace_existing"), default=True),
        "json_mode": normalize_bool(payload.get("json_mode"), default=True),
    }


def generate_chunks_with_llm(project_slug, shard_id, payload, actor="admin"):
    """Generate chunks for one shard using an admin-selected LLM configuration."""
    slug = (project_slug or "").strip()
    selected_shard = (shard_id or "").strip()
    if not slug:
        raise ValueError("Le projet est obligatoire.")
    if not selected_shard:
        raise ValueError("Le shard est obligatoire.")

    options = parse_generation_options(payload)
    runtime_config = effective_llm_config(
        redact_key=False,
        config_id=options["llm_config_id"],
    )
    if not runtime_config.get("configured"):
        raise ValueError("Configuration LLM incomplete ou inactive.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            table_names = ensure_project_tables_exist(cur, slug)
            shard = load_shard(cur, slug, selected_shard)
        conn.commit()

    source_text = shard["content_document"].strip()
    if not source_text:
        raise ValueError("Le shard selectionne est vide.")
    windows = split_source_windows(
        source_text,
        options["source_window_chars"],
        options["max_windows"],
    )
    if not windows:
        raise ValueError("Aucun contenu exploitable dans le shard selectionne.")

    generated_chunks = []
    audit_session_ids = []
    for window_index, window_text in enumerate(windows):
        llm_payload = call_llm_chat_completion_with_config(
            runtime_config,
            build_llm_chunk_messages(shard, window_text, options, window_index, len(windows)),
            purpose="shard_to_chunk_llm",
            metadata={
                "project_slug": slug,
                "shard_id": selected_shard,
                "actor": actor,
                "split_profile": options["split_profile"],
                "chunk_type": options["chunk_type"],
                "window_index": window_index,
                "window_count": len(windows),
            },
            temperature=0.1,
            max_tokens=options["output_max_tokens"],
            json_mode=options["json_mode"],
        )
        audit_session_id = llm_payload.get("_audit_session_id", "")
        if audit_session_id:
            audit_session_ids.append(audit_session_id)
        parsed = parse_chunk_json(extract_chat_completion_content(llm_payload))
        window_chunks = normalize_llm_chunks(parsed["chunks"], shard, options, window_index)
        for chunk in window_chunks:
            chunk["llm_audit_session_id"] = audit_session_id
        generated_chunks.extend(window_chunks)

    if not generated_chunks:
        raise ValueError("Le LLM n'a produit aucun chunk exploitable.")

    for index, chunk in enumerate(generated_chunks):
        chunk["previous_chunk_id"] = generated_chunks[index - 1]["chunk_id"] if index > 0 else None
        chunk["next_chunk_id"] = (
            generated_chunks[index + 1]["chunk_id"] if index < len(generated_chunks) - 1 else None
        )

    chunk_table = table_names["chunk_table"]
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_business_tables(cur)
            ensure_project_tables_exist(cur, slug)
            if options["replace_existing"]:
                cur.execute(
                    sql.SQL("DELETE FROM {} WHERE shard_id = %s;").format(
                        sql.Identifier("public", chunk_table)
                    ),
                    (selected_shard,),
                )
                cur.execute(
                    f"DELETE FROM public.{CHUNK_METADATA_TABLE} WHERE project_slug = %s AND shard_id = %s;",
                    (slug, selected_shard),
                )
            for chunk in generated_chunks:
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
                        chunk["chunk_id"],
                        selected_shard,
                        shard["source_document"],
                        shard["url_document"],
                        chunk["title"],
                        chunk["content"],
                        shard["autor_document"],
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
                        quality_score,
                        chunk_type,
                        chunking_method,
                        llm_config_id,
                        llm_profile_type,
                        llm_audit_session_id,
                        metadata,
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
                        quality_score = EXCLUDED.quality_score,
                        chunk_type = EXCLUDED.chunk_type,
                        chunking_method = EXCLUDED.chunking_method,
                        llm_config_id = EXCLUDED.llm_config_id,
                        llm_profile_type = EXCLUDED.llm_profile_type,
                        llm_audit_session_id = EXCLUDED.llm_audit_session_id,
                        metadata = EXCLUDED.metadata,
                        updated_at = now();
                    """,
                    (
                        chunk["chunk_id"],
                        slug,
                        selected_shard,
                        selected_shard,
                        chunk["title"],
                        chunk["section_path"],
                        None,
                        chunk["previous_chunk_id"],
                        chunk["next_chunk_id"],
                        compute_quality_score(chunk["content"]),
                        chunk["chunk_type"],
                        "llm",
                        runtime_config.get("config_id", ""),
                        runtime_config.get("profile_type", "general"),
                        chunk.get("llm_audit_session_id", ""),
                        Json(chunk["metadata"]),
                    ),
                )
        conn.commit()

    return {
        "project_slug": slug,
        "shard_id": selected_shard,
        "generated_chunks": len(generated_chunks),
        "audit_session_ids": audit_session_ids,
        "llm_config_id": runtime_config.get("config_id", ""),
        "llm_profile_type": runtime_config.get("profile_type", "general"),
        "options": options,
    }
