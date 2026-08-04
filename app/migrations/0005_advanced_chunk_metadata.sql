ALTER TABLE public.chunk_metadata
ADD COLUMN IF NOT EXISTS summary_short text NOT NULL DEFAULT '';

ALTER TABLE public.chunk_metadata
ADD COLUMN IF NOT EXISTS document_position_ratio numeric(6,5) NOT NULL DEFAULT 0;

ALTER TABLE public.chunk_metadata
ADD COLUMN IF NOT EXISTS zone_type text NOT NULL DEFAULT 'text';

ALTER TABLE public.chunk_metadata
ADD COLUMN IF NOT EXISTS strict_zone boolean NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chunk_metadata_position_ratio_check'
          AND conrelid = 'public.chunk_metadata'::regclass
    ) THEN
        ALTER TABLE public.chunk_metadata
        ADD CONSTRAINT chunk_metadata_position_ratio_check
        CHECK (document_position_ratio >= 0 AND document_position_ratio <= 1);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS chunk_metadata_document_position_idx
ON public.chunk_metadata(document_id, document_position_ratio);
