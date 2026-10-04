-- Review queue for near-misses the exact hash can't catch
CREATE TABLE IF NOT EXISTS tender_possible_duplicate (
  tender_id     uuid NOT NULL REFERENCES tender(id),
  candidate_id  uuid NOT NULL REFERENCES tender(id),
  reason        text NOT NULL,
  similarity    real,
  resolution    text CHECK (resolution IN ('same','different')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tender_id, candidate_id),
  CHECK (tender_id <> candidate_id)
);

-- Stops two scrapers creating the same tender at once when neither has a source_ref
CREATE UNIQUE INDEX IF NOT EXISTS tender_live_hash_unique
  ON tender (dedupe_hash) WHERE merged_into IS NULL AND status <> 'withdrawn';

CREATE OR REPLACE FUNCTION find_or_create_tender(
  p_source char(3), p_source_ref text, p_hash text,
  p_title text, p_buyer text, p_deadline timestamptz)
RETURNS TABLE (tender_id uuid, accession_no text, outcome text)
LANGUAGE plpgsql AS $$
DECLARE
  v_id uuid;
BEGIN
  -- Serialise work on the same tender so concurrent scrapers wait instead of racing
  PERFORM pg_advisory_xact_lock(hashtextextended(coalesce(p_source || ':' || p_source_ref, p_hash), 0));

  -- 1. Same platform reference: always the same tender
  IF p_source_ref IS NOT NULL THEN
    SELECT t.id INTO v_id FROM tender t
    WHERE t.source = p_source AND t.source_ref = p_source_ref;
    IF FOUND THEN
      RETURN QUERY SELECT r.id, r.accession_no, 'matched_source_ref'::text
        FROM tender r WHERE r.id = (SELECT coalesce(merged_into, id) FROM tender WHERE id = v_id);
      RETURN;
    END IF;
  END IF;

  -- 2. Same cleaned title, buyer and deadline: a re-post or a copy on another platform
  SELECT t.id INTO v_id FROM tender t
  WHERE t.dedupe_hash = p_hash AND t.merged_into IS NULL AND t.status <> 'withdrawn'
  LIMIT 1;
  IF FOUND THEN
    RETURN QUERY SELECT t.id, t.accession_no, 'matched_hash'::text FROM tender t WHERE t.id = v_id;
    RETURN;
  END IF;

  -- 3. New tender
  INSERT INTO tender (accession_no, source, source_ref, dedupe_hash, title, buyer, deadline_at)
  VALUES (issue_accession(p_source), p_source, p_source_ref, p_hash, p_title, p_buyer, p_deadline)
  RETURNING id INTO v_id;

  -- Flag close-but-not-exact matches from the same buyer for a person to review
  INSERT INTO tender_possible_duplicate (tender_id, candidate_id, reason, similarity)
  SELECT v_id, t.id, 'same buyer, similar title, deadline within 7 days', similarity(t.title, p_title)
  FROM tender t
  WHERE t.id <> v_id AND t.merged_into IS NULL
    AND lower(t.buyer) = lower(p_buyer)
    AND abs(extract(epoch FROM (t.deadline_at - p_deadline))) <= 7 * 86400
    AND similarity(t.title, p_title) >= 0.6
  ON CONFLICT DO NOTHING;

  RETURN QUERY SELECT t.id, t.accession_no, 'created'::text FROM tender t WHERE t.id = v_id;
END $$;

-- Merge a confirmed duplicate: keeps both numbers, old one resolves to the survivor
CREATE OR REPLACE FUNCTION merge_tender(p_duplicate uuid, p_survivor uuid)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF p_duplicate = p_survivor THEN RAISE EXCEPTION 'Cannot merge a tender into itself'; END IF;
  UPDATE tender SET merged_into = p_survivor, status = 'merged' WHERE id = p_duplicate;
  INSERT INTO accession_alias (old_accession_no, tender_id)
  SELECT accession_no, p_survivor FROM tender WHERE id = p_duplicate
  ON CONFLICT (old_accession_no) DO UPDATE SET tender_id = EXCLUDED.tender_id;
  UPDATE accession_alias SET tender_id = p_survivor
  WHERE tender_id = p_duplicate;
  UPDATE tender_possible_duplicate SET resolution = 'same'
  WHERE (tender_id, candidate_id) IN ((p_duplicate, p_survivor), (p_survivor, p_duplicate));
END $$;

-- Look up any ID, including old merged ones
CREATE OR REPLACE FUNCTION resolve_accession(p_accession text)
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    (SELECT coalesce(merged_into, id) FROM tender WHERE accession_no = split_part(upper(p_accession), '/', 1)),
    (SELECT tender_id FROM accession_alias WHERE old_accession_no = split_part(upper(p_accession), '/', 1)));
$$;
