-- 005_platforms_lots.sql  --  Tender & RFP Cockpit: platform registry + lot origins/labels
-- Apply AFTER 004_change_tracking.sql. Safe to re-run. Always run 002 → 003 → 004 → 005 together
-- (apply.sh does). One transaction.
--
-- What it changes:
--   * source_platform: the registry of tender platforms. Its 3-letter code is the one used in
--     accession numbers (TRC-26-PLC-000001-L). tender.source and tender_source_ref.source get a
--     foreign key to it. Seeds PLC, FTS, TED, MAN (active); any other code already used in the
--     data is added as 'proposed' with a WARNING (review it, then `platforms.py activate CODE`).
--   * Code rules (trigger source_platform_guard): a code is frozen the first time a tender or a
--     platform reference uses it (frozen_at is set automatically). A frozen, used or retired code
--     can never be changed or deleted; name/short_name/url/notes/priority can. A retired platform
--     can never be reactivated, so its code is never reused.
--   * New tenders need an ACTIVE platform (trigger tender_platform_use).
--   * tender_lot: source_lot_label (the platform's raw label, e.g. "Lote 1"), lot_origin
--     (structured | ai_extracted | manual), confirmed_by / confirmed_at; source_lot_ref now holds
--     the canonical lot ref ("Lote 1", "Lot 1", "LOT-0001", "1" -> "1"), computed in Python
--     (lots.canonical_lot_ref). trc_canonical_lot_ref() is the SQL twin, used here for the
--     backfill and by merge folding; test_platforms_lots.py checks both give the same answers.
--   * merge_move_children v5: folds lots whose canonical refs are equal (fixes "Lote 1" vs "Lot 1").
--   * event type lot_confirmed.
\set ON_ERROR_STOP 1
BEGIN;

-- ---------- Event types ----------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tender_event_event_type_check'
                 AND pg_get_constraintdef(oid) LIKE '%lot_confirmed%') THEN
    ALTER TABLE tender_event DROP CONSTRAINT IF EXISTS tender_event_event_type_check;
    ALTER TABLE tender_event ADD CONSTRAINT tender_event_event_type_check CHECK (event_type IN (
      'created','matched_source_ref','matched_hash','merged','marked_different',
      'amended','lot_added','submission_added','status_changed','deadline_changed',
      'fields_changed','field_locked','field_unlocked','lot_confirmed'));
  END IF;
END $$;

-- ---------- Platform registry ----------
CREATE TABLE IF NOT EXISTS source_platform (
  code         char(3) PRIMARY KEY,
  name         text NOT NULL,
  short_name   text,
  url          text,
  domain       text,                       -- lower case, no "www.", no port (platforms.domain_of)
  country      char(2),                    -- ISO 3166-1 alpha-2; NULL for multi-country (TED)
  data_format  text NOT NULL DEFAULT 'other',
  priority     integer NOT NULL DEFAULT 10, -- who may overwrite tender fields (changes.py)
  status       text NOT NULL DEFAULT 'proposed',
  code_origin  text NOT NULL DEFAULT 'manual',
  frozen_at    timestamptz,                -- first time a tender used the code; set automatically
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz,
  notes        text,
  CONSTRAINT source_platform_code_format CHECK (code ~ '^[A-Z]{3}$'),
  CONSTRAINT source_platform_name_present CHECK (btrim(name) <> ''),
  CONSTRAINT source_platform_domain_format
    CHECK (domain IS NULL OR (domain = lower(domain) AND domain !~ '^www\.' AND domain ~ '^[a-z0-9.-]+\.[a-z0-9-]+$')),
  CONSTRAINT source_platform_country_format CHECK (country IS NULL OR country ~ '^[A-Z]{2}$'),
  CONSTRAINT source_platform_data_format CHECK (data_format IN ('codice','eforms','ocds','html','pdf','other')),
  CONSTRAINT source_platform_priority_range CHECK (priority BETWEEN 0 AND 1000),
  CONSTRAINT source_platform_status CHECK (status IN ('proposed','active','retired')),
  CONSTRAINT source_platform_code_origin CHECK (code_origin IN ('suggested','manual'))
);
-- 006 replaces this index by source_platform_site_uq (domain, tenant_key); re-running 005 after 006
-- must not bring the old one back (shared hosts have several rows per domain).
DO $$
BEGIN
  IF to_regclass('source_platform_site_uq') IS NULL THEN
    CREATE UNIQUE INDEX IF NOT EXISTS source_platform_domain_uq ON source_platform (domain) WHERE domain IS NOT NULL;
  END IF;
END $$;

-- Seeds: the codes used so far. Never overwrites a row someone already edited.
INSERT INTO source_platform (code, name, short_name, url, domain, country, data_format, priority, status, code_origin, notes)
VALUES
  ('PLC', 'Plataforma de Contratación del Sector Público', 'PLACSP', 'https://contrataciondelestado.es',
   'contrataciondelestado.es', 'ES', 'codice', 100, 'active', 'manual', 'seeded by 005'),
  ('FTS', 'Find a Tender', 'FTS', 'https://www.find-tender.service.gov.uk',
   'find-tender.service.gov.uk', 'GB', 'ocds', 100, 'active', 'manual', 'seeded by 005'),
  ('TED', 'Tenders Electronic Daily', 'TED', 'https://ted.europa.eu',
   'ted.europa.eu', NULL, 'eforms', 50, 'active', 'manual', 'seeded by 005 (EU-wide, no country)'),
  ('MAN', 'Manual entry', 'Manual', NULL, NULL, NULL, 'other', 10, 'active', 'manual',
   'seeded by 005: tenders typed in by a person')
ON CONFLICT (code) DO NOTHING;

-- Any other code already in the data: register it as proposed so the FKs can be added.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT DISTINCT c AS code FROM (
             SELECT source::text AS c FROM tender
             UNION SELECT source::text FROM tender_source_ref
             UNION SELECT source::text FROM accession_counter) x
           WHERE c IS NOT NULL AND NOT EXISTS (SELECT 1 FROM source_platform p WHERE p.code = x.c)
           ORDER BY 1 LOOP
    IF r.code !~ '^[A-Z]{3}$' THEN
      RAISE EXCEPTION 'source code % in existing data is not three capital letters; fix it before applying 005', r.code;
    END IF;
    RAISE WARNING 'source code % is used in existing data but unknown: registered as PROPOSED (name = code). Review it, then run: python platforms.py rename % --name ... and python platforms.py activate %', r.code, r.code, r.code;
    INSERT INTO source_platform (code, name, status, code_origin, notes)
    VALUES (r.code, r.code, 'proposed', 'manual', 'backfilled by 005: code found in existing data');
  END LOOP;
END $$;

-- Freeze every code already used (first use = oldest tender or reference).
UPDATE source_platform p SET frozen_at = u.first_use
FROM (SELECT c, min(t) AS first_use FROM (
        SELECT source::text AS c, captured_at AS t FROM tender
        UNION ALL SELECT source::text, first_seen_at FROM tender_source_ref) x GROUP BY c) u
WHERE p.code = u.c AND p.frozen_at IS NULL;

CREATE OR REPLACE FUNCTION trc_platform_code_used(p_code char(3)) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM tender WHERE source = p_code)
      OR EXISTS (SELECT 1 FROM tender_source_ref WHERE source = p_code)
      OR EXISTS (SELECT 1 FROM accession_counter WHERE source = p_code)
$$;

CREATE OR REPLACE FUNCTION source_platform_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status = 'retired' THEN
      RAISE EXCEPTION 'platform % is retired: retired codes are kept forever so they can never be reused', OLD.code;
    END IF;
    IF OLD.frozen_at IS NOT NULL OR trc_platform_code_used(OLD.code) THEN
      RAISE EXCEPTION 'platform code % is frozen (in use since %) and cannot be deleted; retire it instead',
        OLD.code, coalesce(OLD.frozen_at::text, 'existing data');
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.code IS DISTINCT FROM OLD.code THEN
    IF OLD.status = 'retired' THEN
      RAISE EXCEPTION 'platform % is retired: its code can never be changed or reused', OLD.code;
    END IF;
    IF OLD.frozen_at IS NOT NULL OR trc_platform_code_used(OLD.code) THEN
      RAISE EXCEPTION 'platform code % is frozen (in use since %): it is part of accession numbers and cannot change. Renaming name/url is allowed',
        OLD.code, coalesce(OLD.frozen_at::text, 'existing data');
    END IF;
  END IF;
  IF OLD.status = 'retired' AND NEW.status <> 'retired' THEN
    RAISE EXCEPTION 'platform % is retired and cannot be reactivated (retired codes are never reused); register the platform again under a new code', OLD.code;
  END IF;
  IF OLD.frozen_at IS NOT NULL AND NEW.frozen_at IS DISTINCT FROM OLD.frozen_at THEN
    RAISE EXCEPTION 'frozen_at of platform % is set automatically and cannot be changed', OLD.code;
  END IF;
  IF ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*) THEN
    NEW.updated_at := now();
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS source_platform_guard ON source_platform;
CREATE TRIGGER source_platform_guard BEFORE UPDATE OR DELETE ON source_platform
  FOR EACH ROW EXECUTE FUNCTION source_platform_guard();
-- TRUNCATE would skip the row trigger
CREATE OR REPLACE FUNCTION source_platform_no_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'source_platform cannot be truncated (codes are frozen / never reused)';
END $$;
DROP TRIGGER IF EXISTS source_platform_no_truncate ON source_platform;
CREATE TRIGGER source_platform_no_truncate BEFORE TRUNCATE ON source_platform
  FOR EACH STATEMENT EXECUTE FUNCTION source_platform_no_truncate();

-- First use of a code freezes it. A new tender needs an ACTIVE platform; a new platform
-- reference only needs the platform to exist (the FK) so re-running 003's backfill never fails.
CREATE OR REPLACE FUNCTION trc_platform_use() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_status text;
  v_frozen timestamptz;
BEGIN
  SELECT status, frozen_at INTO v_status, v_frozen FROM source_platform WHERE code = NEW.source;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown platform code %: register it first (python platforms.py suggest/apply)', NEW.source
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF TG_TABLE_NAME = 'tender' AND v_status <> 'active' THEN
    RAISE EXCEPTION 'platform % is %, not active: new tenders need an active platform (python platforms.py activate %)',
      NEW.source, v_status, NEW.source USING ERRCODE = 'check_violation';
  END IF;
  IF v_frozen IS NULL THEN
    UPDATE source_platform SET frozen_at = now() WHERE code = NEW.source AND frozen_at IS NULL;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS tender_platform_use ON tender;
CREATE TRIGGER tender_platform_use BEFORE INSERT OR UPDATE OF source ON tender
  FOR EACH ROW EXECUTE FUNCTION trc_platform_use();
DROP TRIGGER IF EXISTS tender_source_ref_platform_use ON tender_source_ref;
CREATE TRIGGER tender_source_ref_platform_use BEFORE INSERT OR UPDATE OF source ON tender_source_ref
  FOR EACH ROW EXECUTE FUNCTION trc_platform_use();

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tender_source_platform_fk') THEN
    ALTER TABLE tender ADD CONSTRAINT tender_source_platform_fk
      FOREIGN KEY (source) REFERENCES source_platform (code) ON UPDATE RESTRICT ON DELETE RESTRICT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tender_source_ref_platform_fk') THEN
    ALTER TABLE tender_source_ref ADD CONSTRAINT tender_source_ref_platform_fk
      FOREIGN KEY (source) REFERENCES source_platform (code) ON UPDATE RESTRICT ON DELETE RESTRICT;
  END IF;
END $$;

-- ---------- Lots: canonical refs, raw labels, origin, confirmation ----------
-- SQL twin of lots.canonical_lot_ref() (Python is the source of truth; keep them identical):
--   accents folded, non-alphanumerics -> one space, upper case; a leading "Lot/Lote/Lots/Lotes/
--   Lotto/Lotti/Los/Lose/Partida(s)" word and a number marker ("nº", "no", "nr", "num",
--   "número", "number", "n") are dropped; then a number (leading zeros dropped, plus one
--   letter suffix like 1A) or, after a lot word, the first token; otherwise the whole label.
CREATE OR REPLACE FUNCTION trc_canonical_lot_ref(p_label text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  s text;
  m text[];
  had_prefix boolean := false;
BEGIN
  IF p_label IS NULL THEN RETURN NULL; END IF;
  s := translate(p_label,
    'áàâäãåāéèêëēíìîïīóòôöõøōúùûüūçñýÿÁÀÂÄÃÅĀÉÈÊËĒÍÌÎÏĪÓÒÔÖÕØŌÚÙÛÜŪÇÑÝºª°',
    'aaaaaaaeeeeeiiiiiooooooouuuuucnyyAAAAAAAEEEEEIIIIIOOOOOOOUUUUUCNYoao');
  s := upper(btrim(regexp_replace(s, '[^A-Za-z0-9]+', ' ', 'g')));
  IF s = '' THEN RETURN NULL; END IF;
  m := regexp_match(s, '^(LOTES|LOTE|LOTS|LOT|LOTTI|LOTTO|LOSE|LOS|PARTIDAS|PARTIDA)(?= |[0-9]|$) ?((NO|NR|NUM|NUMERO|NUMBER|N)(?= |[0-9]) ?)?');
  IF m IS NOT NULL THEN
    had_prefix := true;
    s := btrim(substr(s, length(m[1]) + 1));
    s := btrim(regexp_replace(s, '^(NO|NR|NUM|NUMERO|NUMBER|N)(?= |[0-9]) ?', ''));
    IF s = '' THEN RETURN m[1]; END IF;
  END IF;
  m := regexp_match(s, '^([0-9]+)([A-Z]?)(?= |$)');
  IF m IS NOT NULL THEN
    RETURN coalesce(nullif(ltrim(m[1], '0'), ''), '0') || m[2];
  END IF;
  IF had_prefix THEN
    RETURN split_part(s, ' ', 1);
  END IF;
  RETURN s;
END $$;

DO $$
DECLARE
  v_new_origin boolean := NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name = 'tender_lot' AND column_name = 'lot_origin');
  r record;
BEGIN
  ALTER TABLE tender_lot ADD COLUMN IF NOT EXISTS source_lot_label text;
  ALTER TABLE tender_lot ADD COLUMN IF NOT EXISTS lot_origin text NOT NULL DEFAULT 'structured';
  ALTER TABLE tender_lot ADD COLUMN IF NOT EXISTS confirmed_by text;
  ALTER TABLE tender_lot ADD COLUMN IF NOT EXISTS confirmed_at timestamptz;
  IF v_new_origin THEN
    -- Existing lots: 'structured' (a scraper added them) unless nothing points at a platform:
    -- no source_lot_ref and the lot_added event has no source -> a person added it ('manual').
    UPDATE tender_lot l SET lot_origin = 'manual'
    WHERE l.source_lot_ref IS NULL
      AND NOT EXISTS (SELECT 1 FROM tender_event e
                      WHERE e.event_type = 'lot_added' AND e.source IS NOT NULL
                        AND e.payload->>'lot_id' = l.id::text);
  END IF;
  -- Keep the raw label once, then store the canonical ref (skipped on a clash inside one tender)
  UPDATE tender_lot SET source_lot_label = source_lot_ref
  WHERE source_lot_label IS NULL AND source_lot_ref IS NOT NULL;
  FOR r IN SELECT l.id, l.tender_id, l.accession_no, l.source_lot_ref, trc_canonical_lot_ref(l.source_lot_ref) AS canon
           FROM tender_lot l
           WHERE l.source_lot_ref IS NOT NULL AND l.source_lot_ref IS DISTINCT FROM trc_canonical_lot_ref(l.source_lot_ref)
           ORDER BY l.tender_id, l.lot_no LOOP
    IF EXISTS (SELECT 1 FROM tender_lot o WHERE o.tender_id = r.tender_id AND o.id <> r.id
               AND (o.source_lot_ref = r.canon OR trc_canonical_lot_ref(o.source_lot_ref) = r.canon
                    AND o.lot_no < (SELECT lot_no FROM tender_lot WHERE id = r.id))) THEN
      RAISE WARNING 'lot % keeps source_lot_ref % : another lot of the same tender has canonical ref % (likely the same lot twice; review it)',
        r.accession_no, r.source_lot_ref, r.canon;
    ELSE
      UPDATE tender_lot SET source_lot_ref = r.canon WHERE id = r.id;
    END IF;
  END LOOP;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tender_lot_origin_check') THEN
    ALTER TABLE tender_lot ADD CONSTRAINT tender_lot_origin_check
      CHECK (lot_origin IN ('structured','ai_extracted','manual'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tender_lot_confirmed_pair') THEN
    ALTER TABLE tender_lot ADD CONSTRAINT tender_lot_confirmed_pair
      CHECK ((confirmed_by IS NULL) = (confirmed_at IS NULL));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS tender_lot_unconfirmed_idx ON tender_lot (tender_id)
  WHERE lot_origin = 'ai_extracted' AND confirmed_at IS NULL;

-- ---------- merge_move_children v5: fold on the canonical lot ref ----------
CREATE OR REPLACE FUNCTION merge_move_children(p_dup uuid, p_surv uuid)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  r        record;
  v_tid    uuid;
  v_tacc   text;
  v_torig  text;
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
      SELECT l.id, l.accession_no, l.lot_origin INTO v_tid, v_tacc, v_torig
      FROM tender_lot l WHERE l.tender_id = p_surv
        AND trc_canonical_lot_ref(l.source_lot_ref) = trc_canonical_lot_ref(r.source_lot_ref)
      ORDER BY l.lot_no LIMIT 1;
    END IF;
    IF v_tid IS NOT NULL THEN
      UPDATE tender_amendment  SET lot_id = v_tid WHERE lot_id = r.id;
      UPDATE tender_submission SET lot_id = v_tid WHERE lot_id = r.id;
      UPDATE child_accession_alias SET child_id = v_tid WHERE child_id = r.id;
      INSERT INTO child_accession_alias (old_accession_no, kind, child_id) VALUES (r.accession_no, 'L', v_tid)
      ON CONFLICT (old_accession_no) DO UPDATE SET child_id = EXCLUDED.child_id;
      -- A platform's structured lot beats an AI-extracted guess on the survivor; fill gaps
      UPDATE tender_lot l SET
        lot_origin      = CASE WHEN l.lot_origin = 'ai_extracted' AND r.lot_origin = 'structured' THEN 'structured' ELSE l.lot_origin END,
        description     = coalesce(l.description, r.description),
        cpv_codes       = coalesce(l.cpv_codes, r.cpv_codes),
        estimated_value = coalesce(l.estimated_value, r.estimated_value),
        currency        = coalesce(l.currency, r.currency)
      WHERE l.id = v_tid;
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

  SET CONSTRAINTS tender_amendment_tender_id_lot_id_fkey, tender_submission_tender_id_lot_id_fkey IMMEDIATE;
  RETURN jsonb_build_object('lots', v_lots, 'amendments', v_ams, 'submissions', v_subs, 'folded', v_folded);
END $$;

COMMIT;
