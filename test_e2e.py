"""End-to-end test for ingestion (002 + 003). Run on a FRESH database built by apply.sh:
    ./apply.sh trc_v3_test --fresh
    DATABASE_URL="dbname=trc_v3_test host=/var/run/postgresql" python test_e2e.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accession
import candidates
import dedupe
from ingest import IngestError, add_amendment, add_lot, add_submission, ingest_tender, set_status

DSN = os.environ.get("DATABASE_URL", "dbname=trc_v3_test host=/var/run/postgresql")
PASSED: list[str] = []


def ok(cond, msg):
    if not cond:
        print(f"FAIL  {msg}")
        raise SystemExit(1)
    PASSED.append(msg)
    print(f"PASS  {msg}")


def q(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def events(conn, tid):
    return [r[0] for r in q(conn, "SELECT event_type FROM tender_event WHERE tender_id = %s ORDER BY id", tid)]


def expect_error(conn, sql, exc, msg):
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql)
    except exc as e:
        ok(True, f"{msg} -> {type(e).__name__}: {str(e).splitlines()[0]}")
        return
    ok(False, msg + " (no error raised)")


conn = psycopg.connect(DSN, autocommit=True)
ok(q(conn, "SELECT count(*) FROM tender")[0][0] == 0, "fresh database has no tenders")

d = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
BUYER = "Hospital Clínic S.A."

# --- validation ---------------------------------------------------------------
for bad, why in [({"source": "PLACSP", "title": "  "}, "blank title"),
                 ({"source": "Nowhere", "title": "x"}, "unknown source"),
                 ({"source": "TED", "title": "Kits", "deadline": "next tuesday"}, "bad deadline")]:
    try:
        ingest_tender(conn, bad, "test")
        ok(False, f"rejects {why}")
    except IngestError as e:
        ok(True, f"rejects {why}: {e}")

# --- 1. new tender, numbered by the trigger ------------------------------------
r1 = ingest_tender(conn, {"source": "PLACSP", "source_ref": " exp-900 ", "title": "Suministro de Reactivos de Laboratorio",
                          "buyer": BUYER, "deadline": d, "url": "https://contrataciondelestado.es/x"}, "placsp-scraper")
ok(r1.outcome == "created", f"first ingest created {r1.accession_no}")
ok(accession.is_valid(r1.accession_no) and r1.accession_no.startswith("TRC-26-PLC-"),
   "trigger issued a valid PLC accession number")
row = q(conn, "SELECT source_ref, title_norm, buyer_norm FROM tender WHERE id = %s", r1.tender_id)[0]
ok(row == ("EXP-900", "suministro reactivos laboratorio", "hospital clinic"),
   f"Python-cleaned fields stored: {row}")

# --- 2. same source_ref ---------------------------------------------------------
n_ev = len(events(conn, r1.tender_id))
r2 = ingest_tender(conn, {"source": "PLC", "source_ref": " exp-900", "title": " Suministro de  Reactivos de Laboratorio ",
                          "buyer": BUYER, "deadline": d}, "placsp-scraper")
ok(r2.outcome == "matched_source_ref" and r2.tender_id == r1.tender_id and not r2.changed,
   "same source_ref -> matched_source_ref, nothing changed")
ok(len(events(conn, r1.tender_id)) == n_ev, "unchanged re-scrape of a known ref writes no event (v4)")

# --- 3. TED copy, same hash: its TED reference is now remembered ----------------------
r3 = ingest_tender(conn, {"source": "TED", "source_ref": "TED-555", "title": "SUMINISTRO REACTIVOS LABORATORIO!",
                          "buyer": "hospital clinic", "deadline": (d + timedelta(hours=3)).isoformat()}, "ted-scraper")
ok(r3.outcome == "matched_hash" and r3.tender_id == r1.tender_id, "TED copy -> matched_hash")
refs = q(conn, "SELECT source, source_ref, is_primary FROM tender_source_ref WHERE tender_id=%s ORDER BY is_primary DESC", r1.tender_id)
ok(refs == [("PLC", "EXP-900", True), ("TED", "TED-555", False)], f"tender_source_ref has both platforms: {refs}")
r3b = ingest_tender(conn, {"source": "TED", "source_ref": "TED-555", "title": "Suministro de reactivos (texto corregido en TED)",
                           "buyer": "Hospital Clinic", "deadline": d + timedelta(days=5)}, "ted-scraper")
ok(r3b.outcome == "matched_source_ref" and r3b.tender_id == r1.tender_id,
   "TED re-scrape with changed title and deadline still matches via tender_source_ref")
ok({"title", "deadline_at"} <= set(r3b.not_applied) and not r3b.changed,
   "TED (lower priority) differences recorded, not applied")
first, last = q(conn, "SELECT first_seen_at, last_seen_at FROM tender_source_ref WHERE source='TED' AND source_ref='TED-555'")[0]
ok(last > first, "last_seen_at moved forward on the match")
ok(q(conn, "SELECT count(*) FROM tender WHERE title_norm LIKE 'suministro reactivos%%'")[0][0] == 1,
   "still one tender for the PLACSP/TED pair")

# --- 4. near duplicate: different hash, so created -------------------------------
r4 = ingest_tender(conn, {"source": "Find a Tender", "source_ref": "FTS-77",
                          "title": "Suministro de Reactivos de Laboratorio 2027",
                          "buyer": "HOSPITAL CLINIC SL", "deadline": d + timedelta(days=2)}, "fts-scraper")
ok(r4.outcome == "created" and r4.tender_id != r1.tender_id and "-FTS-" in r4.accession_no,
   f"near-duplicate created as {r4.accession_no}")
ok(q(conn, "SELECT count(*) FROM tender_possible_duplicate")[0][0] == 0,
   "find_or_create_tender did no similarity flagging")

# controls that must NOT be flagged
far = ingest_tender(conn, {"source": "PLC", "source_ref": "EXP-901", "title": "Suministro de Reactivos de Laboratorio",
                           "buyer": BUYER, "deadline": d + timedelta(days=30)}, "placsp-scraper")
other = ingest_tender(conn, {"source": "PLC", "source_ref": "EXP-902", "title": "Mantenimiento de ascensores",
                             "buyer": BUYER, "deadline": d}, "placsp-scraper")
ok(far.created and other.created, "control tenders created (deadline 30 days away; unrelated title)")

# a second near-duplicate pair that a reviewer will mark different
b1 = ingest_tender(conn, {"source": "FTS", "source_ref": "F-1", "title": "Flow cytometry analyser",
                          "buyer": "Cambridge University Hospitals NHS Trust", "deadline": d}, "fts-scraper")
b2 = ingest_tender(conn, {"source": "TED", "source_ref": "T-9", "title": "Flow cytometry analysers",
                          "buyer": "Cambridge University Hospitals", "deadline": d + timedelta(days=1)}, "ted-scraper")
ok(b1.created and b2.created, "second near-duplicate pair created")

# --- 5. candidates job ----------------------------------------------------------
s1 = candidates.run_once(conn)
pairs = {(a, b) for a, b in q(conn, "SELECT tender_id, candidate_id FROM tender_possible_duplicate")}
ok((r4.tender_id, r1.tender_id) in pairs, f"job flagged near-duplicate pair (newer -> older); stats {s1}")
ok((b2.tender_id, b1.tender_id) in pairs, "job flagged flow-cytometry pair")
ok(len(pairs) == 2, f"job flagged exactly 2 pairs, not the controls (got {len(pairs)})")
sim = q(conn, "SELECT similarity FROM tender_possible_duplicate WHERE tender_id=%s", r4.tender_id)[0][0]
ok(sim >= 0.6, f"stored similarity {sim:.2f} >= 0.6")
wm = q(conn, "SELECT watermark FROM job_state WHERE job_name = %s", candidates.JOB_NAME)[0][0]
ok(wm is not None, f"watermark stored: {wm}")

s2 = candidates.run_once(conn)
ok(s2["flagged"] == 0 and q(conn, "SELECT count(*) FROM tender_possible_duplicate")[0][0] == 2,
   f"rerun from watermark adds nothing (scanned {s2['scanned']} in overlap window, flagged {s2['flagged']})")
s3 = candidates.run_once(conn, since=datetime(2000, 1, 1, tzinfo=timezone.utc))
ok(s3["flagged"] == 0 and s3["scanned"] == 6, f"full rescan --since 2000 adds nothing (scanned {s3['scanned']})")
out = subprocess.run([sys.executable, "candidates.py", "--threshold", "0.6", "--days", "7"],
                     capture_output=True, text=True, env={**os.environ, "DATABASE_URL": DSN})
ok(out.returncode == 0 and "flagged 0 new pair(s)" in out.stdout, f"CLI run: {out.stdout.strip()}")

# --- 6. review.py: list, then mark the flow-cytometry pair different --------------
env = {**os.environ, "DATABASE_URL": DSN, "TRC_ACTOR": "review:blaise"}
out = subprocess.run([sys.executable, "review.py", "list"], capture_output=True, text=True, env=env)
print(out.stdout)
ok(out.returncode == 0 and "2 pair(s) waiting for review." in out.stdout, "review.py list shows 2 pairs")
out = subprocess.run([sys.executable, "review.py", "different", b2.accession_no, b1.accession_no],
                     capture_output=True, text=True, env=env)
ok(out.returncode == 0, f"review.py different: {out.stdout.strip() or out.stderr.strip()}")
ok(events(conn, b1.tender_id)[-1] == "marked_different" and events(conn, b2.tender_id)[-1] == "marked_different",
   "marked_different logged on both tenders")
candidates.run_once(conn, since=datetime(2000, 1, 1, tzinfo=timezone.utc))
ok(q(conn, "SELECT resolution FROM tender_possible_duplicate WHERE tender_id=%s", b2.tender_id) == [("different",)],
   "rescan does not re-open a pair marked different")

# --- 7. merge: refs and children follow the survivor ----------------------------------
s_lot1 = add_lot(conn, r1.tender_id, title="Anticuerpos", source_lot_ref="Lote 1", actor="placsp-scraper", source="PLC")
d_lot1 = add_lot(conn, r4.tender_id, title="Anticuerpos", source_lot_ref="Lote 1", actor="fts-scraper", source="FTS")
d_lot9 = add_lot(conn, r4.tender_id, title="Reactivos varios", source_lot_ref="Lote 9", actor="fts-scraper", source="FTS")
d_am = add_amendment(conn, r4.tender_id, kind="clarification", lot_id=d_lot9.id, document_sha256="doc-x",
                     summary="Q&A 1", actor="fts-scraper", source="FTS")
d_sub1 = add_submission(conn, r4.tender_id, lot_id=d_lot1.id, opco="Abcam", actor="user:blaise")
d_sub2 = add_submission(conn, r4.tender_id, lot_id=d_lot9.id, opco="Abcam", actor="user:blaise")
ok([x.accession_no for x in (d_lot1, d_lot9, d_am, d_sub1, d_sub2)] ==
   [r4.accession_no + sfx for sfx in ("/L01", "/L02", "/A01", "/S01", "/S02")], "duplicate has its own children before merge")

with conn.cursor() as cur:
    cur.execute("SELECT merge_tender(%s, %s, %s)", (r4.tender_id, r1.tender_id, "review:blaise"))
ok(q(conn, "SELECT merged_into, status FROM tender WHERE id=%s", r4.tender_id)[0] == (r1.tender_id, "merged"),
   "merge_tender marked the duplicate merged")
ok(q(conn, "SELECT resolution FROM tender_possible_duplicate WHERE tender_id=%s", r4.tender_id) == [("same",)],
   "pair resolved as same")
ok(q(conn, "SELECT tender_id, is_primary FROM tender_source_ref WHERE source='FTS' AND source_ref='FTS-77'")
   == [(r1.tender_id, False)], "duplicate's platform ref moved to survivor as non-primary")
ok(q(conn, "SELECT source_ref FROM tender_source_ref WHERE tender_id=%s AND is_primary", r1.tender_id) == [("EXP-900",)],
   "survivor keeps its own primary ref")
ok(q(conn, "SELECT (SELECT count(*) FROM tender_lot WHERE tender_id=%s) + (SELECT count(*) FROM tender_amendment WHERE tender_id=%s)"
           " + (SELECT count(*) FROM tender_submission WHERE tender_id=%s)", r4.tender_id, r4.tender_id, r4.tender_id)[0][0] == 0,
   "duplicate has no children left")


def child(acc):
    rows = q(conn, "SELECT kind, child_id, tender_id, accession_no FROM resolve_child_accession(%s)", acc)
    return rows[0] if rows else None


c = child(d_lot9.accession_no)
ok(c == ("L", d_lot9.id, r1.tender_id, r1.accession_no + "/L02"), f"moved lot {d_lot9.accession_no} -> {c and c[3]}")
c = child(d_lot1.accession_no)
ok(c == ("L", s_lot1.id, r1.tender_id, s_lot1.accession_no) and
   not q(conn, "SELECT 1 FROM tender_lot WHERE id=%s", d_lot1.id),
   f"same-ref lot {d_lot1.accession_no} folded into survivor's {s_lot1.accession_no}")
c = child(d_am.accession_no)
ok(c == ("A", d_am.id, r1.tender_id, r1.accession_no + "/A01"), f"moved amendment -> {c and c[3]}")
subs = q(conn, "SELECT accession_no, lot_id FROM tender_submission WHERE tender_id=%s ORDER BY submission_no", r1.tender_id)
ok(subs == [(r1.accession_no + "/S01", s_lot1.id), (r1.accession_no + "/S02", d_lot9.id)],
   "submissions renumbered S01/S02 on survivor; the one on the folded lot now points at survivor's lot")
ok(child(d_sub2.accession_no)[3] == r1.accession_no + "/S02" and child(d_sub1.accession_no.lower())[3] == r1.accession_no + "/S01",
   "old submission IDs resolve (case-insensitive)")
ok(child(r1.accession_no + "/L01")[1] == s_lot1.id, "current child IDs resolve to themselves")
ok(q(conn, "SELECT resolve_accession(%s)", d_sub1.accession_no)[0][0] == r1.tender_id,
   "resolve_accession on an old child ID gives the survivor tender")
ok(q(conn, "SELECT last_lot_no, last_amendment_no, last_submission_no FROM tender WHERE id=%s", r1.tender_id)[0] == (2, 1, 2),
   "survivor counters advanced")
mv = q(conn, "SELECT payload->'children_moved' FROM tender_event WHERE tender_id=%s AND event_type='merged'", r1.tender_id)[0][0]
ok(len(mv["lots"]) == 1 and len(mv["folded"]) == 1 and len(mv["amendments"]) == 1 and len(mv["submissions"]) == 2
   and mv["folded"][0]["row"]["title"] == "Anticuerpos", "merged event records moved + folded children (folded row kept in payload)")

r4b = ingest_tender(conn, {"source": "FTS", "source_ref": "FTS-77", "title": "Suministro reactivos 2027 (revisado)",
                           "buyer": "Hospital Clinic", "deadline": d + timedelta(days=9)}, "fts-scraper")
ok(r4b.outcome == "matched_source_ref" and r4b.tender_id == r1.tender_id,
   "merged duplicate's ref, re-scraped with a new title, resolves to survivor")
r4c = ingest_tender(conn, {"source": "FTS", "title": "Suministro de Reactivos de Laboratorio 2027",
                           "buyer": "Hospital Clinic", "deadline": d + timedelta(days=2)}, "fts-scraper")
ok(r4c.outcome == "matched_hash" and r4c.tender_id == r1.tender_id,
   "ref-less copy of merged duplicate routes to survivor by hash")
e1 = events(conn, r1.tender_id)
ok(e1 == ["created", "matched_hash", "fields_changed", "fields_changed", "lot_added", "merged",
          "fields_changed", "matched_hash", "fields_changed"], f"survivor events in order: {e1}")
fc = q(conn, "SELECT payload FROM tender_event WHERE tender_id=%s AND event_type='fields_changed' ORDER BY id", r1.tender_id)
ok([next(iter(p[0]["not_applied"].values()))["reason"] for p in fc] == ["lower_priority", "lower_priority", "merged_duplicate_ref", "stale_copy"]
   and all(not p[0]["changes"] for p in fc),
   "TED copy, merged duplicate's ref and stale hash copy all recorded without overwriting")
e4 = events(conn, r4.tender_id)
ok(e4 == ["created", "lot_added", "lot_added", "amended", "submission_added", "submission_added", "merged"],
   f"duplicate events in order: {e4}")
actors = q(conn, "SELECT actor, source FROM tender_event WHERE tender_id=%s ORDER BY id", r1.tender_id)
ok(actors[0] == ("placsp-scraper", "PLC") and actors[1] == ("ted-scraper", "TED") and actors[5][0] == "review:blaise",
   f"actors/sources recorded")
ts = [r[0] for r in q(conn, "SELECT occurred_at FROM tender_event ORDER BY id")]
ok(ts == sorted(ts), "occurred_at is monotonic with id")

# --- 8. append-only -------------------------------------------------------------
expect_error(conn, "UPDATE tender_event SET actor = 'x'", psycopg.errors.InsufficientPrivilege, "UPDATE tender_event blocked")
expect_error(conn, "DELETE FROM tender_event", psycopg.errors.InsufficientPrivilege, "DELETE tender_event blocked")
expect_error(conn, "DELETE FROM tender_event WHERE false", psycopg.errors.InsufficientPrivilege,
             "DELETE matching no rows also blocked")
expect_error(conn, "TRUNCATE tender_event", psycopg.errors.InsufficientPrivilege, "TRUNCATE tender_event blocked")

# --- 9. direct INSERT without accession_no ---------------------------------------
acc = q(conn, "INSERT INTO tender (source, dedupe_hash, title) VALUES ('TED', 'direct-h', 'Direct row') RETURNING accession_no")[0][0]
ok(accession.is_valid(acc) and acc.startswith("TRC-26-TED-"), f"direct INSERT got valid number {acc}")
acc_old = q(conn, "INSERT INTO tender (source, dedupe_hash, title, captured_at) VALUES ('TED','direct-h2','Old row','2025-06-01') RETURNING accession_no")[0][0]
ok(accession.is_valid(acc_old) and acc_old.startswith("TRC-25-TED-000001"), f"trigger uses captured_at year: {acc_old}")
acc_explicit = q(conn, "INSERT INTO tender (accession_no, source, dedupe_hash, title) VALUES (issue_accession('PLC'), 'PLC', 'direct-h3', 'Explicit') RETURNING accession_no")[0][0]
ok(accession.is_valid(acc_explicit), f"explicit accession_no still accepted: {acc_explicit}")
ok(q(conn, "SELECT count(*) FROM tender_event e JOIN tender t ON t.id=e.tender_id WHERE t.dedupe_hash LIKE 'direct-%%'")[0][0] == 0,
   "trigger writes no events and does no dedupe")

# --- 10. children + deadline change -------------------------------------------------
again = add_lot(conn, r1.tender_id, title="Anticuerpos", source_lot_ref="Lote 1", actor="placsp-scraper")
lot3 = add_lot(conn, r4.tender_id, title="Kits", source_lot_ref="Lote 2", actor="placsp-scraper")   # merged id -> survivor
ok(again.outcome == "existing" and again.id == s_lot1.id, "re-scraped lot returns existing, no new number")
ok(lot3.accession_no == r1.accession_no + "/L03" and lot3.tender_id == r1.tender_id,
   f"new lot {lot3.accession_no} numbered after moved lots (merged id followed to survivor)")
try:
    add_amendment(conn, r1.tender_id, kind="deadline_change", actor="x")
    ok(False, "deadline_change without new_deadline_at rejected")
except IngestError:
    ok(True, "deadline_change without new_deadline_at rejected")
new_dl = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
am = add_amendment(conn, r1.tender_id, kind="deadline_change", lot_id=lot3.id, new_deadline_at=new_dl,
                   document_sha256="abc", actor="placsp-scraper", source="PLC")
am_again = add_amendment(conn, r1.tender_id, kind="deadline_change", new_deadline_at=new_dl,
                         document_sha256="abc", actor="placsp-scraper")
sub = add_submission(conn, r1.tender_id, lot_id=s_lot1.id, opco="Abcam", owner="blaise", actor="user:blaise")
ok(am.accession_no.endswith("/A02") and am_again.outcome == "existing" and sub.accession_no.endswith("/S03"),
   f"amendment {am.accession_no} (dup doc ignored), submission {sub.accession_no}")
dl, h, title, buyer = q(conn, "SELECT deadline_at, dedupe_hash, title, buyer FROM tender WHERE id=%s", r1.tender_id)[0]
ok(dl == new_dl, f"deadline_change amendment moved tender.deadline_at to {dl:%Y-%m-%d %H:%M} UTC")
ok(h == dedupe.dedupe_hash(title, buyer, new_dl), "dedupe_hash recomputed in Python for the new deadline")
p = q(conn, "SELECT payload FROM tender_event WHERE tender_id=%s AND event_type='deadline_changed'", r1.tender_id)
ok(len(p) == 1 and p[0][0]["hash_updated"] and p[0][0]["amendment"] == am.accession_no,
   "deadline_changed logged once (not for the re-scraped duplicate document)")
r_new = ingest_tender(conn, {"source": "FTS", "source_ref": "FTS-NEWDL", "title": title, "buyer": buyer,
                             "deadline": new_dl}, "fts-scraper")
ok(r_new.outcome == "matched_hash" and r_new.tender_id == r1.tender_id,
   "a new platform showing the new deadline matches by hash")
ok(set_status(conn, r1.tender_id, "in_bid", "user:blaise") and not set_status(conn, r1.tender_id, "in_bid", "user:blaise"),
   "set_status changes once, no-op the second time")
e1 = events(conn, r1.tender_id)
ok(e1[-6:] == ["lot_added", "amended", "deadline_changed", "submission_added", "matched_hash", "status_changed"],
   f"child events: {e1[-6:]}")
out = subprocess.run([sys.executable, "review.py", "history", r1.accession_no], capture_output=True, text=True, env=env)
n_ev = q(conn, "SELECT count(*) FROM tender_event WHERE tender_id IN (SELECT id FROM tender WHERE id=%s OR merged_into=%s)",
         r1.tender_id, r1.tender_id)[0][0]
ok(out.returncode == 0 and sum(1 for l in out.stdout.splitlines() if l[:4].isdigit()) == n_ev
   and "deadline_changed" in out.stdout and "(not applied: lower-priority platform)" in out.stdout,
   f"review.py history prints all {n_ev} events of survivor + merged tender")
out = subprocess.run([sys.executable, "review.py", "show", d_lot9.accession_no], capture_output=True, text=True, env=env)
print(out.stdout)
ok(out.returncode == 0 and f"is now {r1.accession_no}/L02" in out.stdout and "FTS FTS-77" in out.stdout,
   "review.py show resolves an old child ID and lists platform refs")

# --- 11. concurrency --------------------------------------------------------------
def hammer(records, actor):
    barrier = threading.Barrier(len(records))
    results, errors = [], []

    def work(rec):
        c = psycopg.connect(DSN, autocommit=True)
        try:
            barrier.wait()
            results.append(ingest_tender(c, rec, actor))
        except Exception as e:      # noqa: BLE001
            errors.append(repr(e))
        finally:
            c.close()
    ts = [threading.Thread(target=work, args=(r,)) for r in records]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return results, errors

same_ref = [{"source": "PLC", "source_ref": "CONC-1", "title": f"Concurrent tender v{i}", "buyer": "Buyer",
             "deadline": d} for i in range(8)]
res, errs = hammer(same_ref, "conc-test")
n = q(conn, "SELECT count(*) FROM tender WHERE source='PLC' AND source_ref='CONC-1'")[0][0]
ok(not errs and n == 1 and len({r.tender_id for r in res}) == 1
   and Counter(r.outcome for r in res) == {"created": 1, "matched_source_ref": 7},
   f"8 threads, same source_ref -> 1 tender; outcomes {dict(Counter(r.outcome for r in res))}; errors {errs}")

same_hash = [{"source": ["PLC", "TED", "FTS"][i % 3], "source_ref": f"H-{i}", "title": "Concurrent copy on many platforms",
              "buyer": "Buyer Ltd", "deadline": d} for i in range(8)]
res, errs = hammer(same_hash, "conc-test")
n = q(conn, "SELECT count(*) FROM tender WHERE title = 'Concurrent copy on many platforms'")[0][0]
ok(not errs and n == 1 and Counter(r.outcome for r in res) == {"created": 1, "matched_hash": 7},
   f"8 threads, different refs/platforms, same hash -> 1 tender; outcomes {dict(Counter(r.outcome for r in res))}; errors {errs}")
ev = q(conn, "SELECT event_type, count(*) FROM tender_event WHERE actor='conc-test' GROUP BY 1 ORDER BY 1")
ok(ev == [("created", 2), ("fields_changed", 7), ("matched_hash", 7)],
   f"events: 2 created, 7 matched_hash (new refs), 7 fields_changed (each same-ref thread changed the title): {ev}")
t_final = q(conn, "SELECT title, dedupe_hash, title_norm, buyer, deadline_at FROM tender WHERE source='PLC' AND source_ref='CONC-1'")[0]
last_new = q(conn, "SELECT payload->'changes'->'title'->>'new' FROM tender_event e JOIN tender t ON t.id=e.tender_id "
                   "WHERE t.source_ref='CONC-1' AND e.event_type='fields_changed' ORDER BY e.id DESC LIMIT 1")[0][0]
ok(t_final[0] == last_new and t_final[1] == dedupe.dedupe_hash(t_final[0], t_final[3], t_final[4])
   and t_final[2] == dedupe.clean_title(t_final[0]), "final title = last logged change; hash and title_norm in sync")

# same second-platform ref from 8 threads: one ref row, one tender
base = ingest_tender(conn, {"source": "PLC", "source_ref": "SP-1", "title": "Second platform tender", "buyer": "Buyer 2",
                            "deadline": d}, "conc-test")
ted = [{"source": "TED", "source_ref": "TED-CONC", "title": "Second platform tender", "buyer": "Buyer 2",
        "deadline": d} for _ in range(8)]
res, errs = hammer(ted, "conc-test2")
ok(not errs and {r.tender_id for r in res} == {base.tender_id}
   and Counter(r.outcome for r in res) == {"matched_hash": 1, "matched_source_ref": 7}
   and q(conn, "SELECT count(*) FROM tender_source_ref WHERE source='TED' AND source_ref='TED-CONC'")[0][0] == 1
   and q(conn, "SELECT count(*) FROM tender WHERE title='Second platform tender'")[0][0] == 1,
   f"8 threads, same second-platform ref -> 1 ref row, 1 tender; outcomes {dict(Counter(r.outcome for r in res))}; errors {errs}")

# merge running while 8 threads re-scrape the duplicate's ref
X = ingest_tender(conn, {"source": "PLC", "source_ref": "X-1", "title": "Microscopios confocales", "buyer": "Hospital del Mar",
                         "deadline": d}, "conc-test")
Y = ingest_tender(conn, {"source": "TED", "source_ref": "Y-1", "title": "Microscopios confocales 2027", "buyer": "Hospital del Mar",
                         "deadline": d + timedelta(days=1)}, "conc-test")
barrier = threading.Barrier(9)
res, errs = [], []


def scrape(i):
    c = psycopg.connect(DSN, autocommit=True)
    try:
        barrier.wait()
        res.append(ingest_tender(c, {"source": "TED", "source_ref": "Y-1", "title": f"Microscopios confocales rev {i}",
                                     "buyer": "Hospital del Mar", "deadline": d}, "conc-test3"))
    except Exception as e:  # noqa: BLE001
        errs.append(repr(e))
    finally:
        c.close()


def merger():
    c = psycopg.connect(DSN, autocommit=True)
    try:
        barrier.wait()
        c.execute("SELECT merge_tender(%s, %s, 'conc-test3')", (Y.tender_id, X.tender_id))
    except Exception as e:  # noqa: BLE001
        errs.append(repr(e))
    finally:
        c.close()


ts = [threading.Thread(target=scrape, args=(i,)) for i in range(8)] + [threading.Thread(target=merger)]
[t.start() for t in ts]
[t.join() for t in ts]
after = ingest_tender(conn, {"source": "TED", "source_ref": "Y-1", "title": "whatever", "deadline": d}, "conc-test3")
ok(not errs and {r.tender_id for r in res} <= {X.tender_id, Y.tender_id} and len(res) == 8
   and after.tender_id == X.tender_id
   and q(conn, "SELECT tender_id FROM tender_source_ref WHERE source='TED' AND source_ref='Y-1'") == [(X.tender_id,)]
   and q(conn, "SELECT count(*) FROM tender WHERE buyer='Hospital del Mar'")[0][0] == 2,
   f"merge racing 8 re-scrapes: no errors/deadlocks, no new tender, ref ends on survivor "
   f"(went to dup before merge: {sum(r.tender_id == Y.tender_id for r in res)}, to survivor: {sum(r.tender_id == X.tender_id for r in res)})")

print(f"\nALL {len(PASSED)} CHECKS PASSED")
