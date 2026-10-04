# Tender & RFP Cockpit: ingestion v6 (your platform list, shared-host tenants)

This folder has everything the ingestion layer needs. Postgres 17 runs it today. The
design keeps Snowflake in mind: **business rules live in Python and the SQL does only the atomic parts.**

## Architecture

```
scraper ──► ingest.ingest_tender(conn, scraped, actor)          (Python: validate, clean, hash)
                 │
                 ▼
          find_or_create_tender(...)                             (SQL: lock → tender_source_ref → hash → hash history → insert)
                 │  INSERT without accession_no           │ matched an existing tender
                 │                                        ▼
                 │                        changes.apply_changes(...)     (Python, same transaction + locks:
                 │                                                        precedence, locks, diff, new hash, fields_changed)
                 ▼
          BEFORE INSERT trigger tender_fill_accession            (fills accession_no only, no dedupe)
                 │
                 ▼
          tender_event (append-only)  ◄── merge_tender / mark_not_duplicate / add_lot / add_amendment / add_submission / set_status

cron / --loop ──► candidates.py ──► tender_possible_duplicate ──► review.py (list / same / different / history)
```

* **Python is the only source of truth for cleaning and hashing** (`dedupe.py`). The database stores
  what Python sends: `dedupe_hash`, `title_norm` and `buyer_norm`. SQL never recomputes them.
* **Exact duplicates** are resolved at ingest time. The first check is any `(source, source_ref)`
  the tender is known by, taken from `tender_source_ref`. The second is the same hash (cleaned
  title + buyer + deadline day). When a second platform's copy matches by hash, its reference is
  recorded, so later re-scrapes from that platform still match after the title changes. A match on
  a merged tender always resolves to the tender it was merged into.
* **Merging** moves the duplicate's platform references (as non-primary), lots, amendments and
  submissions to the survivor. Children get new numbers from the survivor's counters, and old
  child IDs keep resolving via `child_accession_alias` / `resolve_child_accession()`. A lot with the
  same `source_lot_ref`, or an amendment with the same `document_sha256`, already on the survivor is
  folded into it. Its children are repointed, and the folded row is kept in full in the `merged` event.
* **Deadline changes:** `add_amendment(kind="deadline_change", new_deadline_at=...)` moves
  `tender.deadline_at` (lots have no deadline, so a lot-scoped change also moves the tender's). It
  recomputes the dedupe hash in Python and logs `deadline_changed`.
* **Re-scrapes update the tender (v4).** When a scrape matches an existing tender, the fields
  that changed are written back and logged as one `fields_changed` event. See "Change tracking" below.
* **Platform registry (v5).** Every platform has a row in `source_platform`; its three-letter code
  is the one in accession numbers, is frozen on first use and can never be reused. Scrapers name
  the platform by code, name or URL; unknown or non-active platforms are rejected. See "Platforms".
* **Your platform list (v6).** `006_seed_platforms.sql` registers the 363 platforms of your
  cleaned list as active (see "Cleaning a platform list (v6)"). A platform is now unique by
  `(domain, tenant_key)`, so customers on a shared host (in-tendhost universities, PlanetBids
  agencies, Jaggaer/SciQuest customers) are separate platforms that resolve by path or parameter.
* **Lots (v5).** `lots.py` reads lots from PLACSP CODICE, TED eForms and Find a Tender OCDS, and
  (heuristically) from document text. Lot labels are canonicalised ("Lote 1" = "Lot 1" =
  "LOT-0001" = "1"), so re-scrapes and merges fold the same lot. See "Lots".
* **Near-duplicates** are never checked inline. The background job `candidates.py` flags them, and
  a person decides in `review.py`.
* **Every change of state writes an event** to `tender_event`. The table rejects UPDATE, DELETE and
  TRUNCATE with statement-level triggers, and those privileges are revoked from PUBLIC.

## Files

| File | Purpose |
|---|---|
| `schema.sql` | Base tables: tender, counters, aliases, MOD 37-2, `issue_accession`, `issue_child_no`, lots/amendments/submissions (unchanged) |
| `dedupe.sql` | Possible-duplicate table, live-hash unique index, v1 functions (unchanged; 002 replaces its functions) |
| `review.sql` | Review queue and stats views, v1 `mark_not_duplicate` (unchanged; 002 replaces it) |
| `003_source_refs.sql` | The v3 migration, safe to re-run: `tender_source_ref` with backfill, `child_accession_alias`, `resolve_child_accession()`, `merge_move_children()`, `trc_lock_key()`, v3 `find_or_create_tender` / `merge_tender`, the `deadline_changed` event type, and a catch-up move of children left on tenders merged under 002 |
| `004_change_tracking.sql` | The v4 migration, safe to re-run, one transaction: new nullable tender columns (`description`, `estimated_value`, `currency`, `cpv_codes`), `locked_fields`, `updated_at`, `scan_at`; `tender_hash_history` (backfilled from `deadline_changed` events); `tender_source_ref.origin` (primary / hash_match / direct / merge, backfilled) and `.observed`; `trc_lock_tender()`; v4 `find_or_create_tender` (also matches old hashes) and `merge_tender` (row locks); event types `fields_changed`, `field_locked`, `field_unlocked` |
| `005_platforms_lots.sql` | The v5 migration, safe to re-run, one transaction: `source_platform` registry (seeds PLC/FTS/TED/MAN; unknown codes in existing data → `proposed` with a WARNING), FKs from `tender.source` / `tender_source_ref.source`, triggers (frozen codes, retired codes never reused, new tenders need an active platform), `tender_lot.source_lot_label` / `lot_origin` / `confirmed_by` / `confirmed_at` (backfilled), `trc_canonical_lot_ref()` and canonical refs for old lots, `merge_move_children` v5 (folds on canonical refs), event type `lot_confirmed` |
| `002_ingestion.sql` | The v2 migration: pg_trgm, new columns, numbering trigger, `tender_event`, `job_state`, and new `find_or_create_tender` / `merge_tender` / `mark_not_duplicate`. You can run it more than once |
| `006_seed_platforms.sql` | The v6 migration, GENERATED from `platforms/proposals.csv` by `platforms.py seed-sql`, safe to re-run, one transaction: `source_platform.tenant_key` + format check, uniqueness by `(domain, tenant_key)`, then registers every approved platform as active without touching existing rows |
| `apply.sh` | Applies the files in order. `--fresh` drops and recreates the database first. `UPTO=002` … `005` stops early |
| `platform_clean.py` | New in v6. Dedupes a platform list: exact duplicates, same site, tenants on shared hosts, rows without URL; writes `platforms_clean.csv`, `duplicates_removed.csv`, `needs_review.csv` (also `platforms.py clean`) |
| `platforms/` | Your list: `source_list_original.csv` (byte copy of the attachment), `platforms_clean.csv`, `duplicates_removed.csv`, `needs_review.csv`, `proposals.csv` (with `approved_code` and `review_note`) |
| `accession.py` | Python MOD 37-2 and accession number make/child/parse/is_valid (unchanged) |
| `dedupe.py` | Cleaning and `dedupe_hash`, the single source of truth. `find_or_create()` is kept only as a shim over `ingest_tender` |
| `platforms.py` | Code suggester (`suggest_code`), robust CSV/XLSX reader (`read_rows`), `clean`, `suggest [--prefill]`, `seed-sql`, `apply`, `list`, `activate`, `retire`, `rename`, `update`; URL helpers (`normalise_url`, `unwrap_url`, `url_tenants`), `parse_country`; `resolve_platform()` for ingestion |
| `lots.py` | New in v5. Lot parsers: `lots_from_codice`, `lots_from_eforms`, `lots_from_ocds`, `extract_lots`; `TextLotExtractor` interface + `HeuristicTextLotExtractor` (regex, no AI model); `canonical_lot_ref` |
| `examples/` | `platforms_sample.csv` (SAMPLE list of 17 real platforms) and the `platforms_sample_proposals.csv` it produced |
| `tests/fixtures/` | SYNTHETIC sample notices (CODICE ATOM, eForms XML, OCDS JSON, document text) and a messy platform CSV; see its README |
| `changes.py` | New in v4. Change tracking: priorities (from `source_platform` since v5), field cleaners (status, description, value, currency, CPV), diff, precedence and per-field rules, hash recompute and clash handling |
| `ingest.py` | Ingestion service: `ingest_tender` (now applies re-scrape changes), `resolve_source`, `add_lot`, `ingest_lots`, `confirm_lot`, `add_amendment`, `add_submission`, `set_status`, `edit_field`, `lock_field`, `unlock_field`. CLI: `load`, `lots`, `backfill-norms`, `backfill-observed` |
| `candidates.py` | Background near-duplicate job (watermark, batches, idempotent). Run it once, with `--loop`, or from cron |
| `review.py` | Review CLI: list / stats / same / different / interactive / history (with readable field diffs) / show / `lock ID FIELD` / `unlock ID FIELD` / `edit ID FIELD VALUE` / `lots ID` / `confirm-lot LOT_ID` |
| `test_e2e.py` | End-to-end test on a fresh database, including concurrency (79 checks) |
| `test_changes.py` | Change-tracking tests (52 checks): primary change, unchanged re-scrape, old-hash match, secondary logged not applied, locks, status, amendments, hash clash, candidates re-check, 8-thread concurrency, history output |
| `test_platforms_lots.py` | v5 tests (95 checks): suggester determinism, blocklist/reserved, clashes (in file, vs DB, duplicate domain/name), CSV/XLSX reading, apply (refuses clashes, idempotent), frozen/retired rules, `resolve_platform`, ingest by URL/name, unknown/inactive rejected, priorities from the registry, parsers on every fixture, Python/SQL canonical-ref parity, `ingest_lots` idempotency, AI-lot confirm flow, merge folding "Lote 1"/"Lot 1", 8-thread first use of a new code. Safe to re-run on a used database |
| `test_upgrade.py` | Upgrade test (55 checks): builds v2, v3, v4 and a populated v5 database, fills them the old way, applies the later migrations twice, runs `backfill-observed` and checks backfills, child moves, change tracking; v4 → 005/006: registry/FKs, unknown code → proposed with a warning, lot labels/origins, nothing lost; v5 → 006: existing platforms (frozen, renamed, re-prioritised, proposed, a list code already taken) kept or activated, conflicts warned, nothing lost, re-run is a no-op |
| `test_platform_list.py` | v6 tests (56 checks): every approved code valid/unique/not reserved, seeds recognised, every platform resolves by its URL and www/deeper/port variants, tenants and parent-domain fallback; reader robustness (BOM, cp1252, UTF-16, `;`/tab, Excel `_link` columns, repeated headers, nameless rows, URLs in names, `-tid=` repair, urldefense/OAuth unwrap, country names/lists/regions, ccTLD inference); cleaner rules; `seed_sql` refusals; 006-style seeding against conflicting rows |

## Apply order

1. `schema.sql`
2. `dedupe.sql`
3. `review.sql`
4. `002_ingestion.sql`
5. `003_source_refs.sql`
6. `004_change_tracking.sql`
7. `005_platforms_lots.sql`
8. `006_seed_platforms.sql`

```bash
./apply.sh trc_v6_test --fresh      # new database: all eight files
./apply.sh trc                      # existing v1–v5 database: 002 → … → 006 (all re-runnable)
DATABASE_URL=... python ingest.py backfill-norms      # only after upgrading from v1
DATABASE_URL=... python ingest.py backfill-observed   # once after upgrading to 004 (idempotent; harmless to repeat)
DATABASE_URL=... python platforms.py list             # after 005: check the registry, see "Platforms"
```

* Upgrading to 006: no tender data is touched. Platforms already registered are never changed
  (code, name, URL, priority, frozen state); a `proposed` one that is on the list is activated. If
  your database already has a list site under another code, or a list code is taken by another
  site, psql prints a WARNING naming them; those list rows are not registered (see "Cleaning a
  platform list (v6)"). 005 can be re-run after 006 (it no longer recreates the one-per-domain index).
* Always run 002 → 006 together, in that order, which is what `apply.sh` does. An older file on
  its own would put back older functions (`find_or_create_tender`, `merge_tender`,
  `merge_move_children`) until the later ones run again.
* Upgrading to 005: no rows are rewritten except lot refs (counts stay the same). Codes already in
  the data other than PLC/FTS/TED/MAN become `proposed` platforms, with a WARNING from psql: give
  them a name (`platforms.py rename CODE --name ... --url ...`) and `platforms.py activate CODE`,
  otherwise new tenders from them are rejected. Every used code is frozen. Old lots keep their raw
  label in `source_lot_label` and get the canonical ref; if two lots of one tender canonicalise to
  the same ref (e.g. "Lote 1" and "Lot 1" left side by side by an earlier merge) the later one keeps
  its raw ref and psql prints a WARNING on every run until someone resolves it. Old lots become
  `structured`, or `manual` when they have no lot ref and no platform on their `lot_added` event.
  Deploy the v5 Python together with 005 (v4 Python would still send unregistered codes).
* Upgrading to 004: existing data is not rewritten (tender, event and ref counts stay the same).
  `backfill-observed` records what each primary platform last showed; for a tender with a deadline
  amendment it records the pre-amendment deadline, so a primary listing that still shows the old
  deadline does not undo the amendment. Deploy the v4 Python together with 004.
* Don't re-run `dedupe.sql` or `review.sql` after 002. They would bring back the v1 function
  overloads, and `merge_tender(a,b)` / `mark_not_duplicate(a,b)` would then become ambiguous.
* Upgrading from v2: 003 builds `tender_source_ref` from `tender.source/source_ref` (primary)
  and from `matched_hash` / `matched_source_ref` event payloads. Refs of merged tenders point at
  the survivor. 003 also moves children still sitting on tenders merged under 002, logged as
  `merged` events with actor `migration:003`. The whole migration is one transaction.

## Running ingestion

```python
import psycopg
from ingest import ingest_tender, add_lot, add_amendment, add_submission
conn = psycopg.connect(DATABASE_URL, autocommit=True)
r = ingest_tender(conn, {"source": "PLACSP", "source_ref": "EXP-900",   # or "source_url": "https://contrataciondelestado.es/..."
                         "title": "Suministro de reactivos", "buyer": "Hospital Clínic S.A.",
                         "deadline": "2026-11-01T12:00:00Z", "url": "https://..."}, actor="placsp-scraper")
# r.tender_id, r.accession_no, r.outcome in {"created", "matched_source_ref", "matched_hash"}
lot = add_lot(conn, r.tender_id, title="Anticuerpos", source_lot_ref="Lote 1", actor="placsp-scraper")
```

* Platform: `source` (registered code, or the platform's name / short name, e.g. `PLC`, `PLACSP`,
  `Find a Tender`), `platform` (name) and/or `source_url` (any URL on the platform; www, ports and
  subdomains are handled). They must agree. The platform must be registered and **active**; there
  is no fallback and no code is ever invented. A `source_url` on an unregistered host (a document
  server) is ignored when `source`/`platform` already identify the platform.
  Bad input raises `ingest.IngestError`.
* Each call runs in its own transaction. If the caller already has a transaction open, the call
  runs as a savepoint inside it.
* The child helpers follow a merged tender to its survivor and lock the survivor's row. A lot with
  the same `source_lot_ref`, or an amendment with the same `document_sha256`, returns
  `outcome="existing"` and uses no new number.
* A `deadline_change` amendment requires `new_deadline_at`. If another live tender already has the
  recomputed hash, the old hash is kept, and the event records `hash_conflict_with` so it can be
  reviewed. Merging those two is probably right.
* Look up any ID, old or new: `SELECT * FROM resolve_child_accession('TRC-26-FTS-000001-9/L02')`,
  `SELECT resolve_accession(...)` (gives the tender), or `python review.py show ID`.
* Optional scraped fields: `status`, `description`, `estimated_value`, `currency`, `cpv_codes`
  (list or comma-separated). A missing or null field means "not provided", never "cleared".
* `r.changed` / `r.not_applied` list the fields a re-scrape changed or only recorded.
* Batch CLI: `python ingest.py load tenders.jsonl --actor ted-scraper` (one JSON object per line);
  it prints changed / not-applied fields per line.

## Platforms (v5)

`source_platform` holds one row per platform:
* `code` (`^[A-Z]{3}$`, the code in accession numbers), `name`, `short_name`, `url`, `domain`,
  `tenant_key` (v6; NULL = the whole host, else a lower-case path prefix `/a[/b[/c]]` or a tenant
  parameter `param=value`). `(domain, tenant_key)` is unique.
* `country` (ISO 3166-1 alpha-2), `data_format` (codice / eforms / ocds / html / pdf / other)
* `priority`: who may overwrite tender fields (see Change tracking). Seeded PLC 100, FTS 100,
  TED 50, everything else 10.
* `status` (proposed / active / retired), `code_origin` (suggested / manual), `frozen_at`,
  `created_at`, `notes`

Rules, enforced by the database:
* A code is **frozen** automatically the first time a tender or a platform reference uses it.
  A frozen or used code can't be changed or deleted. The name, URL, short name, notes and
  priority can always change.
* **Retired** is final. A retired platform can't be reactivated, deleted or recoded, so its code
  is never reused. Register the platform again under a new code if it comes back.
* New tenders need an **active** platform (a trigger on `tender`; `ingest.py` checks first and gives
  a clear message).

### Registering your own list of platforms

```bash
export DATABASE_URL="dbname=trc host=/var/run/postgresql"     # optional for suggest, required for apply
python platforms.py suggest my_platforms.xlsx --out proposals.csv
#   -> prints a summary and writes proposals.csv
#   open proposals.csv; for every row that is not "ok" read clash_details and, if you agree with a
#   code (suggested_code, one of the alternatives, or your own), type it into approved_code
python platforms.py apply proposals.csv            # registers usable rows as 'proposed'
python platforms.py list                           # check
python platforms.py activate CFN                   # one by one ...
python platforms.py apply proposals.csv --activate # ... or activate everything in the file
python platforms.py update CFN --priority 50 --data-format ocds    # tune later
```

* Input (v6 reader): CSV or XLSX (first sheet; cell hyperlinks are read too).
  * Encoding: UTF-16 (BOM), UTF-8 with or without BOM, else Windows-1252, else latin-1.
  * Delimiter: the one of `,` `;` tab `|` that splits the file most consistently.
  * Header matching ignores case, spaces, `-` and `_`, then falls back to keywords:
    `name` (required; platform, platform name, "Name of Tender Platform", portal, nombre, nom),
    `url` (website, link, web address, homepage), `short_name` (abbreviation, acronym, alias),
    `country`, `importance`/`sector`, `data_format` (format), `priority`, `notes` (comment).
    Excel hyperlink exports (`URL` + `URL_link`) use the `_link` column as the URL and keep the
    link text as `url_title`.
  * Repeated header rows (a list in sections) are skipped; `source_row` stays the spreadsheet row.
  * URLs: text around a URL, `(https://…`, `… (IRELAND)`, bare hosts like `SAM.gov`, junk glued to
    the host (`org-tid=123`, flagged), Proofpoint urldefense / Safe Links / login-redirect wrappers
    (unwrapped) are handled. A URL in brackets in the name is moved out of the name.
  * A name that is a URL, or an empty name with a URL, becomes the domain (flagged for review);
    rows with neither name nor URL are skipped and reported.
  * Country names, UK/USA, alpha-3 codes → ISO alpha-2 (noted); several countries → `countries`;
    regions (EU, Europe, Global, Nordics…) and unknown values are noted, never guessed.
* Proposals CSV columns: `name, url, domain, suggested_code, alternatives, reason, status,
  clash_details`, then `approved_code` (empty: fill it to override; `--prefill` fills it with the
  chosen code), `review_note` (v6: what is worth a look) and the extra input columns
  (`short_name, country, data_format, priority, notes, source_row, tenant_key, countries,
  country_source, sector, importance, aliases, source_rows`; `source_row` is the line number in
  your file, header = 1).
* Clashes are resolved deterministically, strongest claim first: a short name, a 3-letter acronym
  or an exact domain label beat initials, which beat bracket acronyms and consonant patterns, which
  beat generated and last-resort codes; file order breaks ties. The loser gets its next alternative
  and both rows say so.
* `status`:
  * `ok`: nothing to check.
  * `clash_with_existing`: the preferred code belongs to another registered platform (even a
    retired one); an alternative was suggested.
  * `clash_in_file`: an earlier row got the preferred code; an alternative was suggested. The
    earlier row's `clash_details` mentions it too.
  * `duplicate_platform`: same domain or near-identical name as a registered platform or an
    earlier row. `suggested_code` shows the existing code, and apply skips the row unless you
    fill `approved_code`.
  * `needs_review`: the code is not based on clear initials or a known short name (padded with
    vowels, last-resort letters, no significant words), or the country/format/priority looks wrong
    (e.g. `UK`, which should be `GB`).
* `apply` registers rows with status `ok`, or any row where `approved_code` is filled; that code
  wins over `suggested_code`.
  * It refuses codes that are malformed, reserved (MAN, TRC), already used by another platform,
    or used twice in the file, and exits 1 if anything was refused.
  * It is idempotent: a platform already registered (same domain, else same name) with the same
    code is reported as `already` (and activated with `--activate`). With a different code it is
    refused.
* Without `DATABASE_URL`, `suggest` compares only against the built-in seed codes; run it again
  with the database before `apply`.

**How codes are suggested** (`suggest_code`, deterministic). First, accents are stripped,
stopwords in EN/ES/FR/DE/IT/PT and generic words (portal/platform/plataforma/sector/service/gov…)
are dropped, and camelCase is split (eTendersNI → e Tenders NI). Then, in order:
1. `short_name` if given: 3 letters as is; longer → first letter + following consonants (PLACSP → PLC).
2. A 3-letter acronym in capitals in the name (TED, SAM in "SAM.gov").
3. A 3-letter domain label (ted.europa.eu → TED, sam.gov → SAM).
4. A longer acronym in capitals → first letter + following consonants (BOAMP → BMP, ANAC → ANC).
5. Initials:
   * 3+ significant words → first three initials (Public Contracts Scotland → PCS)
   * 2 words → both initials + the next consonant (Contracts Finder → CFN)
   * 1 word → first letter + next two consonants (Mercell → MRC), padded with vowels if needed
     (BASE → BAS, *needs_review*)
6. Other letter combinations from the name (more initials, then more consonants, then earlier
   letters) as ranked alternatives.
7. Last resort: first two letters + A…Z. (Codes are letters only, `^[A-Z]{3}$` like the accession
   format, so the "+ digit" variant is not possible.)

Reserved codes (MAN, TRC), a small blocklist of awkward/offensive combinations and taken codes
are skipped. The `reason` column says which rule won and what was skipped.

Sample: `examples/platforms_sample.csv` lists 17 real platforms (labelled SAMPLE ROW). Running
`suggest` on it against a fresh database gives `examples/platforms_sample_proposals.csv`:
* 11 ok
* 4 duplicate_platform:
  * PLACSP: near-identical name
  * Find a Tender and TED: same domain as the seeds
  * a deliberate Contracts Finder duplicate: same domain as an earlier row
* 1 clash_in_file: Bund.de wanted BAS, which Portal BASE claims through its short name BASE
  (stronger claim since v6) → BNA
* 1 needs_review: Portal BASE → BAS is padded, not clear initials

## Cleaning a platform list (v6)

Your list (`platforms/source_list_original.csv`, a byte-for-byte copy of the attachment) went
through four steps. Every file is in `platforms/`.

```bash
python platforms.py clean platforms/source_list_original.csv --out-dir platforms     # 1. dedupe
DATABASE_URL=... python platforms.py suggest platforms/platforms_clean.csv \
       --out platforms/proposals.csv --prefill                                         # 2. codes
#   3. review proposals.csv: approved_code is pre-filled; read review_note, change codes you dislike,
#      clear approved_code for a row you do not want registered
python platforms.py seed-sql platforms/proposals.csv --out 006_seed_platforms.sql      # 4a. migration
# 4b. or directly, on a 005 database (the schema part of 006 first, for tenants):
python platforms.py seed-sql --schema-only --out 006_schema.sql && psql -d trc -f 006_schema.sql
DATABASE_URL=... python platforms.py apply platforms/proposals.csv --activate
```
Both paths give the same registry (checked: code, domain, tenant, status, priority, country,
format identical), and 006 run after `apply` changes nothing.

**1. Dedupe** (`platform_clean.py`; the rules are in its docstring):
* Exact duplicate rows (case and spacing ignored) → `duplicates_removed.csv`, kind `exact_duplicate`.
* Rows on the same host are one platform when their URL is site-level (no meaningful path) or the
  name is the host; a path or query parameter that identifies a customer on a shared host
  (`in-tendhost.co.uk/universityofleeds`, `planetbids.com/…?CompanyID=42510`, `…?CustOrg=FSU`)
  becomes its own platform with a `tenant_key`. If a host has only one path-level entry, it is the site.
* The kept row is the most complete one (URL, country, importance …); the simplest URL is kept,
  other names become `aliases`, and country/sector/importance are merged when they don't conflict
  (conflicts are noted). Each removed row says which row it went into and why (`same_domain`,
  `same_site_different_name`, which is also flagged, `same_name_no_url`).
* A row with no URL folds into the one platform of the same name; otherwise it is held back.
  Name-only rows (section headings) are held back.
* Never merged, only flagged in `needs_review.csv`: the same or a near-identical name on different
  hosts, and different names at the same address (merged but flagged).
* Country: from your list (names converted to ISO codes). If the list has none, it is inferred
  from a country-code domain (`.de`, `.co.uk` …), with `country_source = inferred_cctld` and a
  note. Generic domains (`.com`, `.eu`, `.io` …) never give a country.

**Priority rule** (written into each row's notes):
| Priority | Who |
|---|---|
| 100 | official national portal with a structured feed already parsed: PLC (CODICE), FTS (OCDS) |
| 50 | TED keeps its 005 value: it republishes national notices, so the national source wins |
| 70 | other official portals: sector "Public" in your list, a government domain (`gov`, `gob`, `gouv`, `europa.eu`, `.state.xx.us` …) or a government body in the name |
| 30 | private platforms, aggregators, marketplaces: sector "Private" (incl. Private Hospital / Lab Network) |
| 50 | not classified: the list says neither (review these) |
| 10 | MAN (manual entry), unchanged |

**006 is safe on a database that already has some of these platforms.** For each list row:
same `(domain, tenant_key)` already registered under the same code → nothing (a `proposed` one is
activated); under a different code → kept as it is, listed in a WARNING (`listcode=dbcode`);
the code is taken by another site → that row is not registered (WARNING, choose another code and
`apply` it); otherwise inserted as active. Existing rows are never updated, so frozen codes,
renames and priority changes survive. Don't edit 006 by hand: change `proposals.csv` and run
`seed-sql` again. `platforms.py apply proposals.csv --activate` is the alternative path and uses
the same rules (tenant-aware; refuses taken or malformed codes).

**Adding platforms later**
1. Put the new rows in a CSV (same columns as your list, or just name + URL).
2. `python platforms.py clean new.csv --out-dir new/` (optional for a few rows).
3. `DATABASE_URL=<live db> python platforms.py suggest new/platforms_clean.csv --out new/proposals.csv --prefill`.
   Running it against the live database means codes and sites already registered are recognised
   (`duplicate_platform`) and taken codes are avoided.
4. Review, then `python platforms.py apply new/proposals.csv --activate`.
5. A customer on a shared host: give the URL with its path/parameter; `suggest` keeps the tenant,
   or `python platforms.py update CODE --tenant-key /customer`.
Regenerate 006 only if you want a fresh database to include them; never re-number frozen codes.

## Lots (v5)

```python
import lots
from ingest import ingest_lots, confirm_lot
found = lots.lots_from_eforms(xml_bytes)          # or lots_from_codice(atom, folder_id=...),
                                                  # lots_from_ocds(json), extract_lots(data, fmt)
res = ingest_lots(conn, tender_id, found, "ted-scraper", source="TED")     # idempotent
text_lots = lots.HeuristicTextLotExtractor().extract(document_text)        # origin 'ai_extracted'
ingest_lots(conn, tender_id, text_lots, "doc-extractor", extractor="heuristic-regex-v1")
```

```bash
python ingest.py lots TRC-26-PLC-000001-L notice.xml --format eforms --actor ted-scraper
python ingest.py lots TRC-26-PLC-000001-L pliego.txt --format text --actor doc-extractor
python review.py lots TRC-26-PLC-000001-L            # origin, confirmation state, value, CPV
python review.py confirm-lot TRC-26-PLC-000001-L/L03 # confirm an AI-extracted lot (lot_confirmed event)
```

* Every parser returns the same dicts: `source_lot_ref` (canonical), `source_lot_label` (raw),
  `title`, `description`, `cpv_codes`, `estimated_value`, `currency`, `origin`. Empty, missing or
  unparseable input → `[]`. Namespace-aware stdlib ElementTree, no lxml needed.
  * CODICE (PLACSP ATOM):
    * `cac:ProcurementProjectLot` → `cbc:ID`, `cac:ProcurementProject/cbc:Name` (+ `cbc:Description`)
    * budget from `TaxExclusiveAmount`, else `EstimatedOverallContractAmount`, else `TotalAmount`
      (`currencyID`)
    * CPV from `RequiredCommodityClassification`
    * a feed with several tenders needs `folder_id` (`cbc:ContractFolderID`)
  * eForms (TED):
    * only `cbc:ID schemeName="Lot"` (LOT-0001); groups (`LotsGroup`) and parts are skipped
    * name/description in English when present, else the first language
    * value from `RequestedTenderTotal/EstimatedOverallContractAmount`
    * CPV from Main + Additional `CommodityClassification`
  * OCDS (Find a Tender):
    * the last release that has `tender.lots`, or a record package's `compiledRelease`
    * CPV from `tender.items[]` with `relatedLot`, including `additionalClassifications` with
      scheme CPV
  * Text: `HeuristicTextLotExtractor` (regex heuristics, **not** an AI model).
    * It matches line-start headings "Lote 1:", "Lot 2 -", "Lot nº 3", "LOT 4 -", "Lotto", "Los",
      including bulleted lists after "divided into the following lots" / "dividido en los
      siguientes lotes".
    * Title = the heading's first sentence; description = the following indented lines; a value
      such as "120.000,00 EUR" or "£80,000" is picked up.
    * An LLM extractor can later implement `TextLotExtractor.extract(text)` with the same output;
      nothing calls a model today.
* **Canonical lot refs**: accents and punctuation are ignored. A leading lot word (lot/lote/lots/
  lotes/lotto/lotti/los/lose/partida) and a number marker (nº, no, nr, núm, number) are dropped.
  What's left gives the ref:
  * a number, without leading zeros (plus a one-letter suffix such as 1A)
  * else, after a lot word, its first token ("Lot A - Hardware" → A)
  * else the whole label

  So "Lote 1", "Lot 1", "LOT-0001", "Lot nº 1" and "1" all → "1". `tender_lot.source_lot_ref`
  holds the canonical ref; `source_lot_label` holds the raw label. `trc_canonical_lot_ref()` is
  the SQL twin, used by the migration backfill and merge folding; the tests check that both give
  the same results.
* `ingest_lots` runs in one transaction. Lots already present (same canonical ref; for lots
  without a ref, the same cleaned title) come back as `existing` and use no new number. The same
  lot twice in one notice is added once.
* `lot_origin`:
  * `structured`: from a platform's data.
  * `ai_extracted`: from text; needs a person. `confirm-lot` sets `confirmed_by` /
    `confirmed_at` and logs `lot_confirmed`.
  * `manual`: `add_lot` without a platform or lot ref.

  When the platform later publishes a structured lot with the same canonical ref, it confirms the
  AI guess: origin → structured, empty fields filled, and a `lot_confirmed` event with
  `via: structured_source`.
* **Merges** fold lots whose canonical refs are equal, so "Lote 1" on PLACSP and "Lot 1" on TED
  become one lot (this fixes the v3/v4 caveat). The survivor's lot keeps its own values, gaps are
  filled from the folded one, and a structured lot beats an AI-extracted one.

## Change tracking (v4)

Tracked fields: `title`, `buyer`, `deadline_at`, `status`, `description`, `estimated_value`,
`currency`, `cpv_codes`. On every re-scrape that matches (`matched_source_ref` or `matched_hash`),
inside the same transaction, under the same advisory locks and a `FOR UPDATE` lock on the tender row:

1. **Diff** the scraped values against the stored ones (cosmetic title/buyer differences that
   dedupe ignores are not logged unless applied).
2. **Decide per field** whether it may be written:
   * *Source precedence* (`source_platform.priority` since v5: PLC 100, FTS 100, TED 50, others 10;
     change with `platforms.py update CODE --priority N`; the event records the priorities used). The
     tender's primary source (`tender.source`) or a platform with equal/higher priority may
     overwrite; lower priority → `not_applied` (`lower_priority`).
   * *Platform must have changed it*: a field is only overwritten when that platform's value
     differs from what it showed last time (`tender_source_ref.observed`). Re-asserting an old
     value → `unchanged_on_platform`. This stops a stale listing undoing an amendment or a manual
     edit, and two equal-priority platforms flip-flopping. With no previous observation the
     primary may write; a secondary only records (`first_seen_on_secondary`).
   * *Manual locks* (`tender.locked_fields`) are never overwritten (`locked`).
   * *Stale copies* (matched through a retired hash, or a merged duplicate's hash) and refs that
     came in through a merge (`origin='merge'`) never overwrite (`stale_copy`, `merged_duplicate_ref`).
   * *Status*: only platform statuses (open, closed, suspended, cancelled, awarded, plus aliases
     like canceled/active). A platform saying "open" never overwrites our own workflow state
     (in_bid, withdrawn, ...); cancelled/awarded does, unless locked.
3. **Hash**: Python recomputes `dedupe_hash`, `title_norm`, `buyer_norm`. The replaced hash goes to
   `tender_hash_history`, and `find_or_create_tender` also matches on it (`matched_hash` payload
   `via: current | merged | history`), so a new platform still showing the old title/deadline
   finds the tender. If the new hash belongs to another live tender, the old hash is kept, the
   other changes are still applied, the event records `hash: {reason: "conflict", conflict_with}`
   and the pair goes to `tender_possible_duplicate` ("hash clash after re-scrape") for review.
4. **Event**: one `fields_changed` per re-scrape that changes or newly notices anything:
   `{match, source_ref, precedence, changes: {field: {old, new}}, not_applied: {field: {old, new, reason}}, hash?}`,
   with the scraper as actor. A standing difference that is not applied is reported once, not on
   every scrape. **No event** when nothing changed: only `last_seen_at` moves (v3 wrote a
   `matched_source_ref` event for every known-ref re-scrape; v4 writes it only when a ref is
   re-pointed after a merge).
5. `scan_at` is bumped when title/buyer/deadline change, so `candidates.py` re-checks the tender.

Manual control (`review.py`, or `ingest.edit_field/lock_field/unlock_field`):

```bash
python review.py lock TRC-26-PLC-000001-L deadline_at     # field_locked event
python review.py unlock TRC-26-PLC-000001-L deadline_at   # field_unlocked; primary's next scrape may write it again
python review.py edit TRC-26-PLC-000001-L title "New title"   # fields_changed (match=manual) + locks the field
python review.py history TRC-26-PLC-000001-L              # events with "field: old → new" diff lines
```

A deadline amendment goes through the same code: it writes hash history and respects a locked
`deadline_at` (logged as not applied).

**Concurrency.** Every write path locks the tender row first (`trc_lock_tender`, follows merges),
then references/hashes by advisory lock, so there is one lock order everywhere. Write helpers
retry a deadlock or serialization failure up to 3 times when they own the transaction (a safety
net; the stress test sees no deadlocks even with retries turned off).

## Running the duplicate-candidate job

```bash
python candidates.py                          # one pass from the watermark in job_state
python candidates.py --since 2026-01-01       # rescan (the watermark only moves forward)
python candidates.py --threshold 0.55 --days 10 --batch 500
python candidates.py --loop --interval 300    # long-running
# or cron, every 5 minutes:
*/5 * * * * cd /path/to/v2 && DATABASE_URL=... /path/to/.venv/bin/python candidates.py >> candidates.log 2>&1
```

* A pair is flagged when both tenders are live, `buyer_norm` is equal, the deadlines are no more than
  `--days` apart, and `similarity(title_norm, title_norm)` is at least `--threshold`.
* Pairs are stored newer → older. A pair that already exists in either direction is skipped,
  including pairs a reviewer has already marked `different` or `same`.
* Each pass rescans 10 minutes behind the watermark (`--overlap-minutes`), so a transaction that
  commits late is not missed. Skipping existing pairs makes this safe to repeat.
* Only one instance runs at a time (session advisory lock). A second copy exits with code 1.

## Testing

```bash
./apply.sh trc_v6_test --fresh
export DATABASE_URL="dbname=trc_v6_test host=/var/run/postgresql"
python test_e2e.py                 # needs a fresh database
python test_changes.py             # fresh or used database
python test_platforms_lots.py      # fresh or used database
python test_platform_list.py       # needs 006 applied; fresh or used database
python test_upgrade.py trc_upgrade_test       # drops and recreates trc_upgrade_test_v2 / _v3 / _v4 / _v5
```

## Snowflake migration notes

**Moves across as it is:** all the Python (`dedupe.py`, `accession.py`, validation in `ingest.py`,
the candidates job's structure and watermark in `job_state`, all of `changes.py`, and all of
`platforms.py` and `lots.py`: the suggester, CSV/XLSX handling and parsers are plain Python with
stdlib ElementTree/json, and `resolve_platform` is plain SELECTs). The table shapes also move, with
these type changes: `uuid` → `VARCHAR(36)` generated in Python (`uuid4()`), `jsonb` → `VARIANT`,
`text[]` → `ARRAY`, `timestamptz` → `TIMESTAMP_TZ`. The views are plain SQL (swap `width_bucket`
for its equivalent and check `round`/`extract` syntax).

**Must be rewritten:**

* **Numbering trigger → Python.** Snowflake has no row triggers. `ingest_tender` should issue the
  number itself before inserting: an `UPDATE accession_counter SET last_no = last_no + 1 ... ` plus
  a read inside one transaction (or a `SEQUENCE` per yy/source, if gaps are acceptable), then
  `accession.make()`. Child numbers (`issue_child_no`) move the same way.
* **`tender_source_ref` and `child_accession_alias`** are plain tables and move as they are. The
  `merge_move_children` loop becomes Python: issue the numbers, run `UPDATE`s in one transaction.
* **Deferrable FKs** (used while children move) don't matter there: Snowflake doesn't enforce FKs.
* **Advisory locks → single writer.** Snowflake has no `pg_advisory_xact_lock`. Run ingestion
  through one writer (a queue consumer, or one task per source), or stage rows and resolve them in
  one `MERGE` per batch keyed on `(source, source_ref)` and then on `dedupe_hash`. Snowflake also
  doesn't enforce UNIQUE constraints, so the single writer is what keeps data correct. The same
  applies to v4: `trc_lock_tender` / `FOR UPDATE` / `pg_try_advisory_xact_lock` in
  `apply_changes` disappear, and the single writer serialises re-scrapes of one tender.
* **plpgsql functions → Python** (or Snowflake Scripting procedures): `find_or_create_tender`,
  `merge_tender`, `merge_move_children`, `mark_not_duplicate`, `resolve_accession` (the two
  resolve functions are plain SQL and nearly portable as they are).
* **pg_trgm → `JAROWINKLER_SIMILARITY(a, b) / 100.0` or `EDITDISTANCE`.** The scores differ from
  trigram similarity, so retune `--threshold` using `duplicate_review_stats`. There is no exact
  equivalent: start around 0.85–0.9 Jaro-Winkler and tune from real review decisions.
* **Append-only `tender_event`:** grant only INSERT/SELECT to the app role (there are no triggers).
  Time Travel covers accidents.
* **v5 registry rules → the writer.** Snowflake has no triggers and doesn't enforce FK/UNIQUE, so
  the `source_platform_guard` / `trc_platform_use` rules (frozen codes, retired is final, active
  platform for new tenders, freeze on first use, unique domain) move into `platforms.py` and the
  single ingestion writer. Today `ingest.py` already checks "active" before inserting. Keep
  `source_platform` a small table that only `platforms.py` writes. The v6 `(domain, tenant_key)`
  uniqueness and the tenant format check are Postgres-only too: `platforms.py apply` /
  `update` already check both in Python; generate the Snowflake seed with a `MERGE` on
  `(domain, coalesce(tenant_key,''))` and the same "never update existing rows" rule.
* **`trc_canonical_lot_ref()`** is only needed in SQL for the Postgres backfill and merge folding.
  In Snowflake, merge folding is Python (see `merge_move_children` above) and calls
  `lots.canonical_lot_ref` directly.
* **CHECK constraints and regex checks** aren't enforced in Snowflake. Validate in Python, which
  `ingest.py` already does, and check `accession.is_valid` before writing.

## Known limitations

* Direct `INSERT`s into `tender` (not via `ingest_tender`) get a number, but no event, no
  `title_norm`/`buyer_norm`, no `tender_source_ref` row and no change tracking. The ref row is
  added the first time that reference is ingested, and `ingest.py backfill-norms` fills the norms.
* Lots fold on merge and re-scrape by **canonical** ref ("Lote 1" = "Lot 1" = "LOT-0001" = "1").
  Labels that mean the same but differ in form are not equated: roman numerals ("Lot II"), words
  ("Lote único", "Lot one"), or a platform that numbers lots differently from another. Two lots
  that an earlier merge left side by side on one tender ("Lote 1" + "Lot 1") are reported by 005
  but not merged; there is no lot-merge command yet. Submissions are never folded.
* Lot details are written when a lot is first added. A re-scrape returns `existing` and doesn't
  update a lot's title/value/CPV (only an AI-extracted lot gets gaps filled when the structured lot
  arrives). There is no reject/delete for a wrong AI-extracted lot yet; leave it unconfirmed.
* The text extractor is heuristic: it needs lot headings at the start of a line, picks the first
  amount with a currency in the lot's text, and misses lots described only in tables or running
  prose. Plug in a better `TextLotExtractor` later; its lots still need confirmation.
* CODICE budgets: lots without `TaxExclusiveAmount` fall back to `EstimatedOverallContractAmount`
  (which may include extensions) and then `TotalAmount` (incl. VAT). The parser doesn't say which
  one it used.
* One domain per platform. A platform that moved domain (PLACSP: contrataciondelestado.es →
  contrataciondelsectorpublico.gob.es) resolves by URL only for the registered one. Rename the
  URL, or send `source` as well. A `duplicate_platform` by name still catches it in `suggest`.
* Tenants resolve only with their path or parameter (`in-tendhost.co.uk/universityofleeds/…`,
  `…?CompanyID=42510`). A URL on a shared host without it goes to the host's site-level platform,
  or a parent domain's (`app02.jaggaer.com` → `jaggaer.com`), or is refused when there is none
  (`in-tendhost.co.uk`, `planetbids.com`, `ungm.org`). Watch the parent fallback: your list has
  "State of Iowa (JAGGAER)" on plain `sciquest.com`, so an unknown customer on `bids.sciquest.com`
  resolves to it. Register that customer, or send `source` as well.
* `resolve_platform` by URL picks the most specific registered domain, down to two labels. If you
  register a broad domain (e.g. `gov.uk`), every unregistered gov.uk subdomain resolves to it.
* The suggester can be fooled by a capitalised vendor or brand in the name ("SAP Ariba" → SAP when
  no short name is given). Give a `short_name` to steer it. Always review the proposals.
* MAN (manual entry) has priority 10 like any other unlisted platform, so a TED copy (50) can
  overwrite a manually entered tender's fields unless they are locked. `edit_field` locks by
  default.
* Platform changes (rename/activate/retire) are not written to `tender_event` (that log is per
  tender); `source_platform.updated_at` and `notes` are the only trail.
* A secondary platform (e.g. TED for a PLACSP tender) never changes fields, including a
  cancellation seen only on TED: it is logged in `not_applied` for a person to act on
  (`set_status` / `edit`). Raise that platform's priority (`platforms.py update`) if that is too strict.
* The first value a secondary platform shows is never applied; only later changes on that
  platform can be (and only if its priority allows).
* A scrape cannot clear a field: missing/null means "not provided". Clear by `edit` if needed.
* Scrapes without a `source_ref` match by hash only, always log `matched_hash`, and use the last
  `fields_changed` event from that platform to avoid repeating the same not-applied difference.
* `ingest.py backfill-observed` must be run once after upgrading to 004. Without it, the first
  primary re-scrape of each old tender is treated as "no previous observation" and may overwrite
  (e.g. revert an amendment deadline).
* A hash clash keeps the old hash; the two tenders stay separate until someone merges them in
  `review.py`. If the clash reason was `lock_busy` (another transaction was writing that hash at
  the same moment) the next re-scrape tries again.
* A matched ref whose tender is merged is re-pointed lazily, on its next match. `merge_tender`
  moves all of them anyway, so this only covers rows written some other way.
* The append-only triggers on `tender_event` can be disabled by a superuser. Run the app as a
  non-superuser role.
* Each candidates-job pass rescans the last 10 minutes. Harmless (nothing is re-flagged), but
  the "scanned" count includes those rows.
* Rare deadlocks between unrelated write paths are retried (3 attempts) only when the helper owns
  the transaction; inside a caller's transaction the error goes to the caller.
