-- 002_ingestion.sql  --  Tender & RFP Cockpit ingestion v2
-- Apply AFTER schema.sql, dedupe.sql, review.sql. Safe to re-run.
--
-- What it changes:
--   * pg_trgm for the background candidate job (not used by any trigger or by find_or_create_tender)
--   * tender.title_norm / buyer_norm (filled by Python, dedupe.py is the single source of truth)
--     and tender.created_at (row-insert time, used by the candidates job watermark)
--   * thin BEFORE INSERT trigger that only fills accession_no
--   * append-only tender_event audit log
--   * job_state (watermarks for background jobs)
--   * find_or_create_tender: no inline similarity flagging, relies on the trigger, writes events
--   * merge_tender / mark_not_duplicate: take an actor and write events
\set ON_ERROR_STOP 1
BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- New tender columns ----------
ALTER TABLE tender ADD COLUMN IF NOT EXISTS title_norm text;
ALTER TABLE tender ADD COLUMN IF NOT EXISTS buyer_norm text;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'tender' AND column_name = 'created_at') THEN
    ALTER TABLE tender ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
    UPDATE tender SET created_at = captured_at;      -- existing rows: best available insert time
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS tender_created_idx ON tender (created_at, id);
CREATE INDEX IF NOT EXISTS tender_buyer_deadline_idx
  ON tender (buyer_norm, deadline_at) WHERE merged_into IS NULL;

-- ---------- 1. Thin numbering trigger (no dedupe here, ever) ----------
CREATE OR REPLACE FUNCTION tender_fill_accession()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.accession_no IS NULL THEN
    NEW.accession_no := issue_accession(NEW.source, NEW.captured_at);
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS tender_fill_accession ON tender;
CREATE TRIGGER tender_fill_accession
  BEFORE INSERT ON tender
  FOR EACH ROW WHEN (NEW.accession_no IS NULL)
  EXECUTE FUNCTION tender_fill_accession();

-- ---------- 3. Append-only event log ----------
CREATE TABLE IF NOT EXISTS tender_event (
  id           bigserial PRIMARY KEY,
  tender_id    uuid NOT NULL REFERENCES tender(id) ON DELETE RESTRICT,
  event_type   text NOT NULL CHECK (event_type IN (
                 'created','matched_source_ref','matched_hash','merged','marked_different',
                 'amended','lot_added','submission_added','status_changed')),
  actor        text NOT NULL,                  -- scraper name, 'review:<user>', 'candidates-job', ...
  source       char(3),                        -- platform the event came from, if any
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS tender_event_tender_idx ON tender_event (tender_id, id);

CREATE OR REPLACE FUNCTION tender_event_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'tender_event is append-only: % is not allowed', TG_OP
    USING ERRCODE = 'insufficient_privilege',
          HINT = 'Record a correcting event instead of changing history.';
END $$;

-- Statement-level so it also fires when the WHERE clause matches nothing
DROP TRIGGER IF EXISTS tender_event_no_update ON tender_event;
CREATE TRIGGER tender_event_no_update BEFORE UPDATE OR DELETE ON tender_event
  FOR EACH STATEMENT EXECUTE FUNCTION tender_event_append_only();
DROP TRIGGER IF EXISTS tender_event_no_truncate ON tender_event;
CREATE TRIGGER tender_event_no_truncate BEFORE TRUNCATE ON tender_event
  FOR EACH STATEMENT EXECUTE FUNCTION tender_event_append_only();
REVOKE UPDATE, DELETE, TRUNCATE ON tender_event FROM PUBLIC;

-- ---------- Background job state ----------
CREATE TABLE IF NOT EXISTS job_state (
  job_name    text PRIMARY KEY,
  watermark   timestamptz,                    -- NULL = never run
  last_run    jsonb,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------- 2. find_or_create_tender: explicit call, no similarity work ----------
DROP FUNCTION IF EXISTS find_or_create_tender(char, text, text, text, text, timestamptz);

CREATE OR REPLACE FUNCTION find_or_create_tender(
  p_source      char(3),
  p_source_ref  text,          -- already cleaned by dedupe.clean_source_ref
  p_hash        text,          -- dedupe.dedupe_hash (Python is the only place it is computed)
  p_title       text,
  p_buyer       text,
  p_deadline    timestamptz,
  p_title_norm  text,          -- dedupe.clean_title
  p_buyer_norm  text,          -- dedupe.clean_buyer
  p_actor       text,
  p_captured_at timestamptz DEFAULT NULL,
  p_payload     jsonb DEFAULT '{}'::jsonb)
RETURNS TABLE (tender_id uuid, accession_no text, outcome text)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
  v_id      uuid;
  v_k_hash  bigint;
  v_k_ref   bigint;
  v_payload jsonb := coalesce(p_payload, '{}'::jsonb)
                     || jsonb_build_object('source_ref', p_source_ref, 'dedupe_hash', p_hash);
BEGIN
  IF p_source IS NULL OR p_hash IS NULL OR p_title IS NULL OR p_actor IS NULL THEN
    RAISE EXCEPTION 'find_or_create_tender: source, hash, title and actor are required';
  END IF;

  -- Serialise work on the same reference AND on the same hash. Two locks, always taken
  -- in the same order, so a PLC scrape and a TED copy of it cannot both insert.
  v_k_hash := hashtextextended('trc:hash:' || p_hash, 0);
  IF p_source_ref IS NOT NULL THEN
    v_k_ref := hashtextextended('trc:ref:' || p_source || ':' || p_source_ref, 0);
    PERFORM pg_advisory_xact_lock(least(v_k_hash, v_k_ref));
    PERFORM pg_advisory_xact_lock(greatest(v_k_hash, v_k_ref));
  ELSE
    PERFORM pg_advisory_xact_lock(v_k_hash);
  END IF;

  -- 1. Same platform reference: always the same tender (follow a merge to the survivor)
  IF p_source_ref IS NOT NULL THEN
    SELECT coalesce(t.merged_into, t.id) INTO v_id
    FROM tender t WHERE t.source = p_source AND t.source_ref = p_source_ref;
    IF FOUND THEN
      INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
      VALUES (v_id, 'matched_source_ref', p_actor, p_source, v_payload);
      RETURN QUERY SELECT t.id, t.accession_no, 'matched_source_ref'::text FROM tender t WHERE t.id = v_id;
      RETURN;
    END IF;
  END IF;

  -- 2. Same cleaned title + buyer + deadline day: a re-post or a copy on another platform.
  --    Prefer a live tender; a hash that only exists on a merged duplicate goes to its survivor
  --    (otherwise re-scraping a merged duplicate would create it all over again).
  SELECT coalesce(t.merged_into, t.id) INTO v_id FROM tender t
  WHERE t.dedupe_hash = p_hash AND t.status <> 'withdrawn'
  ORDER BY (t.merged_into IS NULL) DESC, t.created_at
  LIMIT 1;
  IF FOUND THEN
    INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
    VALUES (v_id, 'matched_hash', p_actor, p_source, v_payload);
    RETURN QUERY SELECT t.id, t.accession_no, 'matched_hash'::text FROM tender t WHERE t.id = v_id;
    RETURN;
  END IF;

  -- 3. New tender. accession_no is left out on purpose: the trigger numbers it.
  INSERT INTO tender (source, source_ref, dedupe_hash, title, buyer, deadline_at,
                      title_norm, buyer_norm, captured_at)
  VALUES (p_source, p_source_ref, p_hash, p_title, p_buyer, p_deadline,
          p_title_norm, p_buyer_norm, coalesce(p_captured_at, now()))
  RETURNING tender.id INTO v_id;

  INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
  VALUES (v_id, 'created', p_actor, p_source, v_payload);

  RETURN QUERY SELECT t.id, t.accession_no, 'created'::text FROM tender t WHERE t.id = v_id;
END $$;

-- ---------- merge_tender with actor + events ----------
DROP FUNCTION IF EXISTS merge_tender(uuid, uuid);

CREATE OR REPLACE FUNCTION merge_tender(p_duplicate uuid, p_survivor uuid, p_actor text DEFAULT 'system')
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_dup  tender%ROWTYPE;
  v_surv tender%ROWTYPE;
BEGIN
  IF p_duplicate = p_survivor THEN RAISE EXCEPTION 'Cannot merge a tender into itself'; END IF;

  -- Lock both rows in a fixed order so two reviewers cannot cross-merge
  PERFORM 1 FROM tender WHERE id IN (p_duplicate, p_survivor) ORDER BY id FOR UPDATE;
  SELECT * INTO v_dup  FROM tender WHERE id = p_duplicate;
  SELECT * INTO v_surv FROM tender WHERE id = p_survivor;
  IF v_dup.id IS NULL OR v_surv.id IS NULL THEN RAISE EXCEPTION 'Tender not found'; END IF;
  IF v_surv.merged_into IS NOT NULL THEN
    RAISE EXCEPTION 'Survivor % is itself merged into %', v_surv.accession_no, v_surv.merged_into;
  END IF;
  IF v_dup.merged_into IS NOT NULL THEN
    RAISE EXCEPTION 'Tender % is already merged into %', v_dup.accession_no, v_dup.merged_into;
  END IF;

  UPDATE tender SET merged_into = p_survivor, status = 'merged' WHERE id = p_duplicate;
  -- Keep merges one level deep so a source_ref lookup always lands on a live tender
  UPDATE tender SET merged_into = p_survivor WHERE merged_into = p_duplicate;

  INSERT INTO accession_alias (old_accession_no, tender_id)
  VALUES (v_dup.accession_no, p_survivor)
  ON CONFLICT (old_accession_no) DO UPDATE SET tender_id = EXCLUDED.tender_id;
  UPDATE accession_alias SET tender_id = p_survivor WHERE tender_id = p_duplicate;
  UPDATE tender_possible_duplicate SET resolution = 'same'
  WHERE (tender_id, candidate_id) IN ((p_duplicate, p_survivor), (p_survivor, p_duplicate));

  INSERT INTO tender_event (tender_id, event_type, actor, payload) VALUES
    (p_duplicate, 'merged', p_actor, jsonb_build_object(
       'role', 'duplicate', 'into', p_survivor, 'into_accession', v_surv.accession_no,
       'previous_status', v_dup.status)),
    (p_survivor,  'merged', p_actor, jsonb_build_object(
       'role', 'survivor', 'absorbed', p_duplicate, 'absorbed_accession', v_dup.accession_no));
END $$;

-- ---------- mark_not_duplicate with actor + events ----------
DROP FUNCTION IF EXISTS mark_not_duplicate(uuid, uuid);

CREATE OR REPLACE FUNCTION mark_not_duplicate(p_tender uuid, p_candidate uuid, p_actor text DEFAULT 'system')
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_n int;
BEGIN
  IF p_tender = p_candidate THEN RAISE EXCEPTION 'A tender cannot be a duplicate of itself'; END IF;
  UPDATE tender_possible_duplicate SET resolution = 'different'
  WHERE (tender_id, candidate_id) IN ((p_tender, p_candidate), (p_candidate, p_tender));
  GET DIAGNOSTICS v_n = ROW_COUNT;
  IF v_n = 0 THEN
    -- Not flagged yet: record the decision so the candidates job never flags this pair
    INSERT INTO tender_possible_duplicate (tender_id, candidate_id, reason, resolution)
    VALUES (p_tender, p_candidate, 'manual review', 'different');
  END IF;

  INSERT INTO tender_event (tender_id, event_type, actor, payload) VALUES
    (p_tender,    'marked_different', p_actor, jsonb_build_object('other', p_candidate)),
    (p_candidate, 'marked_different', p_actor, jsonb_build_object('other', p_tender));
END $$;

COMMIT;
