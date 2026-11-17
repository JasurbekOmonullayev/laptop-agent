CREATE TABLE IF NOT EXISTS document_chunks (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_title text NOT NULL,
    section_title  text,
    clause_no      text NOT NULL,
    part_no        integer NOT NULL DEFAULT 1 CHECK (part_no > 0),
    page_from      integer NOT NULL CHECK (page_from > 0),
    page_to        integer NOT NULL CHECK (page_to >= page_from),
    content        text NOT NULL CHECK (length(trim(content)) > 0),
    content_tsv    tsvector GENERATED ALWAYS AS (to_tsvector('russian', content)) STORED,
    embedding      vector(1024) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT document_chunks_clause_part_key UNIQUE (document_title, clause_no, part_no)
);

CREATE INDEX IF NOT EXISTS document_chunks_tsv_idx
    ON document_chunks USING gin (content_tsv);
    