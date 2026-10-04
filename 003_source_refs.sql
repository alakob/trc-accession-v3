-- 003_source_refs.sql  --  Tender & RFP Cockpit: every platform reference a tender is known by
-- Apply AFTER 002_ingestion.sql. Safe to re-run.
--
-- What it changes:
--   * tender_source_ref: all (source, source_ref) pairs that point at a tender, not just the first one.
--     Backfilled from tender.source/source_ref and from matched_* event payloads.
--   * find_or_create_tender: looks references up in tender_source_ref first, records the new
--     platform's reference on a hash match, and updates last_seen_at
--   * merge_tender: moves the duplicate's references AND its lots / amendments / submissions to
--     the survivor, renumbering children from the survivor's counters. Old child numbers are kept
--     in child_accession_alias and resolve via resolve_child_accession()
--   * already-merged tenders (merged under 002) have their children moved by this migration
--   * new event type 'deadline_changed' (written by ingest.add_amendment)
\set ON_ERROR_STOP 1
BEGIN;

-- ---------- Event type for deadline changes ----------
-- Only widen the list if it is still the 002 one, so re-running 003 on a later database never
-- narrows it (which would fail on rows using newer event types)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tender_event_event_type_check'
                 AND pg_get_constraintdef(oid) LIKE '%deadline_changed%') THEN
    ALTER TABLE tender_event DROP CONSTRAINT IF EXISTS tender_event_event_type_check;
    ALTER TABLE tender_event ADD CONSTRAINT tender_event_event_type_check CHECK (event_type IN (
      'created','matched_source_ref','matched_hash','merged','marked_different',
      'amended','lot_added','submission_added','status_changed','deadline_changed'));
  END IF;
END $$;

-- ---------- One lock-key recipe for SQL and Python ----------
CREATE OR REPLACE FUNCTION trc_lock_key(p_kind text, p_value text)
RETURNS bigint LANGUAGE sql IMMUTABLE AS $$
  SELECT hashtextextended('trc:' || p_kind || ':' || p_value, 0)
$$;

-- ---------- Platform references ----------
CREATE TABLE IF NOT EXISTS tender_source_ref (
  tender_id      uuid NOT NULL REFERENCES tender(id) ON DELETE RESTRICT,
  source         char(3) NOT NULL,
  source_ref     text NOT NULL,
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  is_primary     boolean NOT NULL DEFAULT false,   -- the reference the tender was created from
  PRIMARY KEY (source, source_ref)
);
CREATE INDEX IF NOT EXISTS tender_source_ref_tender_idx ON tender_source_ref (tender_id);
CREATE UNIQUE INDEX IF NOT EXISTS tender_source_ref_one_primary
  ON tender_source_ref (tender_id) WHERE is_primary;

-- Backfill a) each tender's own reference. A tender already merged gives its reference to the survivor.
INSERT INTO tender_source_ref (tender_id, source, source_ref, first_seen_at, last_seen_at, is_primary)
SELECT coalesce(t.merged_into, t.id), t.source, t.source_ref, t.captured_at, t.captured_at, t.merged_into IS NULL
FROM tender t
WHERE t.source_ref IS NOT NULL
ON CONFLICT (source, source_ref) DO NOTHING;

-- Backfill b) references seen in matched_* events (e.g. the TED copy of a PLACSP tender).
-- If one reference was routed to several tenders over time, the most recent routing wins.
INSERT INTO tender_source_ref (tender_id, source, source_ref, first_seen_at, last_seen_at, is_primary)
SELECT DISTINCT ON (e.source, e.payload->>'source_ref')
       coalesce(t.merged_into, t.id), e.source, e.payload->>'source_ref',
       min(e.occurred_at) OVER w, max(e.occurred_at) OVER w, false
FROM tender_event e
JOIN tender t ON t.id = e.tender_id
WHERE e.event_type IN ('matched_hash', 'matched_source_ref')
  AND e.source IS NOT NULL AND coalesce(e.payload->>'source_ref', '') <> ''
WINDOW w AS (PARTITION BY e.source, e.payload->>'source_ref')
ORDER BY e.source, e.payload->>'source_ref', e.id DESC
ON CONFLICT (source, source_ref) DO UPDATE
  SET last_seen_at = greatest(tender_source_ref.last_seen_at, EXCLUDED.last_seen_at)
  WHERE tender_source_ref.tender_id = EXCLUDED.tender_id;

-- ---------- Old child numbers after a merge ----------
CREATE TABLE IF NOT EXISTS child_accession_alias (
  old_accession_no text PRIMARY KEY,
  kind             char(1) NOT NULL CHECK (kind IN ('L','A','S')),
  child_id         uuid NOT NULL,        -- current row in tender_lot / tender_amendment / tender_submission
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS child_accession_alias_child_idx ON child_accession_alias (child_id);

-- Children move between tenders in two steps inside one transaction, so the lot FKs
-- must be checkable at commit. INITIALLY IMMEDIATE keeps normal behaviour for everyone else.
ALTER TABLE tender_amendment  ALTER CONSTRAINT tender_amendment_tender_id_lot_id_fkey  DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE tender_submission ALTER CONSTRAINT tender_submission_tender_id_lot_id_fkey DEFERRABLE INITIALLY IMMEDIATE;

-- Look up any child ID (current or pre-merge). Returns its current number and tender.
CREATE OR REPLACE FUNCTION resolve_child_accession(p_accession text)
RETURNS TABLE (kind char(1), child_id uuid, tender_id uuid, accession_no text)
LANGUAGE sql STABLE AS $$
  WITH q AS (SELECT upper(btrim(p_accession)) AS acc),
  cur AS (
    SELECT 'L'::char(1) AS kind, l.id, l.tender_id, l.accession_no FROM tender_lot l
    UNION ALL SELECT 'A', a.id, a.tender_id, a.accession_no FROM tender_amendment a
    UNION ALL SELECT 'S', s.id, s.tender_id, s.accession_no FROM tender_submission s)
  SELECT c.kind, c.id, c.tender_id, c.accession_no
  FROM q JOIN cur c ON c.accession_no = q.acc
  UNION ALL
  SELECT c.kind, c.id, c.tender_id, c.accession_no
  FROM q JOIN child_accession_alias al ON al.old_accession_no = q.acc
         JOIN cur c ON c.id = al.child_id AND c.kind = al.kind
  WHERE NOT EXISTS (SELECT 1 FROM cur c2 WHERE c2.accession_no = q.acc)
  LIMIT 1
$$;

-- Move lots, amendments and submissions from p_dup to p_surv, renumbering from the survivor's
-- counters. A lot with the same source_lot_ref, or an amendment with the same document_sha256,
-- already on the survivor is folded into it: its children are repointed, and its old number
-- aliases the survivor's row. The folded row itself is deleted, with a full copy in the returned
-- jsonb, which goes into the merged event. Caller must hold both tender rows locked.
CREATE OR REPLACE FUNCTION merge_move_children(p_dup uuid, p_surv uuid)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  r        record;
  v_tid    uuid;
  v_tacc   text;
  v_no     smallint;
  v_acc    text;
  v_lots   jsonb := '[]';
  v_ams    jsonb := '[]';
  v_subs   jsonb := '[]';
  v_folded jsonb := '[]';
BEGIN
  SET CONSTRAINTS tender_amendment_tender_id_lot_id_fkey, tender_submission_tender_id_lot_id_fkey DEFERRED;

  FOR r IN SELECT * FROM tender_lot WHERE tender_id = p_dup ORDER BY lot_no LOOP
    v_tid := NULL;
    IF r.source_lot_ref IS NOT NULL THEN
      SELECT l.id, l.accession_no INTO v_tid, v_tacc
      FROM tender_lot l WHERE l.tender_id = p_surv AND l.source_lot_ref = r.source_lot_ref;
    END IF;
    IF v_tid IS NOT NULL THEN
      UPDATE tender_amendment  SET lot_id = v_tid WHERE lot_id = r.id;
      UPDATE tender_submission SET lot_id = v_tid WHERE lot_id = r.id;
      UPDATE child_accession_alias SET child_id = v_tid WHERE child_id = r.id;
      INSERT INTO child_accession_alias (old_accession_no, kind, child_id) VALUES (r.accession_no, 'L', v_tid)
      ON CONFLICT (old_accession_no) DO UPDATE SET child_id = EXCLUDED.child_id;
      DELETE FROM tender_lot WHERE id = r.id;
      v_folded := v_folded || jsonb_build_object('kind', 'L', 'old', r.accession_no, 'into', v_tacc, 'row', to_jsonb(r));
    ELSE
      SELECT n.child_no, n.child_accession INTO v_no, v_acc FROM issue_child_no(p_surv, 'L') n;
      UPDATE tender_lot SET tender_id = p_surv, lot_no = v_no, accession_no = v_acc WHERE id = r.id;
      INSERT INTO child_accession_alias (old_accession_no, kind, child_id) VALUES (r.accession_no, 'L', r.id)
      ON CONFLICT (old_accession_no) DO UPDATE SET child_id = EXCLUDED.child_id;
      v_lots := v_lots || jsonb_build_object('old', r.accession_no, 'new', v_acc);
    END IF;
  END LOOP;

  FOR r IN SELECT * FROM tender_amendment WHERE tender_id = p_dup ORDER BY amendment_no LOOP
    v_tid := NULL;
    IF r.document_sha256 IS NOT NULL THEN
      SELECT a.id, a.accession_no INTO v_tid, v_tacc
      FROM tender_amendment a WHERE a.tender_id = p_surv AND a.document_sha256 = r.document_sha256;
    END IF;
    IF v_tid IS NOT NULL THEN
      UPDATE child_accession_alias SET child_id = v_tid WHERE child_id = r.id;
      INSERT INTO child_accession_alias (old_accession_no, kind, child_id) VALUES (r.accession_no, 'A', v_tid)
      ON CONFLICT (old_accession_no) DO UPDATE SET child_id = EXCLUDED.child_id;
      DELETE FROM tender_amendment WHERE id = r.id;
      v_folded := v_folded || jsonb_build_object('kind', 'A', 'old', r.accession_no, 'into', v_tacc, 'row', to_jsonb(r));
    ELSE
      SELECT n.child_no, n.child_accession INTO v_no, v_acc FROM issue_child_no(p_surv, 'A') n;
      UPDATE tender_amendment SET tender_id = p_surv, amendment_no = v_no, accession_no = v_acc WHERE id = r.id;
      INSERT INTO child_accession_alias (old_accession_no, kind, child_id) VALUES (r.accession_no, 'A', r.id)
      ON CONFLICT (old_accession_no) DO UPDATE SET child_id = EXCLUDED.child_id;
      v_ams := v_ams || jsonb_build_object('old', r.accession_no, 'new', v_acc);
    END IF;
  END LOOP;

  FOR r IN SELECT * FROM tender_submission WHERE tender_id = p_dup ORDER BY submission_no LOOP
    SELECT n.child_no, n.child_accession INTO v_no, v_acc FROM issue_child_no(p_surv, 'S') n;
    UPDATE tender_submission SET tender_id = p_surv, submission_no = v_no, accession_no = v_acc WHERE id = r.id;
    INSERT INTO child_accession_alias (old_accession_no, kind, child_id) VALUES (r.accession_no, 'S', r.id)
    ON CONFLICT (old_accession_no) DO UPDATE SET child_id = EXCLUDED.child_id;
    v_subs := v_subs || jsonb_build_object('old', r.accession_no, 'new', v_acc);
  END LOOP;

  -- Check the FKs now rather than at commit, so a problem fails here with a clear error
  SET CONSTRAINTS tender_amendment_tender_id_lot_id_fkey, tender_submission_tender_id_lot_id_fkey IMMEDIATE;
  RETURN jsonb_build_object('lots', v_lots, 'amendments', v_ams, 'submissions', v_subs, 'folded', v_folded);
END $$;

-- ---------- find_or_create_tender v3 ----------
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
  v_k_hash  bigint;
  v_k_ref   bigint;
  v_payload jsonb := coalesce(p_payload, '{}'::jsonb)
                     || jsonb_build_object('source_ref', p_source_ref, 'dedupe_hash', p_hash);
BEGIN
  IF p_source IS NULL OR p_hash IS NULL OR p_title IS NULL OR p_actor IS NULL THEN
    RAISE EXCEPTION 'find_or_create_tender: source, hash, title and actor are required';
  END IF;

  -- Serialise on the reference AND the hash, always in key order (merge_tender uses the same keys)
  v_k_hash := trc_lock_key('hash', p_hash);
  IF p_source_ref IS NOT NULL THEN
    v_k_ref := trc_lock_key('ref', p_source || ':' || p_source_ref);
    PERFORM pg_advisory_xact_lock(least(v_k_hash, v_k_ref));
    PERFORM pg_advisory_xact_lock(greatest(v_k_hash, v_k_ref));
  ELSE
    PERFORM pg_advisory_xact_lock(v_k_hash);
  END IF;

  IF p_source_ref IS NOT NULL THEN
    -- 1a. Any platform reference we have seen before, on any tender (follow a merge to the survivor)
    SELECT r.tender_id, coalesce(t.merged_into, t.id) INTO v_ref_tid, v_id
    FROM tender_source_ref r JOIN tender t ON t.id = r.tender_id
    WHERE r.source = p_source AND r.source_ref = p_source_ref;
    IF FOUND THEN
      UPDATE tender_source_ref r
         SET last_seen_at = now(),
             tender_id    = v_id,
             is_primary   = r.is_primary AND v_ref_tid = v_id
       WHERE r.source = p_source AND r.source_ref = p_source_ref;
      INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
      VALUES (v_id, 'matched_source_ref', p_actor, p_source, v_payload || '{"via":"tender_source_ref"}');
      RETURN QUERY SELECT t.id, t.accession_no, 'matched_source_ref'::text FROM tender t WHERE t.id = v_id;
      RETURN;
    END IF;

    -- 1b. A row inserted straight into tender (not through here) has no tender_source_ref row yet
    SELECT t.id, coalesce(t.merged_into, t.id) INTO v_ref_tid, v_id
    FROM tender t WHERE t.source = p_source AND t.source_ref = p_source_ref;
    IF FOUND THEN
      INSERT INTO tender_source_ref (tender_id, source, source_ref, is_primary)
      VALUES (v_id, p_source, p_source_ref, v_ref_tid = v_id
              AND NOT EXISTS (SELECT 1 FROM tender_source_ref x WHERE x.tender_id = v_id AND x.is_primary));
      INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
      VALUES (v_id, 'matched_source_ref', p_actor, p_source, v_payload || '{"via":"tender"}');
      RETURN QUERY SELECT t.id, t.accession_no, 'matched_source_ref'::text FROM tender t WHERE t.id = v_id;
      RETURN;
    END IF;
  END IF;

  -- 2. Same cleaned title + buyer + deadline day. Prefer a live tender; a hash that only exists
  --    on a merged duplicate goes to its survivor.
  SELECT coalesce(t.merged_into, t.id) INTO v_id FROM tender t
  WHERE t.dedupe_hash = p_hash AND t.status <> 'withdrawn'
  ORDER BY (t.merged_into IS NULL) DESC, t.created_at
  LIMIT 1;
  IF FOUND THEN
    IF p_source_ref IS NOT NULL THEN   -- remember this platform's reference for next time
      INSERT INTO tender_source_ref (tender_id, source, source_ref, is_primary)
      VALUES (v_id, p_source, p_source_ref, false)
      ON CONFLICT (source, source_ref) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at;
    END IF;
    INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
    VALUES (v_id, 'matched_hash', p_actor, p_source,
            v_payload || jsonb_build_object('ref_recorded', p_source_ref IS NOT NULL));
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
    INSERT INTO tender_source_ref (tender_id, source, source_ref, is_primary)
    VALUES (v_id, p_source, p_source_ref, true);
  END IF;

  INSERT INTO tender_event (tender_id, event_type, actor, source, payload)
  VALUES (v_id, 'created', p_actor, p_source, v_payload);

  RETURN QUERY SELECT t.id, t.accession_no, 'created'::text FROM tender t WHERE t.id = v_id;
END $$;

-- ---------- merge_tender v3 ----------
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

  -- Same advisory keys find_or_create_tender uses, taken in key order before any row lock,
  -- so a scrape of either tender waits for the merge (or the merge waits for it)
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

  -- NO KEY UPDATE: blocks other merges and child numbering, but not event inserts (FK key-share)
  PERFORM 1 FROM tender WHERE id IN (p_duplicate, p_survivor) ORDER BY id FOR NO KEY UPDATE;
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
  UPDATE tender SET merged_into = p_survivor WHERE merged_into = p_duplicate;   -- keep merges one level deep

  INSERT INTO accession_alias (old_accession_no, tender_id)
  VALUES (v_dup.accession_no, p_survivor)
  ON CONFLICT (old_accession_no) DO UPDATE SET tender_id = EXCLUDED.tender_id;
  UPDATE accession_alias SET tender_id = p_survivor WHERE tender_id = p_duplicate;
  UPDATE tender_possible_duplicate SET resolution = 'same'
  WHERE (tender_id, candidate_id) IN ((p_duplicate, p_survivor), (p_survivor, p_duplicate));

  -- Platform references follow the tender; the survivor keeps its own primary
  WITH moved AS (
    UPDATE tender_source_ref SET tender_id = p_survivor, is_primary = false
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

-- ---------- Catch up tenders merged before 003 ----------
DO $$
DECLARE
  r record;
  v_children jsonb;
BEGIN
  FOR r IN
    SELECT t.id, t.merged_into, t.accession_no FROM tender t
    WHERE t.merged_into IS NOT NULL
      AND (EXISTS (SELECT 1 FROM tender_lot WHERE tender_id = t.id)
        OR EXISTS (SELECT 1 FROM tender_amendment WHERE tender_id = t.id)
        OR EXISTS (SELECT 1 FROM tender_submission WHERE tender_id = t.id))
    ORDER BY t.created_at
  LOOP
    PERFORM 1 FROM tender WHERE id IN (r.id, r.merged_into) ORDER BY id FOR NO KEY UPDATE;
    v_children := merge_move_children(r.id, r.merged_into);
    INSERT INTO tender_event (tender_id, event_type, actor, payload) VALUES
      (r.id, 'merged', 'migration:003', jsonb_build_object('role', 'duplicate', 'into', r.merged_into,
         'children_moved', v_children, 'note', 'children moved after an earlier merge')),
      (r.merged_into, 'merged', 'migration:003', jsonb_build_object('role', 'survivor', 'absorbed', r.id,
         'absorbed_accession', r.accession_no, 'children_moved', v_children,
         'note', 'children moved after an earlier merge'));
  END LOOP;
END $$;

COMMIT;
