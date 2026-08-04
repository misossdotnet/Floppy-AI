ALTER TABLE public.document_processing
ADD COLUMN raw_content text;

ALTER TABLE public.document_processing
ADD COLUMN normalization_config jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.document_processing
ADD COLUMN detected_language text NOT NULL DEFAULT 'und';

ALTER TABLE public.document_processing
ADD COLUMN content_type text NOT NULL DEFAULT 'unknown';

ALTER TABLE public.document_processing
ADD COLUMN extracted_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX document_processing_language_idx
ON public.document_processing(detected_language);

CREATE INDEX document_processing_content_type_idx
ON public.document_processing(content_type);
