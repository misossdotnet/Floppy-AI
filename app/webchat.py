"""Public webchat configuration, pipeline, and session helpers."""

import json
import re
from datetime import datetime, timezone
from string import Formatter
from uuid import uuid4

from psycopg2.extras import Json

from db import get_db_connection
from llm_gateway import (
    call_llm_chat_completion,
    extract_chat_completion_content,
    extract_chat_completion_reasoning,
    llm_connection_status,
    normalize_config_id,
)


WEBCHAT_CONFIG_TABLE = "webchat_config"
WEBCHAT_PIPELINE_TABLE = "webchat_pipeline_step"
WEBCHAT_SESSION_TABLE = "webchat_session"
WEBCHAT_MESSAGE_TABLE = "webchat_message"
PIPELINE_DIRECTIONS = {"inbound", "outbound"}
PIPELINE_STEP_TYPES = {"llm_transform", "llm_guard"}
DEFAULT_WEBCHAT_SYSTEM_PROMPT = (
    "Tu es l'assistant public de Floppy-AI. Reponds en francais, de facon claire, "
    "utile et concise. Si une information manque, pose une question courte."
)
DEFAULT_GUARD_BLOCK_MESSAGE = (
    "Votre message ne peut pas etre traite dans ce contexte."
)


class SafeFormatDict(dict):
    """Format map that leaves unknown placeholders readable."""

    def __missing__(self, key):
        return "{" + key + "}"


def now_iso():
    """Return UTC timestamp as ISO text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_webchat_tables(cur):
    """Create webchat tables without touching project chat tables."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.webchat_config (
            id integer PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            llm_config_id text NOT NULL DEFAULT '',
            system_prompt text NOT NULL DEFAULT '',
            temperature numeric(4, 2) NOT NULL DEFAULT 0.2,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.webchat_config ADD COLUMN IF NOT EXISTS llm_config_id text NOT NULL DEFAULT '';")
    cur.execute(
        """
        INSERT INTO public.webchat_config (id, enabled, system_prompt, temperature)
        VALUES (1, true, %s, 0.2)
        ON CONFLICT (id) DO NOTHING;
        """,
        (DEFAULT_WEBCHAT_SYSTEM_PROMPT,),
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.webchat_pipeline_step (
            step_id text PRIMARY KEY,
            direction text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
            position integer NOT NULL DEFAULT 100,
            name text NOT NULL,
            step_type text NOT NULL DEFAULT 'llm_transform'
                CHECK (step_type IN ('llm_transform', 'llm_guard')),
            enabled boolean NOT NULL DEFAULT true,
            fail_closed boolean NOT NULL DEFAULT true,
            llm_config_id text NOT NULL DEFAULT '',
            prompt_template text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.webchat_pipeline_step ADD COLUMN IF NOT EXISTS llm_config_id text NOT NULL DEFAULT '';")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS webchat_pipeline_order_idx
        ON public.webchat_pipeline_step(direction, position, created_at);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.webchat_session (
            session_id text PRIMARY KEY,
            status text NOT NULL DEFAULT 'active',
            user_agent text,
            ip_address text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.webchat_message (
            message_id text PRIMARY KEY,
            session_id text NOT NULL REFERENCES public.webchat_session(session_id) ON DELETE CASCADE,
            role text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
            content text NOT NULL,
            raw_content text,
            pipeline_trace jsonb NOT NULL DEFAULT '[]'::jsonb,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS webchat_message_session_idx
        ON public.webchat_message(session_id, created_at);
        """
    )


def normalize_bool(value, default=False):
    """Normalize form booleans."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes", "oui"}


def normalize_temperature(raw_value):
    """Normalize a temperature value."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = 0.2
    return min(max(value, 0.0), 2.0)


def normalize_direction(raw_direction):
    """Normalize pipeline direction."""
    direction = str(raw_direction or "").strip().lower()
    if direction not in PIPELINE_DIRECTIONS:
        raise ValueError("Direction pipeline invalide.")
    return direction


def normalize_step_type(raw_step_type):
    """Normalize pipeline step type."""
    step_type = str(raw_step_type or "llm_transform").strip().lower()
    if step_type not in PIPELINE_STEP_TYPES:
        raise ValueError("Type d'etape pipeline invalide.")
    return step_type


def normalize_position(raw_position):
    """Normalize a pipeline step position."""
    try:
        position = int(raw_position)
    except (TypeError, ValueError):
        position = 100
    return min(max(position, 1), 10000)


def get_webchat_config():
    """Return webchat config and pipeline steps."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                SELECT enabled, llm_config_id, system_prompt, temperature, updated_at
                FROM public.webchat_config
                WHERE id = 1;
                """
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT step_id, direction, position, name, step_type, enabled,
                       fail_closed, llm_config_id, prompt_template, created_at, updated_at
                FROM public.webchat_pipeline_step
                ORDER BY direction ASC, position ASC, created_at ASC;
                """
            )
            step_rows = cur.fetchall()
        conn.commit()

    config = {
        "enabled": bool(row[0]) if row else True,
        "llm_config_id": row[1] if row and row[1] else "",
        "system_prompt": row[2] if row and row[2] else DEFAULT_WEBCHAT_SYSTEM_PROMPT,
        "temperature": float(row[3]) if row else 0.2,
        "updated_at": row[4].isoformat(timespec="seconds") if row and row[4] else "",
    }
    steps = [serialize_step(row) for row in step_rows]
    return {
        "config": config,
        "steps": steps,
        "inbound_steps": [step for step in steps if step["direction"] == "inbound"],
        "outbound_steps": [step for step in steps if step["direction"] == "outbound"],
    }


def serialize_step(row):
    """Serialize a pipeline step row."""
    return {
        "step_id": row[0],
        "direction": row[1],
        "position": int(row[2]),
        "name": row[3],
        "step_type": row[4],
        "enabled": bool(row[5]),
        "fail_closed": bool(row[6]),
        "llm_config_id": row[7] or "",
        "prompt_template": row[8],
        "created_at": row[9].isoformat(timespec="seconds") if row[9] else "",
        "updated_at": row[10].isoformat(timespec="seconds") if row[10] else "",
    }


def update_webchat_config(payload):
    """Update public webchat access and base prompt."""
    enabled = normalize_bool(payload.get("enabled"), default=False)
    system_prompt = (payload.get("system_prompt") or "").strip()
    if not system_prompt:
        raise ValueError("Le system prompt webchat est obligatoire.")
    temperature = normalize_temperature(payload.get("temperature"))
    llm_config_id = normalize_config_id(payload.get("llm_config_id"))
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                UPDATE public.webchat_config
                SET enabled = %s,
                    llm_config_id = %s,
                    system_prompt = %s,
                    temperature = %s,
                    updated_at = now()
                WHERE id = 1;
                """,
                (enabled, llm_config_id, system_prompt, temperature),
            )
        conn.commit()
    return get_webchat_config()


def add_pipeline_step(payload):
    """Add a webchat pipeline step."""
    direction = normalize_direction(payload.get("direction"))
    step_type = normalize_step_type(payload.get("step_type"))
    name = (payload.get("name") or "").strip()
    prompt_template = (payload.get("prompt_template") or "").strip()
    if not name:
        raise ValueError("Le nom de l'etape est obligatoire.")
    if not prompt_template:
        raise ValueError("Le prompt template est obligatoire.")
    step_id = uuid4().hex
    position = normalize_position(payload.get("position"))
    enabled = "enabled" in payload and normalize_bool(payload.get("enabled"), default=False)
    fail_closed = "fail_closed" in payload and normalize_bool(payload.get("fail_closed"), default=False)
    llm_config_id = normalize_config_id(payload.get("llm_config_id"))
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                INSERT INTO public.webchat_pipeline_step
                    (
                        step_id, direction, position, name, step_type, enabled,
                        fail_closed, llm_config_id, prompt_template
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    step_id,
                    direction,
                    position,
                    name,
                    step_type,
                    enabled,
                    fail_closed,
                    llm_config_id,
                    prompt_template,
                ),
            )
        conn.commit()
    return step_id


def update_pipeline_step(step_id, payload):
    """Update one webchat pipeline step."""
    cleaned_step_id = (step_id or "").strip()
    if not cleaned_step_id:
        raise ValueError("Etape introuvable.")
    direction = normalize_direction(payload.get("direction"))
    step_type = normalize_step_type(payload.get("step_type"))
    name = (payload.get("name") or "").strip()
    prompt_template = (payload.get("prompt_template") or "").strip()
    if not name:
        raise ValueError("Le nom de l'etape est obligatoire.")
    if not prompt_template:
        raise ValueError("Le prompt template est obligatoire.")
    llm_config_id = normalize_config_id(payload.get("llm_config_id"))
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                UPDATE public.webchat_pipeline_step
                SET direction = %s,
                    position = %s,
                    name = %s,
                    step_type = %s,
                    enabled = %s,
                    fail_closed = %s,
                    llm_config_id = %s,
                    prompt_template = %s,
                    updated_at = now()
                WHERE step_id = %s;
                """,
                (
                    direction,
                    normalize_position(payload.get("position")),
                    name,
                    step_type,
                    normalize_bool(payload.get("enabled"), default=False),
                    normalize_bool(payload.get("fail_closed"), default=False),
                    llm_config_id,
                    prompt_template,
                    cleaned_step_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError("Etape pipeline introuvable.")
        conn.commit()


def delete_pipeline_step(step_id):
    """Delete one webchat pipeline step."""
    cleaned_step_id = (step_id or "").strip()
    if not cleaned_step_id:
        raise ValueError("Etape introuvable.")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                "DELETE FROM public.webchat_pipeline_step WHERE step_id = %s;",
                (cleaned_step_id,),
            )
            if cur.rowcount == 0:
                raise ValueError("Etape pipeline introuvable.")
        conn.commit()


def webchat_public_status(llm_status=None):
    """Return whether the public webchat can be opened."""
    try:
        payload = get_webchat_config()
        enabled = payload["config"]["enabled"]
        config_id = payload["config"].get("llm_config_id", "")
        db_error = ""
    except Exception as exc:
        enabled = False
        config_id = ""
        db_error = str(exc)
    status = llm_status if llm_status and not config_id else llm_connection_status(config_id=config_id)
    return {
        "enabled": enabled,
        "available": bool(enabled and status.get("connected")),
        "llm_connected": bool(status.get("connected")),
        "llm_status": status,
        "db_error": db_error,
    }


def require_webchat_available():
    """Return webchat availability or raise a public-safe validation error."""
    status = webchat_public_status()
    if status["db_error"]:
        raise ValueError("Webchat indisponible: stockage des sessions inaccessible.")
    if not status["enabled"]:
        raise ValueError("Webchat desactive par l'administration.")
    if not status["llm_connected"]:
        raise ValueError("Webchat indisponible: moteur LLM deconnecte.")
    return status


def ensure_webchat_session(session_id="", user_agent="", ip_address=""):
    """Create or refresh a public webchat session."""
    resolved_session_id = (session_id or "").strip() or uuid4().hex
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                INSERT INTO public.webchat_session (session_id, user_agent, ip_address)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET updated_at = now();
                """,
                (resolved_session_id, user_agent or "", ip_address or ""),
            )
        conn.commit()
    return resolved_session_id


def list_recent_webchat_sessions(limit=100):
    """List recent public webchat sessions for admin."""
    safe_limit = min(max(int(limit or 100), 1), 500)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                SELECT
                    s.session_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    COUNT(m.message_id)::int AS message_count,
                    MAX(m.created_at) AS last_message_at
                FROM public.webchat_session s
                LEFT JOIN public.webchat_message m ON m.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.updated_at DESC
                LIMIT %s;
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()
        conn.commit()
    return [
        {
            "session_id": row[0],
            "status": row[1],
            "created_at": row[2].isoformat(timespec="seconds") if row[2] else "",
            "updated_at": row[3].isoformat(timespec="seconds") if row[3] else "",
            "message_count": int(row[4]),
            "last_message_at": row[5].isoformat(timespec="seconds") if row[5] else "",
        }
        for row in rows
    ]


def get_webchat_session_detail(session_id):
    """Return one webchat session and its messages."""
    requested_session_id = (session_id or "").strip()
    if not requested_session_id:
        raise ValueError("Session webchat introuvable.")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                SELECT session_id, status, user_agent, ip_address, created_at, updated_at
                FROM public.webchat_session
                WHERE session_id = %s;
                """,
                (requested_session_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Session webchat introuvable: {requested_session_id}")
            cur.execute(
                """
                SELECT message_id, role, content, raw_content, pipeline_trace, metadata, created_at
                FROM public.webchat_message
                WHERE session_id = %s
                ORDER BY created_at ASC;
                """,
                (requested_session_id,),
            )
            message_rows = cur.fetchall()
        conn.commit()
    return {
        "session_id": row[0],
        "status": row[1],
        "user_agent": row[2] or "",
        "ip_address": row[3] or "",
        "created_at": row[4].isoformat(timespec="seconds") if row[4] else "",
        "updated_at": row[5].isoformat(timespec="seconds") if row[5] else "",
        "messages": [
            {
                "message_id": item[0],
                "role": item[1],
                "content": item[2],
                "raw_content": item[3] or "",
                "pipeline_trace": item[4] or [],
                "pipeline_trace_json": json.dumps(item[4] or [], ensure_ascii=False, indent=2),
                "metadata": item[5] or {},
                "metadata_json": json.dumps(item[5] or {}, ensure_ascii=False, indent=2),
                "created_at": item[6].isoformat(timespec="seconds") if item[6] else "",
            }
            for item in message_rows
        ],
    }


def store_webchat_message(session_id, role, content, raw_content="", pipeline_trace=None, metadata=None):
    """Store a public webchat message."""
    message_id = uuid4().hex
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                INSERT INTO public.webchat_message
                    (message_id, session_id, role, content, raw_content, pipeline_trace, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    message_id,
                    session_id,
                    role,
                    content,
                    raw_content or "",
                    Json(pipeline_trace or []),
                    Json(metadata or {}),
                ),
            )
            cur.execute(
                """
                UPDATE public.webchat_session
                SET updated_at = now()
                WHERE session_id = %s;
                """,
                (session_id,),
            )
        conn.commit()
    return message_id


def load_conversation_messages(session_id, limit=16):
    """Load recent public webchat messages for context."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_webchat_tables(cur)
            cur.execute(
                """
                SELECT role, content
                FROM public.webchat_message
                WHERE session_id = %s
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (session_id, min(max(int(limit), 1), 50)),
            )
            rows = cur.fetchall()
        conn.commit()
    return [
        {"role": role, "content": content}
        for role, content in reversed(rows)
        if role in {"user", "assistant"}
    ]


def compact_conversation_context(messages):
    """Build a compact text context for pipeline prompts."""
    lines = []
    for item in messages[-8:]:
        lines.append(f"{item['role']}: {item['content']}")
    return "\n".join(lines)


def apply_prompt_template(template, values):
    """Render an admin prompt template with safe placeholders."""
    cleaned_template = template or "{input}"
    formatter = Formatter()
    try:
        formatter.parse(cleaned_template)
        return cleaned_template.format_map(SafeFormatDict(values))
    except (KeyError, ValueError):
        return cleaned_template


def parse_guard_output(raw_output):
    """Parse a guard step output.

    Guard steps should preferably return JSON:
    {"allowed": true, "content": "...", "reason": "..."}
    """
    cleaned = (raw_output or "").strip()
    parsed = None
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        parsed = None

    if isinstance(parsed, dict):
        allowed = bool(parsed.get("allowed", True))
        content = str(parsed.get("content") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        return allowed, content, reason

    lowered = cleaned.lower()
    if lowered.startswith("block") or lowered.startswith("refuse") or "allowed:false" in lowered:
        return False, "", cleaned
    return True, cleaned, ""


def execute_pipeline_step(step, text, values):
    """Execute one LLM-backed pipeline step."""
    prompt = apply_prompt_template(
        step["prompt_template"],
        {
            **values,
            "input": text,
        },
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Tu es une etape de traitement interne du webchat. "
                "Retourne uniquement le resultat attendu par le prompt."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    llm_payload = call_llm_chat_completion(
        messages,
        purpose=f"webchat_pipeline_{step['direction']}",
        metadata={
            "step_id": step["step_id"],
            "step_name": step["name"],
            "step_type": step["step_type"],
            "direction": step["direction"],
            "llm_config_id": step.get("llm_config_id", ""),
        },
        temperature=0.0,
        config_id=step.get("llm_config_id", ""),
    )
    output = extract_chat_completion_content(llm_payload)
    trace = {
        "step_id": step["step_id"],
        "name": step["name"],
        "direction": step["direction"],
        "step_type": step["step_type"],
        "input": text,
        "output": output,
        "llm_config_id": step.get("llm_config_id", ""),
        "audit_session_id": llm_payload.get("_audit_session_id", ""),
        "status": "success",
    }

    if step["step_type"] == "llm_guard":
        allowed, guarded_content, reason = parse_guard_output(output)
        trace["allowed"] = allowed
        trace["reason"] = reason
        if not allowed:
            trace["status"] = "blocked"
            raise ValueError(reason or DEFAULT_GUARD_BLOCK_MESSAGE)
        return guarded_content or text, trace

    return output, trace


def run_pipeline(direction, text, original_user_message, conversation_messages, base_response=""):
    """Run all enabled pipeline steps for one direction."""
    payload = get_webchat_config()
    steps = [
        step for step in payload["steps"]
        if step["enabled"] and step["direction"] == direction
    ]
    current_text = text
    trace = []
    values = {
        "original_user_message": original_user_message,
        "current_response": base_response or text,
        "conversation_context": compact_conversation_context(conversation_messages),
    }
    for step in steps:
        try:
            current_text, step_trace = execute_pipeline_step(step, current_text, values)
            trace.append(step_trace)
            values["current_response"] = current_text
        except Exception as exc:
            step_trace = {
                "step_id": step["step_id"],
                "name": step["name"],
                "direction": step["direction"],
                "step_type": step["step_type"],
                "input": current_text,
                "llm_config_id": step.get("llm_config_id", ""),
                "status": "error",
                "error": str(exc),
            }
            trace.append(step_trace)
            if step["fail_closed"]:
                raise
    return current_text, trace


def build_final_llm_messages(config, conversation_messages, user_text):
    """Build final webchat LLM messages."""
    messages = [{"role": "system", "content": config["system_prompt"]}]
    messages.extend(conversation_messages[-16:])
    messages.append({"role": "user", "content": user_text})
    return messages


def process_public_webchat_message(payload, user_agent="", ip_address=""):
    """Process a public webchat message through inbound, core LLM, and outbound chains."""
    require_webchat_available()
    raw_message = (payload.get("message") or "").strip() if isinstance(payload, dict) else ""
    if not raw_message:
        raise ValueError("Message vide.")
    if len(raw_message) > 8000:
        raise ValueError("Message trop long.")

    session_id = ensure_webchat_session(
        (payload.get("session_id") or "").strip() if isinstance(payload, dict) else "",
        user_agent=user_agent,
        ip_address=ip_address,
    )
    config_payload = get_webchat_config()
    conversation_messages = load_conversation_messages(session_id)

    inbound_text, inbound_trace = run_pipeline(
        "inbound",
        raw_message,
        original_user_message=raw_message,
        conversation_messages=conversation_messages,
    )
    user_message_id = store_webchat_message(
        session_id,
        "user",
        inbound_text,
        raw_content=raw_message,
        pipeline_trace=inbound_trace,
    )

    final_messages = build_final_llm_messages(
        config_payload["config"],
        conversation_messages,
        inbound_text,
    )
    llm_payload = call_llm_chat_completion(
        final_messages,
        purpose="webchat_response",
        metadata={
            "webchat_session_id": session_id,
            "user_message_id": user_message_id,
            "llm_config_id": config_payload["config"].get("llm_config_id", ""),
        },
        temperature=config_payload["config"]["temperature"],
        config_id=config_payload["config"].get("llm_config_id", ""),
    )
    base_response = extract_chat_completion_content(llm_payload)
    reasoning_text = extract_chat_completion_reasoning(llm_payload)

    outbound_text, outbound_trace = run_pipeline(
        "outbound",
        base_response,
        original_user_message=raw_message,
        conversation_messages=[*conversation_messages, {"role": "user", "content": inbound_text}],
        base_response=base_response,
    )
    assistant_message_id = store_webchat_message(
        session_id,
        "assistant",
        outbound_text,
        raw_content=base_response,
        pipeline_trace=outbound_trace,
        metadata={
            "audit_session_id": llm_payload.get("_audit_session_id", ""),
            "reasoning": reasoning_text,
        },
    )
    return {
        "session_id": session_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "message": outbound_text,
        "raw_response": base_response,
        "reasoning": reasoning_text,
        "inbound_trace": inbound_trace,
        "outbound_trace": outbound_trace,
        "audit_session_id": llm_payload.get("_audit_session_id", ""),
        "created_at": now_iso(),
    }
