--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: chunk_metadata; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_metadata (
    chunk_id text NOT NULL,
    project_slug text NOT NULL,
    shard_id text NOT NULL,
    document_id text NOT NULL,
    section_title text,
    section_path text,
    previous_document_id text,
    previous_chunk_id text,
    next_chunk_id text,
    quality_score numeric(5,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    chunk_type text DEFAULT 'markdown'::text NOT NULL,
    chunking_method text DEFAULT 'deterministic'::text NOT NULL,
    llm_config_id text,
    llm_profile_type text,
    llm_audit_session_id text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: dataset_build; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dataset_build (
    build_id text NOT NULL,
    project_slug text NOT NULL,
    status text NOT NULL,
    quality_min numeric(5,4) DEFAULT 0 NOT NULL,
    options jsonb DEFAULT '{}'::jsonb NOT NULL,
    stats jsonb DEFAULT '{}'::jsonb NOT NULL,
    items_preview jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: document_processing; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_processing (
    document_id text NOT NULL,
    project_slug text NOT NULL,
    normalization_version text DEFAULT 'v1'::text NOT NULL,
    normalized_content text,
    rendered_text text,
    structured_content jsonb,
    approval_status text DEFAULT 'pending'::text NOT NULL,
    approval_comment text,
    approved_by text,
    approved_at timestamp with time zone,
    quality_score numeric(5,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT document_processing_approval_status_check CHECK ((approval_status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: document_registry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_registry (
    document_id text NOT NULL,
    project_slug text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_review_annotation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_review_annotation (
    annotation_id text NOT NULL,
    document_id text NOT NULL,
    project_slug text NOT NULL,
    target_type text DEFAULT 'document'::text NOT NULL,
    target_id text,
    section_path text,
    severity text DEFAULT 'medium'::text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    note text NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT document_review_annotation_severity_check CHECK ((severity = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text]))),
    CONSTRAINT document_review_annotation_status_check CHECK ((status = ANY (ARRAY['open'::text, 'resolved'::text]))),
    CONSTRAINT document_review_annotation_target_type_check CHECK ((target_type = ANY (ARRAY['document'::text, 'section'::text, 'chunk'::text])))
);


--
-- Name: document_section_exclusion; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_section_exclusion (
    exclusion_id text NOT NULL,
    document_id text NOT NULL,
    project_slug text NOT NULL,
    section_path text NOT NULL,
    section_title text,
    reason text,
    excluded_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_vision_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_vision_config (
    id integer NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    llm_config_id text DEFAULT ''::text NOT NULL,
    temperature numeric(4,2) DEFAULT 0.1 NOT NULL,
    max_tokens integer DEFAULT 2500 NOT NULL,
    max_file_size_mb integer DEFAULT 20 NOT NULL,
    auto_create_shard boolean DEFAULT true NOT NULL,
    system_prompt text DEFAULT ''::text NOT NULL,
    extraction_prompt text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_vision_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_vision_run (
    run_id text NOT NULL,
    project_slug text NOT NULL,
    actor text DEFAULT ''::text NOT NULL,
    filename text DEFAULT ''::text NOT NULL,
    media_type text DEFAULT ''::text NOT NULL,
    file_size integer DEFAULT 0 NOT NULL,
    file_sha256 text DEFAULT ''::text NOT NULL,
    prompt_text text DEFAULT ''::text NOT NULL,
    analysis_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    extracted_markdown text DEFAULT ''::text NOT NULL,
    shard_id text DEFAULT ''::text NOT NULL,
    audit_session_id text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'completed'::text NOT NULL,
    error_message text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_audit_exchange; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_audit_exchange (
    exchange_id text NOT NULL,
    session_id text NOT NULL,
    sequence_no integer DEFAULT 1 NOT NULL,
    request_payload jsonb NOT NULL,
    response_payload jsonb,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_audit_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_audit_session (
    session_id text NOT NULL,
    purpose text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    api_url text NOT NULL,
    status text DEFAULT 'started'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_message text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    config_id text,
    config_name text
);


--
-- Name: llm_comparator_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_comparator_result (
    result_id text NOT NULL,
    run_id text NOT NULL,
    config_id text DEFAULT ''::text NOT NULL,
    model text DEFAULT ''::text NOT NULL,
    language text DEFAULT ''::text NOT NULL,
    benchmark_id text DEFAULT ''::text NOT NULL,
    system_prompt text DEFAULT ''::text NOT NULL,
    user_prompt text DEFAULT ''::text NOT NULL,
    output_text text DEFAULT ''::text NOT NULL,
    raw_response jsonb DEFAULT '{}'::jsonb NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    audit_session_id text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'completed'::text NOT NULL,
    error_message text DEFAULT ''::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone
);


--
-- Name: llm_comparator_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_comparator_run (
    run_id text NOT NULL,
    actor text DEFAULT ''::text NOT NULL,
    mode text DEFAULT 'benchmark'::text NOT NULL,
    benchmark_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    selected_models jsonb DEFAULT '[]'::jsonb NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    error_message text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_config (
    id integer NOT NULL,
    provider text DEFAULT 'ollama'::text NOT NULL,
    api_url text NOT NULL,
    api_key text,
    model text NOT NULL,
    timeout_seconds integer DEFAULT 90 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    config_id text,
    name text,
    is_default boolean DEFAULT false NOT NULL,
    max_tokens integer DEFAULT 800 NOT NULL,
    retries integer DEFAULT 1 NOT NULL,
    json_mode boolean DEFAULT false NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    profile_type text DEFAULT 'general'::text NOT NULL
);


--
-- Name: project; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project (
    uuid text NOT NULL,
    project_name text NOT NULL,
    project_nameslug text NOT NULL,
    last_date_edit timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: quizbot_audit_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quizbot_audit_event (
    event_id text NOT NULL,
    session_id text,
    actor text DEFAULT 'system'::text NOT NULL,
    event_type text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: quizbot_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quizbot_config (
    id integer NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    llm_config_id text DEFAULT ''::text NOT NULL,
    question_llm_config_id text DEFAULT ''::text NOT NULL,
    correction_llm_config_id text DEFAULT ''::text NOT NULL,
    provider text DEFAULT 'ollama'::text NOT NULL,
    api_url text NOT NULL,
    api_key text,
    question_model text DEFAULT ''::text NOT NULL,
    correction_model text DEFAULT ''::text NOT NULL,
    temperature numeric(4,2) DEFAULT 0.2 NOT NULL,
    max_tokens integer DEFAULT 800 NOT NULL,
    timeout_seconds integer DEFAULT 90 NOT NULL,
    retry_count integer DEFAULT 1 NOT NULL,
    strict_json boolean DEFAULT true NOT NULL,
    question_system_prompt text DEFAULT ''::text NOT NULL,
    correction_system_prompt text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT quizbot_config_provider_check CHECK ((provider = ANY (ARRAY['ollama'::text, 'litellm'::text, 'openai'::text, 'lmstudio'::text, 'openai_compatible'::text, 'custom'::text])))
);


--
-- Name: quizbot_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quizbot_session (
    session_id text NOT NULL,
    topic_id text,
    topic_label text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'started'::text NOT NULL,
    question_text text DEFAULT ''::text NOT NULL,
    expected_answer text DEFAULT ''::text NOT NULL,
    user_answer text DEFAULT ''::text NOT NULL,
    correction jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_correct boolean,
    rating text,
    comment text DEFAULT ''::text NOT NULL,
    error_message text,
    question_model text DEFAULT ''::text NOT NULL,
    correction_model text DEFAULT ''::text NOT NULL,
    generation_audit_session_id text DEFAULT ''::text NOT NULL,
    correction_audit_session_id text DEFAULT ''::text NOT NULL,
    generation_duration_ms integer,
    correction_duration_ms integer,
    user_agent text DEFAULT ''::text NOT NULL,
    ip_address text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT quizbot_session_rating_check CHECK ((rating = ANY (ARRAY['good'::text, 'neutral'::text, 'bad'::text])))
);


--
-- Name: quizbot_topic; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quizbot_topic (
    topic_id text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    level text DEFAULT ''::text NOT NULL,
    instructions text DEFAULT ''::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    archived boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: shard_quality_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shard_quality_config (
    id integer NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    llm_config_id text DEFAULT ''::text NOT NULL,
    temperature numeric(4,2) DEFAULT 0.1 NOT NULL,
    max_tokens integer DEFAULT 2200 NOT NULL,
    max_input_chars integer DEFAULT 30000 NOT NULL,
    system_prompt text DEFAULT ''::text NOT NULL,
    analysis_prompt text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: shard_quality_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shard_quality_run (
    run_id text NOT NULL,
    project_slug text NOT NULL,
    shard_id text NOT NULL,
    actor text DEFAULT ''::text NOT NULL,
    title_document text DEFAULT ''::text NOT NULL,
    content_sha256 text DEFAULT ''::text NOT NULL,
    local_metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    analysis_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    overall_score integer DEFAULT 0 NOT NULL,
    audit_session_id text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'completed'::text NOT NULL,
    error_message text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: task_sequencer_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_sequencer_config (
    id integer NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    llm_config_id text DEFAULT ''::text NOT NULL,
    temperature numeric(4,2) DEFAULT 0.2 NOT NULL,
    max_tokens integer DEFAULT 1800 NOT NULL,
    axes_system_prompt text DEFAULT ''::text NOT NULL,
    plan_system_prompt text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: task_sequencer_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_sequencer_run (
    run_id text NOT NULL,
    actor text DEFAULT ''::text NOT NULL,
    context_text text NOT NULL,
    task_type text DEFAULT ''::text NOT NULL,
    sequencing_axes text DEFAULT ''::text NOT NULL,
    axes_suggestions jsonb DEFAULT '{}'::jsonb NOT NULL,
    plan_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'completed'::text NOT NULL,
    axes_audit_session_id text DEFAULT ''::text NOT NULL,
    plan_audit_session_id text DEFAULT ''::text NOT NULL,
    error_message text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: vectorization_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vectorization_config (
    config_id text NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    llm_config_id text DEFAULT ''::text NOT NULL,
    embedding_api_url text DEFAULT ''::text NOT NULL,
    embedding_model text DEFAULT ''::text NOT NULL,
    embedding_dimensions integer DEFAULT 1536 NOT NULL,
    batch_size integer DEFAULT 25 NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webchat_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webchat_config (
    id integer NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    llm_config_id text DEFAULT ''::text NOT NULL,
    system_prompt text DEFAULT ''::text NOT NULL,
    temperature numeric(4,2) DEFAULT 0.2 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webchat_message; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webchat_message (
    message_id text NOT NULL,
    session_id text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    raw_content text,
    pipeline_trace jsonb DEFAULT '[]'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT webchat_message_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'system'::text])))
);


--
-- Name: webchat_pipeline_step; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webchat_pipeline_step (
    step_id text NOT NULL,
    direction text NOT NULL,
    "position" integer DEFAULT 100 NOT NULL,
    name text NOT NULL,
    step_type text DEFAULT 'llm_transform'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    fail_closed boolean DEFAULT true NOT NULL,
    llm_config_id text DEFAULT ''::text NOT NULL,
    prompt_template text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT webchat_pipeline_step_direction_check CHECK ((direction = ANY (ARRAY['inbound'::text, 'outbound'::text]))),
    CONSTRAINT webchat_pipeline_step_step_type_check CHECK ((step_type = ANY (ARRAY['llm_transform'::text, 'llm_guard'::text])))
);


--
-- Name: webchat_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webchat_session (
    session_id text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    user_agent text,
    ip_address text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chunk_metadata chunk_metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_metadata
    ADD CONSTRAINT chunk_metadata_pkey PRIMARY KEY (chunk_id);


--
-- Name: dataset_build dataset_build_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dataset_build
    ADD CONSTRAINT dataset_build_pkey PRIMARY KEY (build_id);


--
-- Name: document_processing document_processing_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_processing
    ADD CONSTRAINT document_processing_pkey PRIMARY KEY (document_id);


--
-- Name: document_registry document_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_registry
    ADD CONSTRAINT document_registry_pkey PRIMARY KEY (document_id);


--
-- Name: document_review_annotation document_review_annotation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_review_annotation
    ADD CONSTRAINT document_review_annotation_pkey PRIMARY KEY (annotation_id);


--
-- Name: document_section_exclusion document_section_exclusion_document_id_project_slug_section_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_section_exclusion
    ADD CONSTRAINT document_section_exclusion_document_id_project_slug_section_key UNIQUE (document_id, project_slug, section_path);


--
-- Name: document_section_exclusion document_section_exclusion_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_section_exclusion
    ADD CONSTRAINT document_section_exclusion_pkey PRIMARY KEY (exclusion_id);


--
-- Name: document_vision_config document_vision_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_vision_config
    ADD CONSTRAINT document_vision_config_pkey PRIMARY KEY (id);


--
-- Name: document_vision_run document_vision_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_vision_run
    ADD CONSTRAINT document_vision_run_pkey PRIMARY KEY (run_id);


--
-- Name: llm_audit_exchange llm_audit_exchange_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_audit_exchange
    ADD CONSTRAINT llm_audit_exchange_pkey PRIMARY KEY (exchange_id);


--
-- Name: llm_audit_session llm_audit_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_audit_session
    ADD CONSTRAINT llm_audit_session_pkey PRIMARY KEY (session_id);


--
-- Name: llm_comparator_result llm_comparator_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_comparator_result
    ADD CONSTRAINT llm_comparator_result_pkey PRIMARY KEY (result_id);


--
-- Name: llm_comparator_run llm_comparator_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_comparator_run
    ADD CONSTRAINT llm_comparator_run_pkey PRIMARY KEY (run_id);


--
-- Name: llm_config llm_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_config
    ADD CONSTRAINT llm_config_pkey PRIMARY KEY (id);


--
-- Name: project project_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_pkey PRIMARY KEY (uuid);


--
-- Name: project project_project_nameslug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_project_nameslug_key UNIQUE (project_nameslug);


--
-- Name: quizbot_audit_event quizbot_audit_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quizbot_audit_event
    ADD CONSTRAINT quizbot_audit_event_pkey PRIMARY KEY (event_id);


--
-- Name: quizbot_config quizbot_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quizbot_config
    ADD CONSTRAINT quizbot_config_pkey PRIMARY KEY (id);


--
-- Name: quizbot_session quizbot_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quizbot_session
    ADD CONSTRAINT quizbot_session_pkey PRIMARY KEY (session_id);


--
-- Name: quizbot_topic quizbot_topic_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quizbot_topic
    ADD CONSTRAINT quizbot_topic_pkey PRIMARY KEY (topic_id);


--
-- Name: shard_quality_config shard_quality_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shard_quality_config
    ADD CONSTRAINT shard_quality_config_pkey PRIMARY KEY (id);


--
-- Name: shard_quality_run shard_quality_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shard_quality_run
    ADD CONSTRAINT shard_quality_run_pkey PRIMARY KEY (run_id);


--
-- Name: task_sequencer_config task_sequencer_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_sequencer_config
    ADD CONSTRAINT task_sequencer_config_pkey PRIMARY KEY (id);


--
-- Name: task_sequencer_run task_sequencer_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_sequencer_run
    ADD CONSTRAINT task_sequencer_run_pkey PRIMARY KEY (run_id);


--
-- Name: vectorization_config vectorization_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vectorization_config
    ADD CONSTRAINT vectorization_config_pkey PRIMARY KEY (config_id);


--
-- Name: webchat_config webchat_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webchat_config
    ADD CONSTRAINT webchat_config_pkey PRIMARY KEY (id);


--
-- Name: webchat_message webchat_message_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webchat_message
    ADD CONSTRAINT webchat_message_pkey PRIMARY KEY (message_id);


--
-- Name: webchat_pipeline_step webchat_pipeline_step_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webchat_pipeline_step
    ADD CONSTRAINT webchat_pipeline_step_pkey PRIMARY KEY (step_id);


--
-- Name: webchat_session webchat_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webchat_session
    ADD CONSTRAINT webchat_session_pkey PRIMARY KEY (session_id);


--
-- Name: document_vision_run_project_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_vision_run_project_created_idx ON public.document_vision_run USING btree (project_slug, created_at DESC);


--
-- Name: idx_chunk_metadata_project_shard; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunk_metadata_project_shard ON public.chunk_metadata USING btree (project_slug, shard_id);


--
-- Name: idx_chunk_metadata_quality_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunk_metadata_quality_score ON public.chunk_metadata USING btree (quality_score DESC);


--
-- Name: idx_chunk_metadata_shard_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunk_metadata_shard_id ON public.chunk_metadata USING btree (shard_id);


--
-- Name: idx_dataset_build_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dataset_build_project ON public.dataset_build USING btree (project_slug, created_at DESC);


--
-- Name: idx_document_processing_approval_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_processing_approval_status ON public.document_processing USING btree (approval_status);


--
-- Name: idx_document_processing_project_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_processing_project_slug ON public.document_processing USING btree (project_slug);


--
-- Name: idx_document_processing_quality_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_processing_quality_score ON public.document_processing USING btree (quality_score DESC);


--
-- Name: idx_document_registry_project_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_registry_project_slug ON public.document_registry USING btree (project_slug);


--
-- Name: idx_document_review_annotation_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_review_annotation_document ON public.document_review_annotation USING btree (project_slug, document_id, created_at DESC);


--
-- Name: idx_document_section_exclusion_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_section_exclusion_document ON public.document_section_exclusion USING btree (project_slug, document_id);


--
-- Name: llm_audit_exchange_session_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_audit_exchange_session_idx ON public.llm_audit_exchange USING btree (session_id, sequence_no);


--
-- Name: llm_audit_session_started_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_audit_session_started_idx ON public.llm_audit_session USING btree (started_at DESC);


--
-- Name: llm_comparator_result_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_comparator_result_run_idx ON public.llm_comparator_result USING btree (run_id, model, benchmark_id);


--
-- Name: llm_comparator_run_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_comparator_run_created_idx ON public.llm_comparator_run USING btree (created_at DESC);


--
-- Name: llm_config_config_id_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX llm_config_config_id_uq ON public.llm_config USING btree (config_id);


--
-- Name: llm_config_one_default_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX llm_config_one_default_uq ON public.llm_config USING btree (is_default) WHERE (is_default = true);


--
-- Name: quizbot_audit_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX quizbot_audit_created_idx ON public.quizbot_audit_event USING btree (created_at DESC);


--
-- Name: quizbot_session_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX quizbot_session_created_idx ON public.quizbot_session USING btree (created_at DESC);


--
-- Name: quizbot_session_topic_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX quizbot_session_topic_idx ON public.quizbot_session USING btree (topic_id, created_at DESC);


--
-- Name: shard_quality_run_project_shard_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX shard_quality_run_project_shard_created_idx ON public.shard_quality_run USING btree (project_slug, shard_id, created_at DESC);


--
-- Name: task_sequencer_run_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_sequencer_run_created_idx ON public.task_sequencer_run USING btree (created_at DESC);


--
-- Name: webchat_message_session_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX webchat_message_session_idx ON public.webchat_message USING btree (session_id, created_at);


--
-- Name: webchat_pipeline_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX webchat_pipeline_order_idx ON public.webchat_pipeline_step USING btree (direction, "position", created_at);


--
-- Name: llm_audit_exchange llm_audit_exchange_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_audit_exchange
    ADD CONSTRAINT llm_audit_exchange_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.llm_audit_session(session_id) ON DELETE CASCADE;


--
-- Name: llm_comparator_result llm_comparator_result_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_comparator_result
    ADD CONSTRAINT llm_comparator_result_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.llm_comparator_run(run_id) ON DELETE CASCADE;


--
-- Name: quizbot_session quizbot_session_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quizbot_session
    ADD CONSTRAINT quizbot_session_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.quizbot_topic(topic_id) ON DELETE SET NULL;


--
-- Name: webchat_message webchat_message_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webchat_message
    ADD CONSTRAINT webchat_message_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.webchat_session(session_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


