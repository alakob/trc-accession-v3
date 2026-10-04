-- 004_change_tracking.sql  --  Tender & RFP Cockpit: track what changes when a tender is re-scraped
-- Apply AFTER 003_source_refs.sql. Safe to re-run. Always run 002 → 003 → 004 together (apply.sh does).
--
-- What it changes:
--   * tender: description, estimated_value, currency, cpv_codes (nullable), locked_fields (fields a
--     person edited or locked; scrapers never overwrite them), updated_at, scan_at (bumped when
--     title/buyer/deadline change so the candidates job re-checks the tender)
--   * tender_hash_history: every dedupe hash a tender used to have, so a platform still showing an
--     old title/deadline matches by hash. Backfilled from deadline_changed events.
--   * tender_source_ref.origin (primary | hash_match | direct | merge) and .observed (last values
--     that platform reference showed)
--   * event types fields_changed, field_locked, field_unlocked
--   * find_or_create_tender v4: also matches old hashes; no event for a plain re-scrape of a known
--     reference (the Python service logs fields_changed only when something changed)
--   * merge_tender v4: moved references get origin 'merge'
-- The diff / precedence / hash rules themselves live in ingest.py.
\set ON_ERROR_STOP 1
BEGIN;

-- ---------- Event types ----------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tender_event_event_type_check'
                 AND pg_get_constraintdef(oid) LIKE '%field_unlocked%') THEN
    ALTER TABLE tender_event DROP CONSTRAINT IF EXISTS tender_event_event_type_check;
    ALTER TABLE tender_event ADD CONSTRAINT tender_event_event_type_check CHECK (event_type IN (
      'created','matched_source_ref','matched_hash','merged','marked_different',
      'amended','lot_added','submission_added','status_changed','deadline_changed',
      'fields_changed','field_locked','field_unlocked'));
  END IF;
END $$;

-- ---------- Tracked fields and manual locks ----------
ALTER TABLE tender ADD COLUMN IF NOT EXISTS description     text;
ALTER TABLE tender ADD COLUMN IF NOT EXISTS estimated_value numeric(18,2);
ALTER TABLE tender ADD COLUMN IF NOT EXISTS currency        char(3);
ALTER TABLE tender ADD COLUMN IF NOT EXISTS cpv_codes       text[];
ALTER TABLE tender ADD COLUMN IF NOT EXISTS locked_fields   text[] NOT NULL DEFAULT '{}';
ALTER TABLE tender ADD COLUMN IF NOT EXISTS updated_at      timestamptz;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'tender' AND column_name = 'scan_at') THEN
    ALTER TABLE tender ADD COLUMN scan_at timestamptz NOT NULL DEFAULT now();
    UPDATE tender SET scan_at = created_at;
  END IF;
END $$;
ALTER TABLE tender DROP CONSTRAINT IF EXISTS tender_locked_fields_known;
ALTER TABLE tender ADD CONSTRAINT tender_locked_fields_known CHECK (locked_fields <@ ARRAY[
  'title','buyer','deadline_at','status','description','estimated_value','currency','cpv_codes']::text[]);
CREATE INDEX IF NOT EXISTS tender_scan_idx ON tender (scan_at, id);

-- ---------- Previous hashes ----------
CREATE TABLE IF NOT EXISTS tender_hash_history (
  dedupe_hash  text NOT NULL,
  tender_id    uuid NOT NULL REFERENCES tender(id) ON DELETE RESTRICT,
  retired_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (dedupe_hash, tender_id)
);
INSERT INTO tender_hash_history (dedupe_hash, tender_id, retired_at)
SELECT e.payload->>'old_hash', e.tender_id, max(e.occurred_at)
FROM tender_event e
WHERE e.event_type = 'deadline_changed' AND (e.payload->>'hash_updated')::boolean
  AND e.payload->>'old_hash' IS NOT NULL
GROUP BY 1, 2
ON CONFLICT (dedupe_hash, tender_id) DO NOTHING;

-- ---------- Where a reference came from, and what it last showed ----------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'tender_source_ref' AND column_name = 'origin') THEN
    ALTER TABLE tender_source_ref ADD COLUMN origin text NOT NULL DEFAULT 'hash_match'
      CHECK (origin IN ('primary','hash_match','direct','merge'));
    UPDATE tender_source_ref SET origin = 'primary' WHERE is_primary;
    -- a merged tender's own reference, or one listed in a merge event, came over in a merge
    UPDATE tender_source_ref r SET origin = 'merge'
    WHERE NOT r.is_primary AND (
      EXISTS (SELECT 1 FROM tender t WHERE t.source = r.source AND t.source_ref = r.source_ref AND t.id <> r.tender_id)
      OR EXISTS (SELECT 1 FROM tender_event e, jsonb_array_elements(e.payload->'refs_moved') m
                 WHERE e.event_type = 'merged' AND e.payload->>'role' = 'duplicate'
                   AND m->>'source' = r.source AND m->>'source_ref' = r.source_ref));
  END IF;
END $$;
-- rows 003's backfill may add on a re-run get the column default; fix primaries every time
UPDATE tender_source_ref SET origin = 'primary' WHERE is_primary AND origin <> 'primary';
ALTER TABLE tender_source_ref ADD COLUMN IF NOT EXISTS observed jsonb;

-- ---------- Row-lock a tender (following a merge) before touching anything that references it ----------
-- Every writer locks the tender row FOR UPDATE *before* inserting rows that reference it (events,
-- refs, children). Otherwise one transaction can hold a foreign-key share lock while waiting for
-- the row, while the row holder waits for that share lock to change dedupe_hash (a key column,
-- because of the live-hash unique index): a deadlock.
CREATE OR REPLACE FUNCTION trc_lock_tender(p_id uuid)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
  v_id uuid := p_id;
  v_next uuid;
BEGIN
  LOOP
    SELECT merged_into INTO v_next FROM tender WHERE id = v_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Tender % not found', v_id; END IF;
    EXIT WHEN v_next IS NULL;
    v_id := v_next;          -- merged meanwhile: lock the survivor instead
  END LOOP;
  RETURN v_id;
END $$;

-- ---------- find_or_create_tender v4 ----------
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
  v_ref_tid uuid;
  v_via     text;
  v_k_hash  bigint;
  v_k_ref   bigint;
  v_payload jsonb := coalesce(p_payload, '{}'::jsonb)
                     || jsonb_build_object('source_ref', p_source_ref, 'dedupe_hash', p_hash);
BEGIN
  IF p_source IS NULL OR p_hash IS NULL OR p_title IS NULL OR p_actor IS NULL THEN
    RAISE EXCEPTION 'find_or_create_tender: source, hash, title and actor are required';
  END IF;

  -- Serialise on the reference AND the hash, always in key order (merge_tender uses the same keys).
  -- The locks are held to the end of the caller's transaction, which is where ingest.py applies
  -- field changes.
  v_k_hash := trc_lock_key('hash', p_hash);
  IF p_source_ref IS NOT NULL THEN
    v_k_ref := trc_lock_key('ref', p_source || ':' || p_source_ref);
    PERFORM pg_advisory_xact_lock(least(v_k_hash, v_k_ref));
    PERFORM pg_advisory_xact_lock(greatest(v_k_hash, v_k_ref));
  ELSE
    PERFORM pg_advisory_xact_lock(v_k_hash);
  END IF;

  IF p_source_ref IS NOT NULL THEN
    -- 1a. A reference we know. A plain re-scrape writes no event here; ingest.py logs
    --     fields_changed if anything differs. Only re-pointing after a merge is logged.
    SELECT r.tender_id, coalesce(t.merged_into, t.id) INTO v_ref_tid, v_id
    FROM tender_source_ref r JOIN tender t ON t.id = r.tender_id
    WHERE r.source = p_source AND r.source_ref = p_source_ref;
    IF FOUND THEN
      v_id := trc_lock_tender(v_id);
      UPDATE tender_source_ref r
         SET last_seen_at = now(),
             tender_id    = v_id,
             is_primary   = r.is_primary AND v_ref_tid = v_id,
             origin       = CASE WHEN v_ref_tid = v_id THEN r.origin ELSE 'merge' END
       WHERE r.source = p_source AND r.source_ref = p_source_ref;
      IF v_ref_tid <> v_id THEN
        INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
        VALUES (v_id, 'matched_source_ref', p_actor, p_source, v_payload || '{"via":"repointed_after_merge"}');
      END IF;
      RETURN QUERY SELECT t.id, t.accession_no, 'matched_source_ref'::text FROM tender t WHERE t.id = v_id;
      RETURN;
    END IF;

    -- 1b. A row inserted straight into tender has no tender_source_ref row yet
    SELECT t.id, coalesce(t.merged_into, t.id) INTO v_ref_tid, v_id
    FROM tender t WHERE t.source = p_source AND t.source_ref = p_source_ref;
    IF FOUND THEN
      v_id := trc_lock_tender(v_id);
      INSERT INTO tender_source_ref (tender_id, source, source_ref, is_primary, origin)
      VALUES (v_id, p_source, p_source_ref,
              v_ref_tid = v_id AND NOT EXISTS (SELECT 1 FROM tender_source_ref x WHERE x.tender_id = v_id AND x.is_primary),
              CASE WHEN v_ref_tid = v_id THEN 'direct' ELSE 'merge' END);
      INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
      VALUES (v_id, 'matched_source_ref', p_actor, p_source, v_payload || '{"via":"tender"}');
      RETURN QUERY SELECT t.id, t.accession_no, 'matched_source_ref'::text FROM tender t WHERE t.id = v_id;
      RETURN;
    END IF;
  END IF;

  -- 2. Same cleaned title + buyer + deadline day: current hash of a live tender, then of a merged
  --    one (→ survivor), then any hash a tender used to have (→ that tender or its survivor).
  SELECT coalesce(t.merged_into, t.id), CASE WHEN t.merged_into IS NULL THEN 'current' ELSE 'merged' END
  INTO v_id, v_via FROM tender t
  WHERE t.dedupe_hash = p_hash AND t.status <> 'withdrawn'
  ORDER BY (t.merged_into IS NULL) DESC, t.created_at
  LIMIT 1;
  IF NOT FOUND THEN
    SELECT coalesce(t.merged_into, t.id), 'history' INTO v_id, v_via
    FROM tender_hash_history h JOIN tender t ON t.id = h.tender_id
    WHERE h.dedupe_hash = p_hash AND t.status <> 'withdrawn'
    ORDER BY h.retired_at DESC
    LIMIT 1;
  END IF;
  IF v_id IS NOT NULL THEN
    v_id := trc_lock_tender(v_id);
    IF p_source_ref IS NOT NULL THEN   -- remember this platform's reference for next time
      INSERT INTO tender_source_ref (tender_id, source, source_ref, is_primary, origin)
      VALUES (v_id, p_source, p_source_ref, false, 'hash_match')
      ON CONFLICT (source, source_ref) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at;
    END IF;
    INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
    VALUES (v_id, 'matched_hash', p_actor, p_source,
            v_payload || jsonb_build_object('ref_recorded', p_source_ref IS NOT NULL, 'via', v_via));
    RETURN QUERY SELECT t.id, t.accession_no, 'matched_hash'::text FROM tender t WHERE t.id = v_id;
    RETURN;
  END IF;

  -- 3. New tender. accession_no is left out on purpose: the trigger numbers it.
  INSERT INTO tender (source, source_ref, dedupe_hash, title, buyer, deadline_at,
                      title_norm, buyer_norm, captured_at)
  VALUES (p_source, p_source_ref, p_hash, p_title, p_buyer, p_deadline,
          p_title_norm, p_buyer_norm, coalesce(p_captured_at, now()))
  RETURNING tender.id INTO v_id;
  IF p_source_ref IS NOT NULL THEN
    INSERT INTO tender_source_ref (tender_id, source, source_ref, is_primary, origin)
    VALUES (v_id, p_source, p_source_ref, true, 'primary');
  END IF;

  INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
  VALUES (v_id, 'created', p_actor, p_source, v_payload);

  RETURN QUERY SELECT t.id, t.accession_no, 'created'::text FROM tender t WHERE t.id = v_id;
END $$;

-- ---------- merge_tender v4 (as v3; moved references get origin 'merge') ----------
CREATE OR REPLACE FUNCTION merge_tender(p_duplicate uuid, p_survivor uuid, p_actor text DEFAULT 'system')
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_dup      tender%ROWTYPE;
  v_surv     tender%ROWTYPE;
  v_key      bigint;
  v_refs     jsonb;
  v_children jsonb;
BEGIN
  IF p_duplicate = p_survivor THEN RAISE EXCEPTION 'Cannot merge a tender into itself'; END IF;

  -- Same advisory keys find_or_create_tender uses, in key order, before any row lock
  FOR v_key IN
    SELECT DISTINCT k FROM (
      SELECT trc_lock_key('hash', t.dedupe_hash) FROM tender t WHERE t.id IN (p_duplicate, p_survivor)
      UNION ALL
      SELECT trc_lock_key('ref', t.source || ':' || t.source_ref) FROM tender t
       WHERE t.id IN (p_duplicate, p_survivor) AND t.source_ref IS NOT NULL
      UNION ALL
      SELECT trc_lock_key('ref', r.source || ':' || r.source_ref) FROM tender_source_ref r
       WHERE r.tender_id IN (p_duplicate, p_survivor)) s(k)
    ORDER BY k
  LOOP
    PERFORM pg_advisory_xact_lock(v_key);
  END LOOP;

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

  UPDATE tender SET merged_into = p_survivor, status = 'merged', updated_at = now() WHERE id = p_duplicate;
  UPDATE tender SET merged_into = p_survivor WHERE merged_into = p_duplicate;

  INSERT INTO accession_alias (old_accession_no, tender_id)
  VALUES (v_dup.accession_no, p_survivor)
  ON CONFLICT (old_accession_no) DO UPDATE SET tender_id = EXCLUDED.tender_id;
  UPDATE accession_alias SET tender_id = p_survivor WHERE tender_id = p_duplicate;
  UPDATE tender_possible_duplicate SET resolution = 'same'
  WHERE (tender_id, candidate_id) IN ((p_duplicate, p_survivor), (p_survivor, p_duplicate));

  WITH moved AS (
    UPDATE tender_source_ref SET tender_id = p_survivor, is_primary = false, origin = 'merge'
    WHERE tender_id = p_duplicate
    RETURNING source, source_ref)
  SELECT coalesce(jsonb_agg(jsonb_build_object('source', source, 'source_ref', source_ref)), '[]')
  INTO v_refs FROM moved;

  v_children := merge_move_children(p_duplicate, p_survivor);

  INSERT INTO tender_event (tender_id, event_type, actor, payload) VALUES
    (p_duplicate, 'merged', p_actor, jsonb_build_object(
       'role', 'duplicate', 'into', p_survivor, 'into_accession', v_surv.accession_no,
       'previous_status', v_dup.status, 'refs_moved', v_refs, 'children_moved', v_children)),
    (p_survivor,  'merged', p_actor, jsonb_build_object(
       'role', 'survivor', 'absorbed', p_duplicate, 'absorbed_accession', v_dup.accession_no,
       'refs_moved', v_refs, 'children_moved', v_children));
END $$;

COMMIT;
