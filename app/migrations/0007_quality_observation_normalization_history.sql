DROP INDEX public.quality_observation_rule_target_uidx;

CREATE UNIQUE INDEX quality_observation_rule_target_uidx
ON public.quality_observation (
    project_slug,
    COALESCE(document_id, ''),
    COALESCE(chunk_id, ''),
    rule_code,
    ruleset_version,
    normalization_hash_version
);
