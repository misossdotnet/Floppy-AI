"""Cross-cutting HTTP security: JWT, RBAC, rate limits, CORS and audit."""

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from flask import g, request, session

LOGGER = logging.getLogger("floppy_ai.security")
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS = defaultdict(deque)
_REVOKED_JTIS = set()
_SECURITY_TABLES_READY = False
_SECURITY_TABLES_LOCK = threading.Lock()

UI_ROLES = {"viewer", "editor", "admin"}
ROLE_SCOPES = {
    "viewer": {"read"},
    "editor": {"read", "write", "approve", "build_dataset"},
    "admin": {"admin"},
}
API_ENDPOINTS = {
    "api_project_imports", "api_document_normalize", "api_project_chunk",
    "api_project_build_dataset", "api_dataset_build_get", "api_chunks_list",
    "api_document_lineage", "api_document_approve", "auth_token", "auth_refresh",
    "auth_revoke",
}


def _bool_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "oui"}


def _b64encode(payload):
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _b64decode(payload):
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))


def jwt_secret():
    secret = (os.getenv("FLOPPY_JWT_SECRET") or os.getenv("FLASK_SECRET_KEY") or "").strip()
    if not secret:
        raise RuntimeError("FLOPPY_JWT_SECRET est obligatoire pour emettre des JWT.")
    return secret.encode("utf-8")


def issue_jwt(subject, scopes, token_type="access", lifetime_seconds=None, family_id=None):
    """Issue a signed HS256 JWT with expiry and rotation metadata."""
    now = int(time.time())
    default_lifetime = 900 if token_type == "access" else 604800
    lifetime = int(lifetime_seconds or default_lifetime)
    payload = {
        "sub": str(subject),
        "scopes": sorted(set(scopes)),
        "type": token_type,
        "iat": now,
        "exp": now + max(30, lifetime),
        "jti": uuid4().hex,
        "family": family_id or uuid4().hex,
        "iss": "floppy-ai",
    }
    header_part = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_part = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_part}.{payload_part}".encode()
    signature = hmac.new(jwt_secret(), signing_input, hashlib.sha256).digest()
    return f"{header_part}.{payload_part}.{_b64encode(signature)}", payload


def decode_jwt(token, expected_type=None):
    """Validate a signed JWT and return its claims."""
    try:
        header_part, payload_part, signature_part = token.split(".")
        signing_input = f"{header_part}.{payload_part}".encode()
        signature = _b64decode(signature_part)
        expected = hmac.new(jwt_secret(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        header = json.loads(_b64decode(header_part))
        payload = json.loads(_b64decode(payload_part))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if header.get("alg") != "HS256" or payload.get("iss") != "floppy-ai":
        return None
    if int(payload.get("exp", 0)) <= int(time.time()):
        return None
    if is_jti_revoked(payload.get("jti")):
        return None
    if expected_type and payload.get("type") != expected_type:
        return None
    return payload


def revoke_jwt(claims):
    """Revoke a JWT by its unique identifier."""
    if claims and claims.get("jti"):
        _REVOKED_JTIS.add(claims["jti"])
        try:
            ensure_security_tables()
            from db import get_db_connection
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.auth_token_revocation (jti, subject, expires_at)
                        VALUES (%s, %s, to_timestamp(%s))
                        ON CONFLICT (jti) DO NOTHING;
                        """,
                        (claims["jti"], claims.get("sub", ""), int(claims.get("exp", time.time()))),
                    )
        except Exception:
            LOGGER.exception("Impossible de persister la revocation JWT.")


def ensure_security_tables():
    """Create persistent security journals once per process."""
    global _SECURITY_TABLES_READY
    if _SECURITY_TABLES_READY:
        return
    with _SECURITY_TABLES_LOCK:
        if _SECURITY_TABLES_READY:
            return
        from db import get_db_connection
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.auth_token_revocation (
                        jti text PRIMARY KEY,
                        subject text NOT NULL DEFAULT '',
                        expires_at timestamptz NOT NULL,
                        revoked_at timestamptz NOT NULL DEFAULT now()
                    );
                    CREATE INDEX IF NOT EXISTS auth_token_revocation_expiry_idx
                    ON public.auth_token_revocation(expires_at);
                    CREATE TABLE IF NOT EXISTS public.business_audit_event (
                        event_id text PRIMARY KEY,
                        actor text NOT NULL,
                        role text NOT NULL DEFAULT '',
                        action text NOT NULL,
                        http_method text NOT NULL,
                        resource_path text NOT NULL,
                        status_code integer NOT NULL,
                        remote_addr text NOT NULL DEFAULT '',
                        details jsonb NOT NULL DEFAULT '{}'::jsonb,
                        created_at timestamptz NOT NULL DEFAULT now()
                    );
                    CREATE INDEX IF NOT EXISTS business_audit_created_idx
                    ON public.business_audit_event(created_at DESC);
                    """
                )
        _SECURITY_TABLES_READY = True


def is_jti_revoked(jti):
    if not jti:
        return True
    if jti in _REVOKED_JTIS:
        return True
    try:
        ensure_security_tables()
        from db import get_db_connection
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM public.auth_token_revocation WHERE jti = %s;", (jti,))
                return cur.fetchone() is not None
    except Exception:
        LOGGER.warning("Verification DB de revocation JWT indisponible; cache local utilise.")
        app_env = (os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "development").lower()
        return app_env not in {"local", "dev", "development", "test"}


def configured_api_users():
    """Load API users from JSON: {username: {password, scopes}}."""
    raw = (os.getenv("FLOPPY_API_USERS") or "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            LOGGER.error("FLOPPY_API_USERS doit etre un objet JSON valide.")
    return {}


def configured_ui_users():
    """Load UI users from JSON: {username: {password, role}}."""
    raw = (os.getenv("FLOPPY_UI_USERS") or "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            LOGGER.error("FLOPPY_UI_USERS doit etre un objet JSON valide.")
    return {}


def verify_ui_user(username, password):
    users = configured_ui_users()
    if not users:
        return None
    entry = users.get(username)
    if not isinstance(entry, dict):
        return None
    if not hmac.compare_digest(str(entry.get("password", "")), password or ""):
        return None
    role = str(entry.get("role", "viewer")).lower()
    return role if role in UI_ROLES else None


def role_allows(role, required_role):
    hierarchy = {"viewer": 1, "editor": 2, "admin": 3}
    return hierarchy.get(role, 0) >= hierarchy.get(required_role, 99)


def require_ui_role(required_role):
    """Restrict an HTML route to a minimum UI role."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not role_allows(session.get("admin_role", ""), required_role):
                from services import auth_failure_response
                return auth_failure_response(403, "Permissions UI insuffisantes.")
            return func(*args, **kwargs)
        return wrapper
    return decorator


def _rate_rule():
    path = request.path
    if path.startswith("/mcp"):
        return "mcp", int(os.getenv("FLOPPY_RATE_LIMIT_MCP", "120"))
    if path.startswith("/api/") or request.endpoint in API_ENDPOINTS:
        return "api", int(os.getenv("FLOPPY_RATE_LIMIT_API", "120"))
    if "webchat" in (request.endpoint or "") or "quizbot" in (request.endpoint or ""):
        return "public", int(os.getenv("FLOPPY_RATE_LIMIT_PUBLIC", "30"))
    return None, 0


def request_client_ip():
    """Return a proxy-derived address only when proxy headers are trusted."""
    remote_addr = request.remote_addr or "unknown"
    if not _bool_env("FLOPPY_TRUST_PROXY_HEADERS", False):
        return remote_addr
    forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded_for or remote_addr


def enforce_rate_limit():
    """Apply a fixed one-minute sliding-window limit to exposed endpoints."""
    if not _bool_env("FLOPPY_RATE_LIMIT_ENABLED", True):
        return None
    group, limit = _rate_rule()
    if not group or limit <= 0:
        return None
    client = request_client_ip()
    key = (group, client)
    now = time.monotonic()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[key]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            from services import api_error_response
            response, status = api_error_response(
                "Trop de requetes. Reessayez dans une minute.", 429, "rate_limited"
            )
            return response, status, {"Retry-After": "60"}
        bucket.append(now)
    return None


def apply_cors(response):
    """Apply an explicit allow-list CORS policy to API and MCP responses."""
    if not request.path.startswith(("/api/", "/mcp")) and request.endpoint not in API_ENDPOINTS:
        return response
    allowed = {
        origin.strip()
        for origin in (os.getenv("FLOPPY_CORS_ORIGINS") or "").split(",")
        if origin.strip()
    }
    origin = request.headers.get("Origin", "")
    if origin and origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, X-Floppy-Token, X-Api-Token"
        )
        response.headers["Access-Control-Max-Age"] = "600"
    return response


def audit_sensitive_action(response):
    """Emit a structured audit record for sensitive mutating actions."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return response
    endpoint = request.endpoint or ""
    keywords = ("delete", "remove", "approve", "build", "import", "normalize", "chunk", "save", "update")
    if not any(keyword in endpoint for keyword in keywords):
        return response
    claims = getattr(g, "auth_claims", {}) or {}
    actor = claims.get("sub") or session.get("admin_username") or "anonymous"
    event = {
        "event_id": uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "role": session.get("admin_role", ""),
        "endpoint": endpoint,
        "method": request.method,
        "path": request.path,
        "status": response.status_code,
        "remote_addr": request_client_ip(),
    }
    LOGGER.info("business_audit=%s", json.dumps(event, separators=(",", ":"), sort_keys=True))
    try:
        ensure_security_tables()
        from db import get_db_connection
        from psycopg2.extras import Json
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.business_audit_event
                        (event_id, actor, role, action, http_method, resource_path,
                         status_code, remote_addr, details)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        event["event_id"], actor, event["role"], endpoint, request.method,
                        request.path, response.status_code, event["remote_addr"],
                        Json({"query": request.args.to_dict(flat=True)}),
                    ),
                )
    except Exception:
        LOGGER.exception("Echec de persistence de l'audit metier event_id=%s", event["event_id"])
    return response


def register_security(app):
    """Register cross-cutting security hooks and JWT lifecycle endpoints."""
    app.before_request(enforce_rate_limit)

    @app.after_request
    def security_response_hooks(response):
        return apply_cors(audit_sensitive_action(response))

    @app.route("/api/v1/auth/token", methods=["POST", "OPTIONS"])
    def auth_token():
        if request.method == "OPTIONS":
            return "", 204
        from services import api_error_response
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        entry = configured_api_users().get(username)
        if not isinstance(entry, dict) or not hmac.compare_digest(str(entry.get("password", "")), password):
            return api_error_response(status_code=401, code="unauthorized")
        scopes = entry.get("scopes", [])
        if isinstance(scopes, str):
            scopes = scopes.split("|")
        access, access_claims = issue_jwt(username, scopes, "access", int(os.getenv("FLOPPY_JWT_ACCESS_TTL", "900")))
        refresh, _ = issue_jwt(
            username, scopes, "refresh", int(os.getenv("FLOPPY_JWT_REFRESH_TTL", "604800")),
            access_claims["family"],
        )
        return {"ok": True, "access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                "expires_in": access_claims["exp"] - access_claims["iat"]}

    @app.route("/api/v1/auth/refresh", methods=["POST", "OPTIONS"])
    def auth_refresh():
        if request.method == "OPTIONS":
            return "", 204
        from services import api_error_response
        payload = request.get_json(silent=True) or {}
        claims = decode_jwt(str(payload.get("refresh_token", "")), "refresh")
        if not claims:
            return api_error_response(status_code=401, code="unauthorized")
        revoke_jwt(claims)
        access, access_claims = issue_jwt(claims["sub"], claims.get("scopes", []), "access",
                                          int(os.getenv("FLOPPY_JWT_ACCESS_TTL", "900")), claims["family"])
        refresh, _ = issue_jwt(claims["sub"], claims.get("scopes", []), "refresh",
                               int(os.getenv("FLOPPY_JWT_REFRESH_TTL", "604800")), claims["family"])
        return {"ok": True, "access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                "expires_in": access_claims["exp"] - access_claims["iat"]}

    @app.post("/api/v1/auth/revoke")
    def auth_revoke():
        from services import api_error_response, extract_request_token
        claims = decode_jwt(extract_request_token())
        if not claims:
            return api_error_response(status_code=401, code="unauthorized")
        revoke_jwt(claims)
        return {"ok": True, "revoked": True}
