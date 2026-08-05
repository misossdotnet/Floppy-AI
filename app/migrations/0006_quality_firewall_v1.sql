CREATE TABLE public.quality_observation (
    observation_id text PRIMARY KEY,
    project_slug text NOT NULL,
    document_id text,
    chunk_id text,
    rule_code text NOT NULL,
    ruleset_version text NOT NULL,
    severity text NOT NULL,
    score_delta numeric(7,6),
    message text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    sha256_raw character(64),
    sha256_normalized character(64),
    normalization_hash_version text NOT NULL,
    canonical_document_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT quality_observation_project_fk
        FOREIGN KEY (project_slug)
        REFERENCES public.project(project_nameslug)
        ON DELETE CASCADE,
    CONSTRAINT quality_observation_target_check
        CHECK ((document_id IS NOT NULL) <> (chunk_id IS NOT NULL)),
    CONSTRAINT quality_observation_severity_check
        CHECK (severity IN ('info', 'warning', 'error')),
    CONSTRAINT quality_observation_score_delta_check
        CHECK (score_delta IS NULL OR (score_delta >= -1 AND score_delta <= 1)),
    CONSTRAINT quality_observation_message_check
        CHECK (char_length(message) BETWEEN 1 AND 500),
    CONSTRAINT quality_observation_evidence_object_check
        CHECK (jsonb_typeof(evidence) = 'object'),
    CONSTRAINT quality_observation_evidence_size_check
        CHECK (octet_length(evidence::text) <= 4096),
    CONSTRAINT quality_observation_sha256_raw_check
        CHECK (sha256_raw IS NULL OR sha256_raw ~ '^[0-9a-f]{64}$'),
    CONSTRAINT quality_observation_sha256_normalized_check
        CHECK (
            sha256_normalized IS NULL
            OR sha256_normalized ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT quality_observation_canonical_document_check
        CHECK (
            canonical_document_id IS NULL
            OR (
                document_id IS NOT NULL
                AND canonical_document_id <> document_id
            )
        )
);

CREATE UNIQUE INDEX quality_observation_rule_target_uidx
ON public.quality_observation (
    project_slug,
    COALESCE(document_id, ''),
    COALESCE(chunk_id, ''),
    rule_code,
    ruleset_version
);

CREATE INDEX quality_observation_document_idx
ON public.quality_observation (
    project_slug,
    document_id,
    ruleset_version,
    created_at DESC
)
WHERE document_id IS NOT NULL;

CREATE INDEX quality_observation_chunk_idx
ON public.quality_observation (
    project_slug,
    chunk_id,
    ruleset_version,
    created_at DESC
)
WHERE chunk_id IS NOT NULL;

CREATE INDEX quality_observation_raw_hash_idx
ON public.quality_observation (project_slug, sha256_raw)
WHERE document_id IS NOT NULL AND sha256_raw IS NOT NULL;

CREATE INDEX quality_observation_normalized_hash_idx
ON public.quality_observation (
    project_slug,
    normalization_hash_version,
    sha256_normalized
)
WHERE document_id IS NOT NULL AND sha256_normalized IS NOT NULL;
