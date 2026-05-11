"""Task sequencing module configuration, LLM calls, and persistence."""

import json
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


TASK_SEQUENCER_CONFIG_TABLE = "task_sequencer_config"
TASK_SEQUENCER_RUN_TABLE = "task_sequencer_run"
DEFAULT_AXES_SYSTEM_PROMPT = """Tu aides un utilisateur connecte a choisir les axes de sequencage d'une tache.
Retourne uniquement un JSON valide avec cette forme:
{
  "axes": [
    {
      "axis_id": "court_id_stable",
      "label": "Nom court",
      "description": "Ce que cet axe structure",
      "recommended": true
    }
  ],
  "guidance": "Conseil court pour choisir les axes"
}
Propose 4 a 6 axes actionnables, sans redondance."""
DEFAULT_PLAN_SYSTEM_PROMPT = """Tu es un assistant de planification operationnelle.
Tu dois decomposer une tache en sequences executables et verifiables, strictement relatives au contexte fourni.
Retourne uniquement un JSON valide avec cette forme:
{
  "title": "Titre court",
  "summary": "Resume operationnel",
  "sequences": [
    {
      "sequence_no": 1,
      "title": "Nom de sequence",
      "objective": "Objectif",
      "tasks": [
        {
          "title": "Action",
          "description": "Action concrete",
          "expected_output": "Livrable ou resultat attendu",
          "dependencies": ["pre-requis"],
          "estimate": "ordre de grandeur"
        }
      ],
      "validation_checkpoints": ["controle"],
      "risks": ["risque"]
    }
  ],
  "open_questions": ["question si une information manque"]
}
Le plan doit rester concret, ordonne et directement exploitable."""


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


def normalize_temperature(raw_value, default=0.2):
    """Normalize a LLM temperature value."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0.0), 2.0)


def normalize_text(value, limit=20000):
    """Normalize user text while preserving line breaks."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def normalize_slug(value, fallback="axis"):
    """Build a short stable identifier."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
    cleaned = cleaned.strip("-_")
    return (cleaned or fallback)[:80]


def ensure_task_sequencer_tables(cur):
    """Create task sequencer configuration and run tables."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.task_sequencer_config (
            id integer PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            llm_config_id text NOT NULL DEFAULT '',
            temperature numeric(4, 2) NOT NULL DEFAULT 0.2,
            max_tokens integer NOT NULL DEFAULT 1800,
            axes_system_prompt text NOT NULL DEFAULT '',
            plan_system_prompt text NOT NULL DEFAULT '',
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.task_sequencer_config ADD COLUMN IF NOT EXISTS llm_config_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.task_sequencer_config ADD COLUMN IF NOT EXISTS temperature numeric(4, 2) NOT NULL DEFAULT 0.2;")
    cur.execute("ALTER TABLE public.task_sequencer_config ADD COLUMN IF NOT EXISTS max_tokens integer NOT NULL DEFAULT 1800;")
    cur.execute("ALTER TABLE public.task_sequencer_config ADD COLUMN IF NOT EXISTS axes_system_prompt text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.task_sequencer_config ADD COLUMN IF NOT EXISTS plan_system_prompt text NOT NULL DEFAULT '';")
    cur.execute(
        """
        INSERT INTO public.task_sequencer_config
            (id, enabled, llm_config_id, temperature, max_tokens, axes_system_prompt, plan_system_prompt)
        VALUES (1, true, '', 0.2, 1800, %s, %s)
        ON CONFLICT (id) DO NOTHING;
        """,
        (DEFAULT_AXES_SYSTEM_PROMPT, DEFAULT_PLAN_SYSTEM_PROMPT),
    )
    cur.execute(
        """
        UPDATE public.task_sequencer_config
        SET axes_system_prompt = %s
        WHERE id = 1 AND (axes_system_prompt IS NULL OR axes_system_prompt = '');
        """,
        (DEFAULT_AXES_SYSTEM_PROMPT,),
    )
    cur.execute(
        """
        UPDATE public.task_sequencer_config
        SET plan_system_prompt = %s
        WHERE id = 1 AND (plan_system_prompt IS NULL OR plan_system_prompt = '');
        """,
        (DEFAULT_PLAN_SYSTEM_PROMPT,),
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.task_sequencer_run (
            run_id text PRIMARY KEY,
            actor text NOT NULL DEFAULT '',
            context_text text NOT NULL,
            task_type text NOT NULL DEFAULT '',
            sequencing_axes text NOT NULL DEFAULT '',
            axes_suggestions jsonb NOT NULL DEFAULT '{}'::jsonb,
            plan_result jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'completed',
            axes_audit_session_id text NOT NULL DEFAULT '',
            plan_audit_session_id text NOT NULL DEFAULT '',
            error_message text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS task_sequencer_run_created_idx
        ON public.task_sequencer_run(created_at DESC);
        """
    )


def serialize_config(row):
    """Serialize one config row."""
    return {
        "enabled": bool(row[0]) if row else True,
        "llm_config_id": row[1] if row and row[1] else "",
        "temperature": float(row[2]) if row else 0.2,
        "max_tokens": normalize_max_tokens(row[3] if row else 1800, default=1800),
        "axes_system_prompt": row[4] if row and row[4] else DEFAULT_AXES_SYSTEM_PROMPT,
        "plan_system_prompt": row[5] if row and row[5] else DEFAULT_PLAN_SYSTEM_PROMPT,
        "updated_at": row[6].isoformat(timespec="seconds") if row and row[6] else "",
    }


def get_task_sequencer_config():
    """Return persisted task sequencer configuration."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_task_sequencer_tables(cur)
            cur.execute(
                """
                SELECT enabled, llm_config_id, temperature, max_tokens,
                       axes_system_prompt, plan_system_prompt, updated_at
                FROM public.task_sequencer_config
                WHERE id = 1;
                """
            )
            row = cur.fetchone()
        conn.commit()
    return serialize_config(row)


def save_task_sequencer_config(payload):
    """Persist task sequencer configuration."""
    enabled = normalize_bool(payload.get("enabled"), default=False)
    llm_config_id = normalize_config_id(payload.get("llm_config_id"))
    temperature = normalize_temperature(payload.get("temperature"), default=0.2)
    max_tokens = normalize_max_tokens(payload.get("max_tokens"), default=1800)
    axes_system_prompt = normalize_text(payload.get("axes_system_prompt"), limit=8000)
    plan_system_prompt = normalize_text(payload.get("plan_system_prompt"), limit=10000)
    if not axes_system_prompt:
        raise ValueError("Le prompt de suggestion des axes est obligatoire.")
    if not plan_system_prompt:
        raise ValueError("Le prompt Workflow Sequencer est obligatoire.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_task_sequencer_tables(cur)
            cur.execute(
                """
                UPDATE public.task_sequencer_config
                SET enabled = %s,
                    llm_config_id = %s,
                    temperature = %s,
                    max_tokens = %s,
                    axes_system_prompt = %s,
                    plan_system_prompt = %s,
                    updated_at = now()
                WHERE id = 1;
                """,
                (
                    enabled,
                    llm_config_id,
                    temperature,
                    max_tokens,
                    axes_system_prompt,
                    plan_system_prompt,
                ),
            )
        conn.commit()
    return get_task_sequencer_config()


def runtime_config_for_task_sequencer(config=None):
    """Resolve the LLM runtime configuration selected for the module."""
    active_config = config or get_task_sequencer_config()
    runtime = effective_llm_config(
        redact_key=False,
        config_id=active_config.get("llm_config_id", ""),
    )
    return runtime


def task_sequencer_status(config=None):
    """Return module availability status."""
    active_config = config or get_task_sequencer_config()
    try:
        runtime = runtime_config_for_task_sequencer(active_config)
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


def require_task_sequencer_available(config=None):
    """Return config and runtime or raise a public-safe error."""
    active_config = config or get_task_sequencer_config()
    if not active_config.get("enabled"):
        raise ValueError("Workflow Sequencer desactive.")
    runtime = runtime_config_for_task_sequencer(active_config)
    if not runtime.get("configured"):
        raise ValueError("Configuration LLM du Workflow Sequencer incomplete.")
    return active_config, runtime


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

    raise ValueError("La reponse LLM n'est pas un JSON objet valide.")


def normalize_axis(item, index):
    """Normalize one suggested axis."""
    if isinstance(item, str):
        label = normalize_text(item, limit=120)
        description = ""
        recommended = index == 0
        axis_id = normalize_slug(label, fallback=f"axis-{index + 1}")
    elif isinstance(item, dict):
        label = normalize_text(item.get("label") or item.get("name"), limit=120)
        description = normalize_text(item.get("description"), limit=400)
        recommended = normalize_bool(item.get("recommended"), default=index == 0)
        axis_id = normalize_slug(item.get("axis_id") or label, fallback=f"axis-{index + 1}")
    else:
        return None

    if not label:
        return None
    return {
        "axis_id": axis_id,
        "label": label,
        "description": description,
        "recommended": recommended,
    }


def normalize_axes_payload(parsed):
    """Normalize axes JSON returned by the LLM."""
    raw_axes = parsed.get("axes", [])
    if not isinstance(raw_axes, list):
        raw_axes = []
    axes = []
    seen_ids = set()
    for index, item in enumerate(raw_axes[:8]):
        axis = normalize_axis(item, index)
        if not axis or axis["axis_id"] in seen_ids:
            continue
        axes.append(axis)
        seen_ids.add(axis["axis_id"])

    if not axes:
        raise ValueError("Le LLM n'a pas retourne d'axes exploitables.")
    return {
        "axes": axes,
        "guidance": normalize_text(parsed.get("guidance"), limit=800),
    }


def build_axes_messages(config, context_text, task_type):
    """Build messages for axis suggestion."""
    user_content = (
        f"Contexte utilisateur:\n{context_text}\n\n"
        f"Type de tache deja saisi:\n{task_type or 'Non precise'}\n\n"
        "Propose des axes de sequencage adaptes au contexte. "
        "Les axes doivent aider l'utilisateur a choisir le type de sequencage."
    )
    return [
        {"role": "system", "content": config["axes_system_prompt"]},
        {"role": "user", "content": user_content},
    ]


def should_retry_without_json_mode(exc):
    """Return whether a LLM error is likely caused by JSON mode support."""
    message = str(exc or "").lower()
    return any(
        marker in message
        for marker in (
            "response_format",
            "json mode",
            "json_schema",
            "unsupported",
            "not support",
            "400",
        )
    )


def call_task_sequencer_llm(config, runtime, messages, purpose, metadata, max_tokens):
    """Call the task sequencer LLM, retrying without JSON mode if needed."""
    try:
        return call_llm_chat_completion_with_config(
            runtime,
            messages,
            purpose=purpose,
            metadata=metadata,
            temperature=config["temperature"],
            max_tokens=max_tokens,
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
            purpose=purpose,
            metadata=fallback_metadata,
            temperature=config["temperature"],
            max_tokens=max_tokens,
            retries=1,
            json_mode=False,
        )


def suggest_task_sequence_axes(payload, actor="admin"):
    """Generate suggested sequencing axes for a context."""
    context_text = normalize_text(payload.get("context_text"), limit=20000)
    task_type = normalize_text(payload.get("task_type"), limit=500)
    if not context_text:
        raise ValueError("Le contexte est obligatoire.")

    config, runtime = require_task_sequencer_available()
    llm_payload = call_task_sequencer_llm(
        config,
        runtime,
        build_axes_messages(config, context_text, task_type),
        "task_sequencer_axes",
        {
            "actor": actor,
            "context_length": len(context_text),
            "task_type": task_type,
        },
        min(config["max_tokens"], 1400),
    )
    content = extract_chat_completion_content(llm_payload)
    parsed = parse_json_object(content)
    normalized = normalize_axes_payload(parsed)
    normalized["audit_session_id"] = llm_payload.get("_audit_session_id", "")
    normalized["generated_at"] = now_iso()
    return normalized


def parse_axes_suggestions_payload(raw_value):
    """Parse axes suggestions carried by a form hidden field."""
    if isinstance(raw_value, dict):
        return raw_value
    cleaned = str(raw_value or "").strip()
    if not cleaned:
        return {}
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_plan_messages(config, context_text, task_type, sequencing_axes):
    """Build messages for final sequencing."""
    user_content = (
        f"Contexte utilisateur:\n{context_text}\n\n"
        f"Type de tache a sequencer:\n{task_type}\n\n"
        f"Axes de sequencage retenus:\n{sequencing_axes}\n\n"
        "Decoupe les travaux a effectuer par sequences. "
        "Chaque sequence doit contenir des actions concretes, des livrables, "
        "des dependances et des points de validation."
    )
    return [
        {"role": "system", "content": config["plan_system_prompt"]},
        {"role": "user", "content": user_content},
    ]


def normalize_task_item(item, index):
    """Normalize one task item in a sequence."""
    if not isinstance(item, dict):
        item = {"title": str(item or "").strip()}
    dependencies = item.get("dependencies", [])
    if isinstance(dependencies, str):
        dependencies = [dependencies]
    if not isinstance(dependencies, list):
        dependencies = []
    return {
        "title": normalize_text(item.get("title") or f"Action {index + 1}", limit=180),
        "description": normalize_text(item.get("description"), limit=1000),
        "expected_output": normalize_text(item.get("expected_output"), limit=500),
        "dependencies": [normalize_text(dep, limit=160) for dep in dependencies if normalize_text(dep, limit=160)],
        "estimate": normalize_text(item.get("estimate"), limit=120),
    }


def normalize_text_list(value, limit=300):
    """Normalize a value into a list of short strings."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [normalize_text(item, limit=limit) for item in values if normalize_text(item, limit=limit)]


def normalize_sequence_no(raw_value, fallback):
    """Normalize a sequence number from LLM output."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = fallback
    return max(value, 1)


def normalize_sequence(item, index):
    """Normalize one generated sequence."""
    if not isinstance(item, dict):
        item = {"title": str(item or "").strip(), "tasks": []}
    raw_tasks = item.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    tasks = [
        normalize_task_item(task, task_index)
        for task_index, task in enumerate(raw_tasks[:20])
    ]
    return {
        "sequence_no": normalize_sequence_no(item.get("sequence_no"), index + 1),
        "title": normalize_text(item.get("title") or f"Sequence {index + 1}", limit=180),
        "objective": normalize_text(item.get("objective"), limit=800),
        "tasks": tasks,
        "validation_checkpoints": normalize_text_list(item.get("validation_checkpoints"), limit=300),
        "risks": normalize_text_list(item.get("risks"), limit=300),
    }


def normalize_plan_payload(parsed, raw_content):
    """Normalize final plan JSON returned by the LLM."""
    raw_sequences = parsed.get("sequences", [])
    if not isinstance(raw_sequences, list):
        raw_sequences = []
    sequences = [
        normalize_sequence(item, index)
        for index, item in enumerate(raw_sequences[:30])
    ]
    if not sequences:
        raise ValueError("Le LLM n'a pas retourne de sequences exploitables.")
    return {
        "title": normalize_text(parsed.get("title") or "Sequencage de taches", limit=180),
        "summary": normalize_text(parsed.get("summary"), limit=1200),
        "sequences": sequences,
        "open_questions": normalize_text_list(parsed.get("open_questions"), limit=300),
        "raw_json": parsed,
        "raw_content": raw_content,
    }


def store_task_sequence_run(
    actor,
    context_text,
    task_type,
    sequencing_axes,
    axes_suggestions,
    plan_result,
    plan_audit_session_id,
    axes_audit_session_id="",
):
    """Persist one successful task sequencing run."""
    run_id = uuid4().hex
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_task_sequencer_tables(cur)
            cur.execute(
                """
                INSERT INTO public.task_sequencer_run
                    (
                        run_id, actor, context_text, task_type, sequencing_axes,
                        axes_suggestions, plan_result, status,
                        axes_audit_session_id, plan_audit_session_id
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed', %s, %s);
                """,
                (
                    run_id,
                    actor or "",
                    context_text,
                    task_type,
                    sequencing_axes,
                    Json(axes_suggestions or {}),
                    Json(plan_result or {}),
                    axes_audit_session_id or "",
                    plan_audit_session_id or "",
                ),
            )
        conn.commit()
    return run_id


def generate_task_sequence(payload, actor="admin"):
    """Generate a sequenced task plan from user input."""
    context_text = normalize_text(payload.get("context_text"), limit=20000)
    task_type = normalize_text(payload.get("task_type"), limit=1000)
    sequencing_axes = normalize_text(payload.get("sequencing_axes"), limit=2000)
    axes_suggestions = parse_axes_suggestions_payload(payload.get("axes_suggestions_json"))

    if not context_text:
        raise ValueError("Le contexte est obligatoire.")
    if not task_type:
        raise ValueError("Le type de tache est obligatoire.")
    if not sequencing_axes:
        raise ValueError("Les axes de sequencage sont obligatoires.")

    config, runtime = require_task_sequencer_available()
    llm_payload = call_task_sequencer_llm(
        config,
        runtime,
        build_plan_messages(config, context_text, task_type, sequencing_axes),
        "task_sequencer_plan",
        {
            "actor": actor,
            "context_length": len(context_text),
            "task_type": task_type,
            "sequencing_axes": sequencing_axes,
        },
        config["max_tokens"],
    )
    content = extract_chat_completion_content(llm_payload)
    parsed = parse_json_object(content)
    plan = normalize_plan_payload(parsed, content)
    plan["audit_session_id"] = llm_payload.get("_audit_session_id", "")
    plan["generated_at"] = now_iso()
    plan["run_id"] = store_task_sequence_run(
        actor,
        context_text,
        task_type,
        sequencing_axes,
        axes_suggestions,
        plan,
        plan["audit_session_id"],
        axes_suggestions.get("audit_session_id", ""),
    )
    return plan


def serialize_run(row):
    """Serialize a task sequencing run row."""
    plan_result = row[5] or {}
    return {
        "run_id": row[0],
        "actor": row[1] or "",
        "task_type": row[2] or "",
        "sequencing_axes": row[3] or "",
        "status": row[4] or "",
        "title": plan_result.get("title", "") if isinstance(plan_result, dict) else "",
        "plan_audit_session_id": row[6] or "",
        "created_at": row[7].isoformat(timespec="seconds") if row[7] else "",
    }


def list_task_sequence_runs(limit=25):
    """List recent task sequencing runs."""
    safe_limit = min(max(int(limit or 25), 1), 200)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_task_sequencer_tables(cur)
            cur.execute(
                """
                SELECT run_id, actor, task_type, sequencing_axes, status, plan_result,
                       plan_audit_session_id, created_at
                FROM public.task_sequencer_run
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()
        conn.commit()
    return [serialize_run(row) for row in rows]


def get_task_sequencer_payload():
    """Return page payload for task sequencer UI."""
    config = get_task_sequencer_config()
    return {
        "config": config,
        "status": task_sequencer_status(config),
        "runs": list_task_sequence_runs(),
    }
