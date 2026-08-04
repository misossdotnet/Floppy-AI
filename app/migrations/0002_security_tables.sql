CREATE TABLE public.auth_token_revocation (
    jti text PRIMARY KEY,
    subject text NOT NULL DEFAULT '',
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX auth_token_revocation_expiry_idx
ON public.auth_token_revocation(expires_at);

CREATE TABLE public.business_audit_event (
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

CREATE INDEX business_audit_created_idx
ON public.business_audit_event(created_at DESC);
