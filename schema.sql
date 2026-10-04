CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- Parent tender (from before, plus child counters) ----------
CREATE TABLE tender (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  accession_no     text NOT NULL UNIQUE,
  source           char(3) NOT NULL,
  source_ref       text,
  dedupe_hash      text NOT NULL,
  title            text NOT NULL,
  buyer            text,
  deadline_at      timestamptz,
  status           text NOT NULL DEFAULT 'open',
  merged_into      uuid REFERENCES tender(id),
  captured_at      timestamptz NOT NULL DEFAULT now(),
  last_lot_no        smallint NOT NULL DEFAULT 0,
  last_amendment_no  smallint NOT NULL DEFAULT 0,
  last_submission_no smallint NOT NULL DEFAULT 0,
  CONSTRAINT accession_format
    CHECK (accession_no ~ '^TRC-\d{2}-[A-Z]{3}-\d{6}-[0-9A-Z*]$'),
  CONSTRAINT source_ref_unique UNIQUE (source, source_ref)
);
CREATE INDEX tender_dedupe_idx ON tender (dedupe_hash);

CREATE TABLE accession_counter (
  yy smallint NOT NULL, source char(3) NOT NULL, last_no integer NOT NULL DEFAULT 0,
  PRIMARY KEY (yy, source)
);

CREATE TABLE accession_alias (
  old_accession_no text PRIMARY KEY,
  tender_id uuid NOT NULL REFERENCES tender(id)
);

CREATE OR REPLACE FUNCTION mod37_2_check(input text)
RETURNS char(1) LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  alphabet constant text := '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*';
  p int := 0;
  ch text;
BEGIN
  FOREACH ch IN ARRAY regexp_split_to_array(upper(regexp_replace(input, '[^0-9A-Za-z]', '', 'g')), '')
  LOOP
    p := ((p + strpos(alphabet, ch) - 1) * 2) % 37;
  END LOOP;
  RETURN substr(alphabet, ((38 - p) % 37) + 1, 1);
END $$;

CREATE OR REPLACE FUNCTION issue_accession(p_source char(3), p_captured timestamptz DEFAULT now())
RETURNS text LANGUAGE plpgsql AS $$
DECLARE
  v_yy smallint := (extract(year FROM p_captured)::int % 100);
  v_no integer;
  v_body text;
BEGIN
  INSERT INTO accession_counter (yy, source, last_no) VALUES (v_yy, p_source, 1)
  ON CONFLICT (yy, source) DO UPDATE SET last_no = accession_counter.last_no + 1
  RETURNING last_no INTO v_no;
  IF v_no > 999999 THEN
    RAISE EXCEPTION 'Accession range exhausted for % %', v_yy, p_source;
  END IF;
  v_body := format('TRC-%s-%s-%s', lpad(v_yy::text, 2, '0'), p_source, lpad(v_no::text, 6, '0'));
  RETURN v_body || '-' || mod37_2_check(v_body);
END $$;

-- ---------- Child numbering ----------
-- Hands out the next L/A/S number for one tender. The UPDATE locks that
-- tender row until commit, so concurrent inserts never collide.
CREATE OR REPLACE FUNCTION issue_child_no(p_tender_id uuid, p_kind char(1))
RETURNS TABLE (child_no smallint, child_accession text) LANGUAGE plpgsql AS $$
DECLARE
  v_parent text;
BEGIN
  IF p_kind = 'L' THEN
    UPDATE tender SET last_lot_no = last_lot_no + 1 WHERE id = p_tender_id
    RETURNING last_lot_no, accession_no INTO child_no, v_parent;
  ELSIF p_kind = 'A' THEN
    UPDATE tender SET last_amendment_no = last_amendment_no + 1 WHERE id = p_tender_id
    RETURNING last_amendment_no, accession_no INTO child_no, v_parent;
  ELSIF p_kind = 'S' THEN
    UPDATE tender SET last_submission_no = last_submission_no + 1 WHERE id = p_tender_id
    RETURNING last_submission_no, accession_no INTO child_no, v_parent;
  ELSE
    RAISE EXCEPTION 'Unknown child kind %, expected L, A or S', p_kind;
  END IF;
  IF v_parent IS NULL THEN
    RAISE EXCEPTION 'Tender % not found', p_tender_id;
  END IF;
  child_accession := v_parent || '/' || p_kind || lpad(child_no::text, 2, '0');
  RETURN NEXT;
END $$;

-- ---------- Lots ----------
CREATE TABLE tender_lot (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tender_id        uuid NOT NULL REFERENCES tender(id) ON DELETE RESTRICT,
  lot_no           smallint NOT NULL,
  accession_no     text NOT NULL UNIQUE,
  source_lot_ref   text,                 -- the platform's own lot number, e.g. "Lote 3"
  title            text NOT NULL,
  description      text,
  cpv_codes        text[],
  estimated_value  numeric(18,2),
  currency         char(3),
  status           text NOT NULL DEFAULT 'open',
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tender_id, lot_no),
  UNIQUE (tender_id, id),               -- lets children check lot and tender match
  UNIQUE (tender_id, source_lot_ref),
  CONSTRAINT lot_accession_format
    CHECK (accession_no ~ '^TRC-\d{2}-[A-Z]{3}-\d{6}-[0-9A-Z*]/L\d{2,3}$')
);

-- ---------- Amendments / clarifications ----------
CREATE TABLE tender_amendment (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tender_id           uuid NOT NULL REFERENCES tender(id) ON DELETE RESTRICT,
  lot_id              uuid,             -- NULL means it applies to the whole tender
  amendment_no        smallint NOT NULL,
  accession_no        text NOT NULL UNIQUE,
  kind                text NOT NULL CHECK (kind IN ('amendment','clarification','deadline_change','cancellation','corrigendum')),
  source_ref          text,
  summary             text,
  published_at        timestamptz,
  new_deadline_at     timestamptz,
  document_uri        text,
  document_sha256     text,             -- spots re-scraped duplicates of the same file
  captured_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tender_id, amendment_no),
  UNIQUE (tender_id, document_sha256),
  FOREIGN KEY (tender_id, lot_id) REFERENCES tender_lot (tender_id, id),
  CONSTRAINT amendment_accession_format
    CHECK (accession_no ~ '^TRC-\d{2}-[A-Z]{3}-\d{6}-[0-9A-Z*]/A\d{2,3}$')
);

-- ---------- Your submissions ----------
CREATE TABLE tender_submission (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tender_id           uuid NOT NULL REFERENCES tender(id) ON DELETE RESTRICT,
  lot_id              uuid,             -- NULL means a bid for the whole tender
  submission_no       smallint NOT NULL,
  accession_no        text NOT NULL UNIQUE,
  opco                text,
  owner               text,
  status              text NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft','in_review','approved','submitted','withdrawn','won','lost','cancelled')),
  supersedes_id       uuid REFERENCES tender_submission(id),   -- a resubmission points at the one it replaces
  portal_receipt_ref  text,
  submitted_at        timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tender_id, submission_no),
  FOREIGN KEY (tender_id, lot_id) REFERENCES tender_lot (tender_id, id),
  CONSTRAINT submission_accession_format
    CHECK (accession_no ~ '^TRC-\d{2}-[A-Z]{3}-\d{6}-[0-9A-Z*]/S\d{2,3}$'),
  CONSTRAINT submitted_needs_time
    CHECK (status NOT IN ('submitted','won','lost') OR submitted_at IS NOT NULL)
);
CREATE INDEX tender_submission_lot_idx ON tender_submission (lot_id);
CREATE INDEX tender_amendment_lot_idx  ON tender_amendment (lot_id);
