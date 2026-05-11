"""Local Ollama model comparison service and persistence."""

import base64
import csv
import io
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

from psycopg2.extras import Json

from db import get_db_connection
from llm_gateway import (
    call_llm_chat_completion_with_config,
    extract_chat_completion_content,
    get_llm_config_by_id,
    headers_for_config,
    list_llm_configs,
    normalize_config_id,
    normalize_max_tokens,
    normalize_retries,
    normalize_runtime_config,
    status_url_for_config,
)


LLM_COMPARATOR_RUN_TABLE = "llm_comparator_run"
LLM_COMPARATOR_RESULT_TABLE = "llm_comparator_result"
SUPPORTED_MODES = ("benchmark", "custom", "tool_calling", "vision")
DEFAULT_SYSTEM_PROMPT = (
    "You are being evaluated in a local model comparison. Answer the user task "
    "directly, follow all requested output constraints, and do not mention the benchmark."
)
DEFAULT_CUSTOM_PROMPT = "Explain in three practical bullets how to choose a local LLM for a private knowledge-base assistant."
DEFAULT_TOOL_PROMPT = (
    "Use the available tool to request weather for Lyon tomorrow, then explain in one sentence "
    "what tool call you prepared. Do not invent weather values."
)
DEFAULT_VISION_PROMPT = "Describe the visible image in one concise paragraph, then list any uncertainty."
MAX_CUSTOM_PROMPT_CHARS = 12000
MAX_SYSTEM_PROMPT_CHARS = 6000
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Small PNG fixture used when no image is uploaded. It is intentionally tiny; it
# validates multimodal plumbing without storing large binary assets in the repo.
VISION_FIXTURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "lookup_weather",
            "description": "Return a weather forecast for a city and date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                    "date": {"type": "string", "description": "Forecast date or relative date"},
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature unit",
                    },
                },
                "required": ["city", "date"],
            },
        },
    }
]

BENCHMARKS = [
    {
        "benchmark_id": "summarization_en",
        "category": "summarization",
        "language": "en",
        "difficulty": "easy",
        "focus": "Concise synthesis and retention of key facts",
        "expected_output": "Five bullets with no invented facts.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Summarize this note in exactly five bullets:\n\n"
            "A small team is moving its internal documentation from scattered folders into a local RAG system. "
            "They need private processing, clear ownership, versioned source documents, and a repeatable way to "
            "check whether retrieved chunks still match approved content. They also want non-technical staff to "
            "flag outdated answers without editing source files directly."
        ),
    },
    {
        "benchmark_id": "summarization_fr",
        "category": "summarization",
        "language": "fr",
        "difficulty": "easy",
        "focus": "Synthese concise et preservation des faits",
        "expected_output": "Cinq puces sans information inventee.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Resume cette note en exactement cinq puces:\n\n"
            "Une petite equipe migre sa documentation interne depuis des dossiers disperses vers un systeme RAG local. "
            "Elle veut un traitement prive, une propriete claire des documents, des sources versionnees et une methode "
            "repetable pour verifier que les chunks retrouves correspondent encore au contenu approuve. Elle veut aussi "
            "permettre aux utilisateurs non techniques de signaler les reponses obsoletes sans modifier les sources."
        ),
    },
    {
        "benchmark_id": "structured_extraction_en",
        "category": "structured extraction",
        "language": "en",
        "difficulty": "medium",
        "focus": "JSON extraction and stable fields",
        "expected_output": "Valid JSON with project, owner, due_date, risks.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Extract JSON only with keys project, owner, due_date, risks from this text:\n"
            "Project Atlas is owned by Mina. It should be ready by 2026-06-15. Risks: missing GPU capacity, "
            "unclear review owner, and incomplete benchmark prompts."
        ),
    },
    {
        "benchmark_id": "structured_extraction_fr",
        "category": "structured extraction",
        "language": "fr",
        "difficulty": "medium",
        "focus": "Extraction JSON et champs stables",
        "expected_output": "JSON valide avec project, owner, due_date, risks.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Extrais uniquement un JSON avec les cles project, owner, due_date, risks depuis ce texte:\n"
            "Le projet Atlas est porte par Mina. Il doit etre pret le 2026-06-15. Risques: capacite GPU manquante, "
            "responsable de revue peu clair et prompts de benchmark incomplets."
        ),
    },
    {
        "benchmark_id": "classification_en",
        "category": "classification",
        "language": "en",
        "difficulty": "easy",
        "focus": "Label selection with short rationale",
        "expected_output": "One label plus one sentence.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Classify the request as one of: bug, feature, support, security. "
            "Request: 'The local model comparison page should export failed runs so I can attach them to an incident.'"
        ),
    },
    {
        "benchmark_id": "classification_fr",
        "category": "classification",
        "language": "fr",
        "difficulty": "easy",
        "focus": "Choix d'etiquette avec justification courte",
        "expected_output": "Une etiquette puis une phrase.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Classe la demande parmi: bug, feature, support, security. "
            "Demande: 'La page de comparaison locale doit exporter les runs en erreur pour les joindre a un incident.'"
        ),
    },
    {
        "benchmark_id": "reasoning_en",
        "category": "reasoning",
        "language": "en",
        "difficulty": "medium",
        "focus": "Stepwise arithmetic without hidden assumptions",
        "expected_output": "Final number and short explanation.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "A benchmark run tests 3 models on 8 prompts. Two model-prompt attempts fail before generation. "
            "Each successful attempt takes 42 seconds on average. What is the total successful generation time in minutes?"
        ),
    },
    {
        "benchmark_id": "reasoning_fr",
        "category": "reasoning",
        "language": "fr",
        "difficulty": "medium",
        "focus": "Calcul explicite sans hypothese cachee",
        "expected_output": "Nombre final et courte explication.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Un run de benchmark teste 3 modeles sur 8 prompts. Deux tentatives modele-prompt echouent avant generation. "
            "Chaque tentative reussie dure 42 secondes en moyenne. Quel est le temps total de generation reussie en minutes ?"
        ),
    },
    {
        "benchmark_id": "data_analysis_en",
        "category": "data analysis",
        "language": "en",
        "difficulty": "medium",
        "focus": "Small table analysis and ranking",
        "expected_output": "Best model by reliability and speed caveat.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Analyze this table and recommend a model for a quick internal demo. Mention the main caveat.\n\n"
            "| model | success_rate | avg_latency_s | tokens_per_s |\n"
            "| --- | ---: | ---: | ---: |\n"
            "| llama-a | 0.95 | 18 | 31 |\n"
            "| mistral-b | 0.88 | 12 | 44 |\n"
            "| qwen-c | 0.98 | 25 | 28 |"
        ),
    },
    {
        "benchmark_id": "data_analysis_fr",
        "category": "data analysis",
        "language": "fr",
        "difficulty": "medium",
        "focus": "Analyse de tableau et recommandation",
        "expected_output": "Meilleur modele selon fiabilite avec reserve sur vitesse.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Analyse ce tableau et recommande un modele pour une demo interne rapide. Mentionne la principale reserve.\n\n"
            "| modele | taux_succes | latence_moy_s | tokens_par_s |\n"
            "| --- | ---: | ---: | ---: |\n"
            "| llama-a | 0.95 | 18 | 31 |\n"
            "| mistral-b | 0.88 | 12 | 44 |\n"
            "| qwen-c | 0.98 | 25 | 28 |"
        ),
    },
    {
        "benchmark_id": "instruction_following_en",
        "category": "instruction following",
        "language": "en",
        "difficulty": "medium",
        "focus": "Format discipline",
        "expected_output": "Exactly three numbered lines, no extra text.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Return exactly three numbered lines. Line 1 must start with Risk:, line 2 with Mitigation:, "
            "line 3 with Owner:. Topic: comparing local LLMs for a support chatbot."
        ),
    },
    {
        "benchmark_id": "instruction_following_fr",
        "category": "instruction following",
        "language": "fr",
        "difficulty": "medium",
        "focus": "Discipline de format",
        "expected_output": "Exactement trois lignes numerotees, aucun texte en plus.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Retourne exactement trois lignes numerotees. La ligne 1 doit commencer par Risque:, la ligne 2 par Parade:, "
            "la ligne 3 par Responsable:. Sujet: comparer des LLM locaux pour un chatbot support."
        ),
    },
    {
        "benchmark_id": "translation_en",
        "category": "translation",
        "language": "en",
        "difficulty": "easy",
        "focus": "English to French translation",
        "expected_output": "Natural French, same meaning.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": "Translate to French: Local benchmarks measure speed and reliability, but the final answer must still be reviewed by a person.",
    },
    {
        "benchmark_id": "translation_fr",
        "category": "translation",
        "language": "fr",
        "difficulty": "easy",
        "focus": "French to English translation",
        "expected_output": "Natural English, same meaning.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": "Traduis en anglais: Les benchmarks locaux mesurent la vitesse et la fiabilite, mais la reponse finale doit rester relue par une personne.",
    },
    {
        "benchmark_id": "code_generation_en",
        "category": "code generation",
        "language": "en",
        "difficulty": "medium",
        "focus": "Small, correct Python function",
        "expected_output": "Python function and brief note.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Write a Python function count_repeated_lines(text) that returns how many non-empty lines appear more than once. "
            "Keep it short and include one example call."
        ),
    },
    {
        "benchmark_id": "code_generation_fr",
        "category": "code generation",
        "language": "fr",
        "difficulty": "medium",
        "focus": "Petite fonction Python correcte",
        "expected_output": "Fonction Python et courte note.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Ecris une fonction Python count_repeated_lines(text) qui retourne combien de lignes non vides apparaissent plus d'une fois. "
            "Reste concis et ajoute un exemple d'appel."
        ),
    },
    {
        "benchmark_id": "tool_calling_en",
        "category": "tool calling",
        "language": "en",
        "difficulty": "advanced",
        "focus": "Function call argument construction",
        "expected_output": "A tool call or a clear explanation of the intended tool call.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": DEFAULT_TOOL_PROMPT,
        "tools": TOOL_SCHEMA,
        "tool_choice": "auto",
    },
    {
        "benchmark_id": "tool_calling_fr",
        "category": "tool calling",
        "language": "fr",
        "difficulty": "advanced",
        "focus": "Construction d'appel fonction",
        "expected_output": "Un appel outil ou une explication claire de l'appel prevu.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": (
            "Utilise l'outil disponible pour demander la meteo a Lyon demain, puis explique en une phrase "
            "l'appel outil prepare. N'invente pas de valeurs meteo."
        ),
        "tools": TOOL_SCHEMA,
        "tool_choice": "auto",
    },
    {
        "benchmark_id": "vision_review_en",
        "category": "vision review",
        "language": "en",
        "difficulty": "advanced",
        "focus": "Multimodal image description",
        "expected_output": "Concise visual description and uncertainty.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": DEFAULT_VISION_PROMPT,
        "image_data_url": VISION_FIXTURE_DATA_URL,
    },
    {
        "benchmark_id": "vision_review_fr",
        "category": "vision review",
        "language": "fr",
        "difficulty": "advanced",
        "focus": "Description multimodale d'image",
        "expected_output": "Description visuelle concise et incertitude.",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": "Decris l'image visible en un court paragraphe, puis liste toute incertitude.",
        "image_data_url": VISION_FIXTURE_DATA_URL,
    },
]


def now_iso():
    """Return UTC timestamp as ISO text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value, limit=20000):
    """Normalize free text from forms."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def normalize_mode(value):
    """Normalize comparator mode."""
    mode = str(value or "benchmark").strip().lower()
    return mode if mode in SUPPORTED_MODES else "benchmark"


def normalize_temperature(value, default=0.2):
    """Normalize a temperature value."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 0.0), 2.0)


def normalize_bool(value, default=False):
    """Normalize checkbox values."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "oui"}


def payload_list(payload, key):
    """Return a form value as a list."""
    if hasattr(payload, "getlist"):
        return [str(item) for item in payload.getlist(key) if str(item).strip()]
    value = payload.get(key) if isinstance(payload, dict) else None
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def payload_value(payload, key, default=""):
    """Return a scalar form value."""
    if hasattr(payload, "get"):
        value = payload.get(key, default)
    else:
        value = default
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return value


def ensure_llm_comparator_tables(cur):
    """Create Local LLM Comparator tables."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.llm_comparator_run (
            run_id text PRIMARY KEY,
            actor text NOT NULL DEFAULT '',
            mode text NOT NULL DEFAULT 'benchmark',
            benchmark_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            selected_models jsonb NOT NULL DEFAULT '[]'::jsonb,
            settings jsonb NOT NULL DEFAULT '{}'::jsonb,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'running',
            error_message text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS actor text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'benchmark';")
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS benchmark_ids jsonb NOT NULL DEFAULT '[]'::jsonb;")
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS selected_models jsonb NOT NULL DEFAULT '[]'::jsonb;")
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS settings jsonb NOT NULL DEFAULT '{}'::jsonb;")
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS summary jsonb NOT NULL DEFAULT '{}'::jsonb;")
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'running';")
    cur.execute("ALTER TABLE public.llm_comparator_run ADD COLUMN IF NOT EXISTS error_message text NOT NULL DEFAULT '';")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.llm_comparator_result (
            result_id text PRIMARY KEY,
            run_id text NOT NULL REFERENCES public.llm_comparator_run(run_id) ON DELETE CASCADE,
            config_id text NOT NULL DEFAULT '',
            model text NOT NULL DEFAULT '',
            language text NOT NULL DEFAULT '',
            benchmark_id text NOT NULL DEFAULT '',
            system_prompt text NOT NULL DEFAULT '',
            user_prompt text NOT NULL DEFAULT '',
            output_text text NOT NULL DEFAULT '',
            raw_response jsonb NOT NULL DEFAULT '{}'::jsonb,
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            audit_session_id text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'completed',
            error_message text NOT NULL DEFAULT '',
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz
        );
        """
    )
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS config_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS language text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS benchmark_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS system_prompt text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS user_prompt text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS output_text text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS raw_response jsonb NOT NULL DEFAULT '{}'::jsonb;")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS metrics jsonb NOT NULL DEFAULT '{}'::jsonb;")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS audit_session_id text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'completed';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS error_message text NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS started_at timestamptz NOT NULL DEFAULT now();")
    cur.execute("ALTER TABLE public.llm_comparator_result ADD COLUMN IF NOT EXISTS ended_at timestamptz;")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS llm_comparator_run_created_idx
        ON public.llm_comparator_run(created_at DESC);
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS llm_comparator_result_run_idx
        ON public.llm_comparator_result(run_id, model, benchmark_id);
        """
    )


def benchmark_catalog():
    """Return benchmark definitions."""
    return [dict(item) for item in BENCHMARKS]


def benchmark_map():
    """Return benchmarks keyed by id."""
    return {item["benchmark_id"]: dict(item) for item in BENCHMARKS}


def default_benchmark_ids():
    """Return default text-focused benchmark ids."""
    return [
        item["benchmark_id"]
        for item in BENCHMARKS
        if item["category"] not in {"tool calling", "vision review"}
    ]


def discover_ollama_models(config):
    """Discover models from one Ollama-compatible configuration."""
    request = urllib.request.Request(
        status_url_for_config(config),
        headers=headers_for_config(config),
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))

    models = []
    if isinstance(payload.get("data"), list):
        for item in payload["data"]:
            if isinstance(item, dict):
                models.append(item.get("id") or item.get("name") or item.get("model"))
            else:
                models.append(item)
    if isinstance(payload.get("models"), list):
        for item in payload["models"]:
            if isinstance(item, dict):
                models.append(item.get("name") or item.get("model") or item.get("id"))
            else:
                models.append(item)

    return sorted({str(model).strip() for model in models if str(model or "").strip()})


def list_ollama_model_options():
    """List selectable Ollama models from the LLM registry and model discovery."""
    options = []
    discovery_errors = []
    seen = set()
    configs = list_llm_configs(redact_key=False)
    for config in configs:
        if config.get("provider") != "ollama" or not config.get("enabled") or not config.get("api_url"):
            continue
        models = []
        try:
            models = discover_ollama_models(config)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
            discovery_errors.append(
                f"{config.get('name') or config.get('config_id')}: {exc}"
            )
        except Exception as exc:
            discovery_errors.append(
                f"{config.get('name') or config.get('config_id')}: {exc}"
            )

        configured_model = str(config.get("model") or "").strip()
        if configured_model and configured_model not in models:
            models.insert(0, configured_model)

        for model in models:
            key = f"{config['config_id']}::{model}"
            if key in seen:
                continue
            seen.add(key)
            options.append(
                {
                    "value": key,
                    "config_id": config["config_id"],
                    "config_name": config.get("name") or config["config_id"],
                    "provider": config.get("provider", "ollama"),
                    "api_url": config.get("api_url", ""),
                    "model": model,
                    "label": f"{config.get('name') or config['config_id']} / {model}",
                    "profile_type": config.get("profile_type", "general"),
                    "discovered": model != configured_model,
                }
            )
    return {"options": options, "errors": discovery_errors}


def runtime_configs_from_selection(model_keys):
    """Resolve selected model keys into runtime LLM configs."""
    selected = []
    for raw_key in model_keys:
        key = str(raw_key or "").strip()
        if "::" not in key:
            continue
        config_id, model = key.split("::", 1)
        config_id = normalize_config_id(config_id)
        model = model.strip()
        if not config_id or not model:
            continue
        config = get_llm_config_by_id(config_id, redact_key=False)
        if not config or config.get("provider") != "ollama" or not config.get("configured"):
            continue
        runtime = dict(config)
        runtime["model"] = model
        runtime["name"] = f"{config.get('name') or config_id} / {model}"
        runtime = normalize_runtime_config(runtime)
        selected.append(
            {
                "key": key,
                "config_id": config_id,
                "config_name": config.get("name") or config_id,
                "model": model,
                "api_url": config.get("api_url", ""),
                "runtime": runtime,
            }
        )
    if not selected:
        raise ValueError("Selectionnez au moins un modele Ollama disponible.")
    return selected


def uploaded_image_data_url(files):
    """Return uploaded image data URL and metadata, or the fixture image."""
    file_storage = files.get("vision_image") if files and hasattr(files, "get") else None
    if not file_storage or not getattr(file_storage, "filename", ""):
        return VISION_FIXTURE_DATA_URL, {
            "image_source": "fixture",
            "filename": "fixture.png",
            "media_type": "image/png",
            "file_size": 0,
        }

    filename = normalize_text(file_storage.filename, limit=260) or "image"
    media_type = (getattr(file_storage, "mimetype", "") or "").split(";")[0].strip().lower()
    if not media_type.startswith("image/"):
        raise ValueError("Le comparateur vision v1 accepte uniquement des images.")
    file_bytes = file_storage.read()
    if not file_bytes:
        raise ValueError("L'image fournie est vide.")
    if len(file_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Image trop volumineuse. Limite: 8 Mo.")
    encoded = base64.b64encode(file_bytes).decode("ascii")
    return f"data:{media_type};base64,{encoded}", {
        "image_source": "upload",
        "filename": filename,
        "media_type": media_type,
        "file_size": len(file_bytes),
    }


def build_jobs(payload, files):
    """Build model-prompt jobs from a form payload."""
    mode = normalize_mode(payload_value(payload, "mode", "benchmark"))
    if mode == "benchmark":
        selected_ids = payload_list(payload, "benchmark_ids") or default_benchmark_ids()
        catalog = benchmark_map()
        jobs = [catalog[item_id] for item_id in selected_ids if item_id in catalog]
        if not jobs:
            raise ValueError("Selectionnez au moins un benchmark.")
        return mode, jobs, {}

    if mode == "custom":
        system_prompt = normalize_text(
            payload_value(payload, "system_prompt", DEFAULT_SYSTEM_PROMPT),
            limit=MAX_SYSTEM_PROMPT_CHARS,
        ) or DEFAULT_SYSTEM_PROMPT
        user_prompt = normalize_text(
            payload_value(payload, "custom_prompt", DEFAULT_CUSTOM_PROMPT),
            limit=MAX_CUSTOM_PROMPT_CHARS,
        )
        if not user_prompt:
            raise ValueError("Le prompt custom est obligatoire.")
        return mode, [
            {
                "benchmark_id": "custom_prompt",
                "category": "custom prompt",
                "language": "custom",
                "difficulty": "custom",
                "focus": "User-provided prompt",
                "expected_output": "User-defined",
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        ], {}

    if mode == "tool_calling":
        system_prompt = normalize_text(
            payload_value(payload, "system_prompt", DEFAULT_SYSTEM_PROMPT),
            limit=MAX_SYSTEM_PROMPT_CHARS,
        ) or DEFAULT_SYSTEM_PROMPT
        user_prompt = normalize_text(
            payload_value(payload, "tool_prompt", DEFAULT_TOOL_PROMPT),
            limit=MAX_CUSTOM_PROMPT_CHARS,
        ) or DEFAULT_TOOL_PROMPT
        return mode, [
            {
                "benchmark_id": "tool_calling_custom",
                "category": "tool calling",
                "language": "custom",
                "difficulty": "advanced",
                "focus": "Function call argument construction",
                "expected_output": "Tool call or clear intended tool call.",
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": TOOL_SCHEMA,
                "tool_choice": "auto",
            }
        ], {}

    image_data_url, image_metadata = uploaded_image_data_url(files)
    system_prompt = normalize_text(
        payload_value(payload, "system_prompt", DEFAULT_SYSTEM_PROMPT),
        limit=MAX_SYSTEM_PROMPT_CHARS,
    ) or DEFAULT_SYSTEM_PROMPT
    user_prompt = normalize_text(
        payload_value(payload, "vision_prompt", DEFAULT_VISION_PROMPT),
        limit=MAX_CUSTOM_PROMPT_CHARS,
    ) or DEFAULT_VISION_PROMPT
    return mode, [
        {
            "benchmark_id": "vision_custom",
            "category": "vision review",
            "language": "custom",
            "difficulty": "advanced",
            "focus": "User-provided image review",
            "expected_output": "Visual description and uncertainty.",
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "image_data_url": image_data_url,
        }
    ], image_metadata


def build_messages(job):
    """Build chat messages for a comparison job."""
    user_content = job["user_prompt"]
    if job.get("image_data_url"):
        user_content = [
            {"type": "text", "text": job["user_prompt"]},
            {"type": "image_url", "image_url": {"url": job["image_data_url"]}},
        ]
    return [
        {"role": "system", "content": job["system_prompt"]},
        {"role": "user", "content": user_content},
    ]


def extract_output_text(llm_payload):
    """Extract assistant output text, including tool call payloads."""
    try:
        return extract_chat_completion_content(llm_payload)
    except ValueError:
        pass

    try:
        message = llm_payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Format de reponse LLM non supporte.") from exc

    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if tool_calls:
        return json.dumps({"tool_calls": tool_calls}, ensure_ascii=False, indent=2)
    content = message.get("content") if isinstance(message, dict) else ""
    if content is None:
        return ""
    return str(content).strip()


def response_usage_metrics(llm_payload):
    """Extract token counts from common provider response shapes."""
    usage = llm_payload.get("usage") if isinstance(llm_payload, dict) else {}
    provider_response = llm_payload.get("provider_response") if isinstance(llm_payload, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    if not isinstance(provider_response, dict):
        provider_response = {}
    def safe_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt_tokens = usage.get("prompt_tokens") or provider_response.get("prompt_eval_count") or 0
    completion_tokens = (
        usage.get("completion_tokens")
        or provider_response.get("eval_count")
        or usage.get("output_tokens")
        or 0
    )
    total_tokens = usage.get("total_tokens") or (safe_int(prompt_tokens) + safe_int(completion_tokens))
    return {
        "prompt_tokens": safe_int(prompt_tokens),
        "completion_tokens": safe_int(completion_tokens),
        "total_tokens": safe_int(total_tokens),
    }


def repeat_metrics(output_text):
    """Compute simple repeat indicators from model output."""
    lines = [line.strip() for line in output_text.splitlines() if line.strip()]
    repeated_lines = len(lines) - len(set(lines))
    words = re.findall(r"\w+", output_text.lower())
    ngrams = [" ".join(words[index : index + 4]) for index in range(max(len(words) - 3, 0))]
    repeated_ngrams = len(ngrams) - len(set(ngrams)) if ngrams else 0
    repeat_ratio = repeated_ngrams / len(ngrams) if ngrams else 0.0
    return {
        "repeated_lines": repeated_lines,
        "repeated_ngram_count": repeated_ngrams,
        "repeat_ratio": round(repeat_ratio, 4),
    }


def build_metrics(llm_payload, output_text, latency_ms):
    """Build per-result metrics."""
    usage = response_usage_metrics(llm_payload)
    completion_tokens = usage["completion_tokens"]
    seconds = max(latency_ms / 1000, 0.001)
    metrics = {
        "latency_ms": latency_ms,
        "tokens_per_second": round(completion_tokens / seconds, 2) if completion_tokens else 0.0,
        "output_chars": len(output_text or ""),
        "output_words": len(re.findall(r"\w+", output_text or "")),
        "attempt_count": int(llm_payload.get("_attempt_count", 1)) if isinstance(llm_payload, dict) else 1,
    }
    metrics.update(usage)
    metrics.update(repeat_metrics(output_text or ""))
    return metrics


def find_audit_session_id(run_id, model, benchmark_id):
    """Find the latest audit session for a comparator attempt."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_id
                    FROM public.llm_audit_session
                    WHERE purpose = 'llm_comparator'
                      AND metadata->>'run_id' = %s
                      AND metadata->>'model' = %s
                      AND metadata->>'benchmark_id' = %s
                    ORDER BY started_at DESC
                    LIMIT 1;
                    """,
                    (run_id, model, benchmark_id),
                )
                row = cur.fetchone()
                return row[0] if row else ""
    except Exception:
        return ""


def call_model_for_job(selected_model, job, settings, run_id):
    """Run one model against one prompt and return a result payload."""
    runtime = selected_model["runtime"]
    started_at = now_iso()
    start = time.perf_counter()
    metadata = {
        "run_id": run_id,
        "mode": settings["mode"],
        "benchmark_id": job["benchmark_id"],
        "language": job.get("language", ""),
        "config_id": selected_model["config_id"],
        "model": selected_model["model"],
    }
    try:
        llm_payload = call_llm_chat_completion_with_config(
            runtime,
            build_messages(job),
            purpose="llm_comparator",
            metadata=metadata,
            temperature=settings["temperature"],
            max_tokens=settings["max_tokens"],
            retries=settings["retries"],
            json_mode=settings["json_mode"],
            tools=job.get("tools"),
            tool_choice=job.get("tool_choice"),
        )
        latency_ms = int(round((time.perf_counter() - start) * 1000))
        output_text = extract_output_text(llm_payload)
        return {
            "result_id": uuid4().hex,
            "run_id": run_id,
            "config_id": selected_model["config_id"],
            "config_name": selected_model["config_name"],
            "model": selected_model["model"],
            "language": job.get("language", ""),
            "benchmark_id": job["benchmark_id"],
            "category": job.get("category", ""),
            "system_prompt": job["system_prompt"],
            "user_prompt": job["user_prompt"],
            "output_text": output_text,
            "raw_response": llm_payload,
            "metrics": build_metrics(llm_payload, output_text, latency_ms),
            "audit_session_id": llm_payload.get("_audit_session_id", ""),
            "status": "completed",
            "error_message": "",
            "started_at": started_at,
            "ended_at": now_iso(),
        }
    except Exception as exc:
        latency_ms = int(round((time.perf_counter() - start) * 1000))
        audit_session_id = find_audit_session_id(
            run_id,
            selected_model["model"],
            job["benchmark_id"],
        )
        return {
            "result_id": uuid4().hex,
            "run_id": run_id,
            "config_id": selected_model["config_id"],
            "config_name": selected_model["config_name"],
            "model": selected_model["model"],
            "language": job.get("language", ""),
            "benchmark_id": job["benchmark_id"],
            "category": job.get("category", ""),
            "system_prompt": job["system_prompt"],
            "user_prompt": job["user_prompt"],
            "output_text": "",
            "raw_response": {},
            "metrics": {"latency_ms": latency_ms, "tokens_per_second": 0.0},
            "audit_session_id": audit_session_id,
            "status": "error",
            "error_message": str(exc),
            "started_at": started_at,
            "ended_at": now_iso(),
        }


def insert_run(run_id, actor, mode, benchmark_ids, selected_models, settings):
    """Persist a new comparator run."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_comparator_tables(cur)
            cur.execute(
                """
                INSERT INTO public.llm_comparator_run
                    (run_id, actor, mode, benchmark_ids, selected_models, settings, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'running');
                """,
                (
                    run_id,
                    actor or "",
                    mode,
                    Json(benchmark_ids),
                    Json(selected_models),
                    Json(settings),
                ),
            )
        conn.commit()


def insert_result(result):
    """Persist one comparator result."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_comparator_tables(cur)
            cur.execute(
                """
                INSERT INTO public.llm_comparator_result
                    (
                        result_id, run_id, config_id, model, language, benchmark_id,
                        system_prompt, user_prompt, output_text, raw_response, metrics,
                        audit_session_id, status, error_message, started_at, ended_at
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    result["result_id"],
                    result["run_id"],
                    result["config_id"],
                    result["model"],
                    result["language"],
                    result["benchmark_id"],
                    result["system_prompt"],
                    result["user_prompt"],
                    result["output_text"],
                    Json(result["raw_response"]),
                    Json(result["metrics"]),
                    result["audit_session_id"],
                    result["status"],
                    result["error_message"],
                    result["started_at"],
                    result["ended_at"],
                ),
            )
        conn.commit()


def summarize_results(results):
    """Build simple and advanced summary values for a run."""
    successes = [item for item in results if item["status"] == "completed"]
    errors = [item for item in results if item["status"] != "completed"]
    by_model = {}
    for item in results:
        bucket = by_model.setdefault(
            item["model"],
            {"total": 0, "success": 0, "latencies": [], "speeds": [], "repeat_ratios": []},
        )
        bucket["total"] += 1
        if item["status"] == "completed":
            bucket["success"] += 1
            bucket["latencies"].append(item["metrics"].get("latency_ms", 0))
            bucket["speeds"].append(item["metrics"].get("tokens_per_second", 0.0))
            bucket["repeat_ratios"].append(item["metrics"].get("repeat_ratio", 0.0))

    model_scores = []
    for model, bucket in by_model.items():
        avg_latency = sum(bucket["latencies"]) / len(bucket["latencies"]) if bucket["latencies"] else None
        avg_speed = sum(bucket["speeds"]) / len(bucket["speeds"]) if bucket["speeds"] else None
        avg_repeat = sum(bucket["repeat_ratios"]) / len(bucket["repeat_ratios"]) if bucket["repeat_ratios"] else None
        model_scores.append(
            {
                "model": model,
                "success_rate": round(bucket["success"] / bucket["total"], 4) if bucket["total"] else 0,
                "success_count": bucket["success"],
                "total_count": bucket["total"],
                "avg_latency_ms": round(avg_latency, 2) if avg_latency is not None else None,
                "avg_tokens_per_second": round(avg_speed, 2) if avg_speed is not None else None,
                "avg_repeat_ratio": round(avg_repeat, 4) if avg_repeat is not None else None,
            }
        )

    fastest = min(successes, key=lambda item: item["metrics"].get("latency_ms", 10**12), default=None)
    speediest = max(successes, key=lambda item: item["metrics"].get("tokens_per_second", 0), default=None)
    most_reliable = max(
        model_scores,
        key=lambda item: (item["success_rate"], item["success_count"], -(item["avg_latency_ms"] or 10**12)),
        default=None,
    )
    least_repetitive = min(
        [item for item in model_scores if item["avg_repeat_ratio"] is not None],
        key=lambda item: item["avg_repeat_ratio"],
        default=None,
    )
    status = "completed"
    if errors and successes:
        status = "partial_failed"
    elif errors and not successes:
        status = "failed"

    simple_lines = []
    if fastest:
        simple_lines.append(
            f"Le resultat le plus rapide est {fastest['model']} sur {fastest['benchmark_id']}."
        )
    if speediest and speediest["metrics"].get("tokens_per_second"):
        simple_lines.append(
            f"La meilleure vitesse de generation mesuree est {speediest['model']} avec {speediest['metrics']['tokens_per_second']} tokens/s."
        )
    if most_reliable:
        simple_lines.append(
            f"Le modele le plus fiable sur ce run est {most_reliable['model']} ({most_reliable['success_count']}/{most_reliable['total_count']} succes)."
        )
    if errors:
        simple_lines.append(f"{len(errors)} execution(s) ont echoue et doivent etre inspectees.")
    simple_lines.append(
        "Ces mesures de vitesse et de fiabilite ne prouvent pas la qualite des reponses; lisez les prompts et sorties avant de choisir un modele."
    )

    return {
        "status": status,
        "total_results": len(results),
        "success_count": len(successes),
        "error_count": len(errors),
        "fastest": {
            "model": fastest["model"],
            "benchmark_id": fastest["benchmark_id"],
            "latency_ms": fastest["metrics"].get("latency_ms"),
        }
        if fastest
        else None,
        "speediest": {
            "model": speediest["model"],
            "benchmark_id": speediest["benchmark_id"],
            "tokens_per_second": speediest["metrics"].get("tokens_per_second"),
        }
        if speediest
        else None,
        "most_reliable": most_reliable,
        "least_repetitive": least_repetitive,
        "model_scores": model_scores,
        "plain_language": " ".join(simple_lines),
    }


def update_run_summary(run_id, summary, error_message=""):
    """Persist final run summary."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_comparator_tables(cur)
            cur.execute(
                """
                UPDATE public.llm_comparator_run
                SET summary = %s, status = %s, error_message = %s
                WHERE run_id = %s;
                """,
                (Json(summary), summary["status"], error_message or "", run_id),
            )
        conn.commit()


def run_llm_comparison(payload, files=None, actor="admin"):
    """Run a comparison and persist its results."""
    mode, jobs, extra_settings = build_jobs(payload, files)
    selected = runtime_configs_from_selection(payload_list(payload, "selected_model_keys"))
    settings = {
        "mode": mode,
        "temperature": normalize_temperature(payload_value(payload, "temperature", 0.2)),
        "max_tokens": normalize_max_tokens(payload_value(payload, "max_tokens", 800), default=800),
        "retries": normalize_retries(payload_value(payload, "retries", 1), default=1),
        "json_mode": normalize_bool(payload_value(payload, "json_mode", None), default=False),
        "created_at": now_iso(),
    }
    settings.update(extra_settings)
    run_id = uuid4().hex
    selected_models = [
        {
            "config_id": item["config_id"],
            "config_name": item["config_name"],
            "model": item["model"],
            "api_url": item["api_url"],
        }
        for item in selected
    ]
    benchmark_ids = [job["benchmark_id"] for job in jobs]
    insert_run(run_id, actor, mode, benchmark_ids, selected_models, settings)

    results = []
    for job in jobs:
        for selected_model in selected:
            result = call_model_for_job(selected_model, job, settings, run_id)
            insert_result(result)
            results.append(result)

    summary = summarize_results(results)
    update_run_summary(run_id, summary)
    return {"run_id": run_id, "summary": summary}


def serialize_run(row):
    """Serialize one comparator run row."""
    return {
        "run_id": row[0],
        "actor": row[1] or "",
        "mode": row[2] or "",
        "benchmark_ids": row[3] or [],
        "selected_models": row[4] or [],
        "settings": row[5] or {},
        "summary": row[6] or {},
        "status": row[7] or "",
        "error_message": row[8] or "",
        "created_at": row[9].isoformat(timespec="seconds") if row[9] else "",
        "result_count": int(row[10] or 0) if len(row) > 10 else 0,
    }


def serialize_result(row):
    """Serialize one comparator result row."""
    return {
        "result_id": row[0],
        "run_id": row[1],
        "config_id": row[2] or "",
        "model": row[3] or "",
        "language": row[4] or "",
        "benchmark_id": row[5] or "",
        "system_prompt": row[6] or "",
        "user_prompt": row[7] or "",
        "output_text": row[8] or "",
        "raw_response": row[9] or {},
        "metrics": row[10] or {},
        "audit_session_id": row[11] or "",
        "status": row[12] or "",
        "error_message": row[13] or "",
        "started_at": row[14].isoformat(timespec="seconds") if row[14] else "",
        "ended_at": row[15].isoformat(timespec="seconds") if row[15] else "",
        "raw_response_json": json.dumps(row[9] or {}, ensure_ascii=False, indent=2, default=str),
        "metrics_json": json.dumps(row[10] or {}, ensure_ascii=False, indent=2, default=str),
    }


def list_recent_runs(limit=25):
    """List recent comparator runs."""
    safe_limit = min(max(int(limit or 25), 1), 100)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_comparator_tables(cur)
            cur.execute(
                """
                SELECT r.run_id, r.actor, r.mode, r.benchmark_ids, r.selected_models,
                       r.settings, r.summary, r.status, r.error_message, r.created_at,
                       COUNT(res.result_id)::int AS result_count
                FROM public.llm_comparator_run r
                LEFT JOIN public.llm_comparator_result res ON res.run_id = r.run_id
                GROUP BY r.run_id
                ORDER BY r.created_at DESC
                LIMIT %s;
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()
    return [serialize_run(row) for row in rows]


def get_run_detail(run_id):
    """Return one run with its results."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_comparator_tables(cur)
            cur.execute(
                """
                SELECT r.run_id, r.actor, r.mode, r.benchmark_ids, r.selected_models,
                       r.settings, r.summary, r.status, r.error_message, r.created_at,
                       COUNT(res.result_id)::int AS result_count
                FROM public.llm_comparator_run r
                LEFT JOIN public.llm_comparator_result res ON res.run_id = r.run_id
                WHERE r.run_id = %s
                GROUP BY r.run_id;
                """,
                (run_id,),
            )
            run_row = cur.fetchone()
            if not run_row:
                raise ValueError("Run Comparator introuvable.")
            cur.execute(
                """
                SELECT result_id, run_id, config_id, model, language, benchmark_id,
                       system_prompt, user_prompt, output_text, raw_response, metrics,
                       audit_session_id, status, error_message, started_at, ended_at
                FROM public.llm_comparator_result
                WHERE run_id = %s
                ORDER BY started_at ASC, model ASC, benchmark_id ASC;
                """,
                (run_id,),
            )
            result_rows = cur.fetchall()
    run = serialize_run(run_row)
    run["settings_json"] = json.dumps(run["settings"], ensure_ascii=False, indent=2, default=str)
    run["summary_json"] = json.dumps(run["summary"], ensure_ascii=False, indent=2, default=str)
    return {
        "run": run,
        "results": [serialize_result(row) for row in result_rows],
    }


def get_llm_comparator_payload():
    """Return admin page payload."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            ensure_llm_comparator_tables(cur)
        conn.commit()
    model_payload = list_ollama_model_options()
    return {
        "benchmarks": benchmark_catalog(),
        "default_benchmark_ids": set(default_benchmark_ids()),
        "model_options": model_payload["options"],
        "model_errors": model_payload["errors"],
        "recent_runs": list_recent_runs(),
        "defaults": {
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "custom_prompt": DEFAULT_CUSTOM_PROMPT,
            "tool_prompt": DEFAULT_TOOL_PROMPT,
            "vision_prompt": DEFAULT_VISION_PROMPT,
            "temperature": 0.2,
            "max_tokens": 800,
            "retries": 1,
        },
        "modes": SUPPORTED_MODES,
    }


def csv_export(detail):
    """Build CSV export text."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "run_id",
            "result_id",
            "model",
            "config_id",
            "benchmark_id",
            "language",
            "status",
            "latency_ms",
            "tokens_per_second",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "repeat_ratio",
            "audit_session_id",
            "error_message",
            "output_text",
        ],
    )
    writer.writeheader()
    for result in detail["results"]:
        metrics = result["metrics"]
        writer.writerow(
            {
                "run_id": detail["run"]["run_id"],
                "result_id": result["result_id"],
                "model": result["model"],
                "config_id": result["config_id"],
                "benchmark_id": result["benchmark_id"],
                "language": result["language"],
                "status": result["status"],
                "latency_ms": metrics.get("latency_ms", ""),
                "tokens_per_second": metrics.get("tokens_per_second", ""),
                "prompt_tokens": metrics.get("prompt_tokens", ""),
                "completion_tokens": metrics.get("completion_tokens", ""),
                "total_tokens": metrics.get("total_tokens", ""),
                "repeat_ratio": metrics.get("repeat_ratio", ""),
                "audit_session_id": result["audit_session_id"],
                "error_message": result["error_message"],
                "output_text": result["output_text"],
            }
        )
    return output.getvalue()


def markdown_export(detail):
    """Build Markdown export text."""
    run = detail["run"]
    lines = [
        f"# Local LLM Comparator Run {run['run_id']}",
        "",
        f"- Status: {run['status']}",
        f"- Mode: {run['mode']}",
        f"- Actor: {run['actor'] or '-'}",
        f"- Created: {run['created_at']}",
        "",
        "## Simple Analysis",
        "",
        run.get("summary", {}).get("plain_language", ""),
        "",
        "## Results",
        "",
    ]
    for result in detail["results"]:
        metrics = result["metrics"]
        lines.extend(
            [
                f"### {result['model']} - {result['benchmark_id']}",
                "",
                f"- Status: {result['status']}",
                f"- Latency: {metrics.get('latency_ms', '-')} ms",
                f"- Tokens/s: {metrics.get('tokens_per_second', '-')}",
                f"- Audit: {result['audit_session_id'] or '-'}",
                "",
                "System prompt:",
                "",
                "```text",
                result["system_prompt"],
                "```",
                "",
                "User prompt:",
                "",
                "```text",
                result["user_prompt"],
                "```",
                "",
                "Output:",
                "",
                "```text",
                result["output_text"] or result["error_message"],
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def export_run(run_id, export_format):
    """Return export filename, mimetype and body."""
    detail = get_run_detail(run_id)
    fmt = str(export_format or "json").strip().lower()
    if fmt == "csv":
        return f"llm-comparator-{run_id}.csv", "text/csv; charset=utf-8", csv_export(detail)
    if fmt in {"md", "markdown"}:
        return f"llm-comparator-{run_id}.md", "text/markdown; charset=utf-8", markdown_export(detail)
    body = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
    return f"llm-comparator-{run_id}.json", "application/json; charset=utf-8", body
