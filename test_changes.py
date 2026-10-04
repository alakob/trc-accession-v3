"""Change-tracking tests (004). Safe to run on a fresh database or right after test_e2e.py:
    DATABASE_URL="dbname=trc_v4_test host=/var/run/postgresql" python test_changes.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import candidates
import dedupe
from ingest import IngestError, add_amendment, edit_field, ingest_tender, lock_field, set_status, unlock_field

DSN = os.environ.get("DATABASE_URL", "dbname=trc_v4_test host=/var/run/postgresql")
RUN = uuid.uuid4().hex[:6].upper()          # unique refs/buyers so the file can run on a used database
PASSED: list[str] = []


def ok(cond, msg):
    if not cond:
        print(f"FAIL  {msg}")
        raise SystemExit(1)
    PASSED.append(msg)
    print(f"PASS  {msg}")


conn = psycopg.connect(DSN, autocommit=True)


def q(sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def evs(tid, typ=None):
    rows = q("SELECT event_type, payload FROM tender_event WHERE tender_id=%s ORDER BY id", tid)
    return [r for r in rows if typ is None or r[0] == typ]


def tender(tid):
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM tender WHERE id=%s", (tid,))
        return cur.fetchone()


def rec(source, ref, title, buyer, deadline, **kw):
    return {"source": source, "source_ref": ref, "title": title, "buyer": buyer, "deadline": deadline, **kw}


d = datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)
BUY = f"Servicio Andaluz de Salud {RUN}"
env = {**os.environ, "DATABASE_URL": DSN, "TRC_ACTOR": "review:blaise"}

# ---------- 1. primary source title change ----------
a = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión", BUY, d,
                            description="Bombas volumétricas", estimated_value="125000.50", currency="eur",
                            cpv_codes="33194110-0, 33100000-1"), "placsp-scraper")
t0 = tender(a.tender_id)
ok(a.created and t0["currency"] == "EUR" and t0["estimated_value"] == Decimal("125000.50")
   and t0["cpv_codes"] == ["33100000-1", "33194110-0"] and t0["description"] == "Bombas volumétricas",
   "optional fields stored on create (currency upper-cased, CPV sorted)")
n0 = len(evs(a.tender_id))
last_seen0 = q("SELECT last_seen_at FROM tender_source_ref WHERE source_ref=%s", f"CH-{RUN}-1")[0][0]

r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión", BUY, d,
                            description="Bombas volumétricas", estimated_value=125000.5, currency="EUR"), "placsp-scraper")
ok(r.outcome == "matched_source_ref" and not r.changed and not r.not_applied and len(evs(a.tender_id)) == n0,
   "unchanged re-scrape: no change, no event (missing cpv_codes is 'unknown', not 'cleared')")
ok(q("SELECT last_seen_at FROM tender_source_ref WHERE source_ref=%s", f"CH-{RUN}-1")[0][0] > last_seen0,
   "unchanged re-scrape still moves last_seen_at")
ok(tender(a.tender_id)["cpv_codes"] == ["33100000-1", "33194110-0"], "field not provided was not cleared")

old_hash = t0["dedupe_hash"]
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión y equipos", BUY, d,
                            estimated_value="130000"), "placsp-scraper")
t1 = tender(a.tender_id)
ok(set(r.changed) == {"title", "estimated_value"} and t1["title"] == "Suministro de bombas de infusión y equipos"
   and t1["estimated_value"] == Decimal("130000.00"), f"primary title + value change applied: {r.changed}")
ok(t1["title_norm"] == dedupe.clean_title(t1["title"]) and t1["dedupe_hash"] == dedupe.dedupe_hash(t1["title"], BUY, d)
   and t1["dedupe_hash"] != old_hash, "title_norm and dedupe_hash recomputed by Python")
ok(q("SELECT count(*) FROM tender_hash_history WHERE tender_id=%s AND dedupe_hash=%s", a.tender_id, old_hash)[0][0] == 1,
   "old hash kept in tender_hash_history")
ok(t1["scan_at"] > t0["scan_at"] and t1["updated_at"] is not None, "scan_at bumped so the candidates job re-checks it")
fc = evs(a.tender_id, "fields_changed")
ok(len(fc) == 1 and fc[0][1]["changes"] == {
       "title": {"old": "Suministro de bombas de infusión", "new": "Suministro de bombas de infusión y equipos"},
       "estimated_value": {"old": "125000.50", "new": "130000.00"}}
   and fc[0][1]["hash"]["updated"] and fc[0][1]["source_ref"] == f"CH-{RUN}-1",
   "one fields_changed event with {field: {old, new}}, hash info and source_ref")
ok(q("SELECT actor, source FROM tender_event WHERE tender_id=%s AND event_type='fields_changed'", a.tender_id)
   == [("placsp-scraper", "PLC")], "event carries actor and source")

# ---------- 2. old hash still matches (never-seen platform showing the old title) ----------
r = ingest_tender(conn, rec("FTS", None, "Suministro de bombas de infusión", BUY, d), "fts-scraper")
ok(r.outcome == "matched_hash" and r.tender_id == a.tender_id, "never-seen copy with the OLD title matches via hash history")
ok(tender(a.tender_id)["title"] == "Suministro de bombas de infusión y equipos" and r.not_applied == ("title",),
   "stale copy does not revert the title (recorded as not applied)")
ok(q("SELECT payload->>'via' FROM tender_event WHERE tender_id=%s AND event_type='matched_hash' ORDER BY id DESC LIMIT 1",
     a.tender_id)[0][0] == "history", "matched_hash event says via=history")
r = ingest_tender(conn, rec("FTS", None, "Suministro de bombas de infusión", BUY, d), "fts-scraper")
ok(len(evs(a.tender_id, "fields_changed")) == 2, "same stale ref-less copy again: no second fields_changed")

# ---------- 3. lower-priority platform ----------
ted = ingest_tender(conn, rec("TED", f"TED-{RUN}-1", "Suministro de bombas de infusión y equipos", BUY, d), "ted-scraper")
ok(ted.outcome == "matched_hash" and not ted.not_applied, "TED copy matched by hash, identical fields: nothing to record")
n = len(evs(a.tender_id, "fields_changed"))
r = ingest_tender(conn, rec("TED", f"TED-{RUN}-1", "Supply of infusion pumps and equipment", BUY, d + timedelta(days=3),
                            status="cancelled"), "ted-scraper")
t2 = tender(a.tender_id)
ok(set(r.not_applied) == {"title", "deadline_at", "status"} and not r.changed
   and t2["title"] == t1["title"] and t2["deadline_at"] == d and t2["status"] == "open",
   "TED differences (title, deadline, status) recorded but not applied")
p = evs(a.tender_id, "fields_changed")[-1][1]
ok(len(evs(a.tender_id, "fields_changed")) == n + 1 and p["not_applied"]["title"]["reason"] == "lower_priority"
   and p["changes"] == {}, "one event, reason lower_priority, no changes")
ingest_tender(conn, rec("TED", f"TED-{RUN}-1", "Supply of infusion pumps and equipment", BUY, d + timedelta(days=3),
                        status="cancelled"), "ted-scraper")
ok(len(evs(a.tender_id, "fields_changed")) == n + 1, "TED showing the same difference again: no new event")
ingest_tender(conn, rec("TED", f"TED-{RUN}-1", "Supply of infusion pumps", BUY, d + timedelta(days=3),
                        status="cancelled"), "ted-scraper")
p = evs(a.tender_id, "fields_changed")[-1][1]
ok(len(evs(a.tender_id, "fields_changed")) == n + 2 and list(p["not_applied"]) == ["title"],
   "TED changes its title again: new event with only the new difference")

# ---------- 4. equal-priority secondary platform ----------
fts = ingest_tender(conn, rec("FTS", f"FTS-{RUN}-1", "Suministro de bombas de infusión y equipos", BUY, d,
                              description="Infusion pumps (FTS text)"), "fts-scraper")
ok(fts.outcome == "matched_hash" and fts.not_applied == ("description",) and tender(a.tender_id)["description"] == "Bombas volumétricas",
   "equal-priority FTS copy: first-seen difference recorded, not applied")
r = ingest_tender(conn, rec("FTS", f"FTS-{RUN}-1", "Suministro de bombas de infusión y equipos", BUY, d,
                            description="Infusion pumps, 40 units (FTS amended)"), "fts-scraper")
ok(r.changed == ("description",) and tender(a.tender_id)["description"] == "Infusion pumps, 40 units (FTS amended)",
   "FTS itself changes the value later: equal priority may overwrite")

# ---------- 5. manual lock / edit ----------
out = subprocess.run([sys.executable, "review.py", "lock", a.accession_no, "title"], capture_output=True, text=True, env=env)
ok(out.returncode == 0 and "locked" in out.stdout and "title" in tender(a.tender_id)["locked_fields"], "review.py lock title")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión, equipos y fungibles", BUY, d),
                  "placsp-scraper")
ok(r.not_applied == ("title",) and tender(a.tender_id)["title"] == "Suministro de bombas de infusión y equipos",
   "primary re-scrape does not overwrite a locked title")
ok(evs(a.tender_id, "fields_changed")[-1][1]["not_applied"]["title"]["reason"] == "locked", "reason 'locked' recorded")
out = subprocess.run([sys.executable, "review.py", "unlock", a.accession_no, "title"], capture_output=True, text=True, env=env)
ok(out.returncode == 0 and "title" not in tender(a.tender_id)["locked_fields"], "review.py unlock title")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión, equipos y fungibles", BUY, d),
                  "placsp-scraper")
ok(r.changed == ("title",), "after unlock the primary source's (unchanged) title is applied")
out = subprocess.run([sys.executable, "review.py", "edit", a.accession_no, "buyer", "SAS - Hospital Virgen del Rocío"],
                     capture_output=True, text=True, env=env)
t3 = tender(a.tender_id)
ok(out.returncode == 0 and t3["buyer"] == "SAS - Hospital Virgen del Rocío" and "buyer" in t3["locked_fields"]
   and t3["buyer_norm"] == dedupe.clean_buyer(t3["buyer"]) and t3["dedupe_hash"] == dedupe.dedupe_hash(t3["title"], t3["buyer"], d),
   "review.py edit: value set, field locked, norms + hash in sync")
last = evs(a.tender_id)[-2:]
ok([e[0] for e in last] == ["fields_changed", "field_locked"] and last[0][1]["match"] == "manual"
   and q("SELECT actor FROM tender_event WHERE tender_id=%s ORDER BY id DESC LIMIT 1", a.tender_id)[0][0] == "review:blaise",
   "manual edit logged (fields_changed match=manual + field_locked, actor review:blaise)")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión, equipos y fungibles", BUY, d), "placsp-scraper")
ok(r.not_applied == ("buyer",) and tender(a.tender_id)["buyer"] == "SAS - Hospital Virgen del Rocío",
   "scrape never overwrites a manually edited field")
out = subprocess.run([sys.executable, "review.py", "lock", a.accession_no, "colour"], capture_output=True, text=True, env=env)
ok(out.returncode != 0 and "unknown field" in out.stderr, "unknown field rejected")
buyer_now = tender(a.tender_id)["buyer"]
unlock_field(conn, a.tender_id, "buyer", "review:blaise")

# ---------- 6. platform status ----------
set_status(conn, a.tender_id, "in_bid", "user:blaise")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión, equipos y fungibles", BUY, d,
                            status="open"), "placsp-scraper")
ok(tender(a.tender_id)["status"] == "in_bid" and "status" not in r.changed + r.not_applied,
   "platform 'open' never overwrites our workflow status (in_bid), and is not logged")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "Suministro de bombas de infusión, equipos y fungibles", BUY, d,
                            status="Canceled"), "placsp-scraper")
ok("status" in r.changed and tender(a.tender_id)["status"] == "cancelled", "platform 'cancelled' from primary is applied")
try:
    ingest_tender(conn, rec("PLC", f"CH-{RUN}-1", "x", BUY, d, status="on hold maybe"), "placsp-scraper")
    ok(False, "unknown platform status rejected")
except IngestError:
    ok(True, "unknown platform status rejected")

# ---------- 7. deadline change via amendment, old deadline still matches ----------
b = ingest_tender(conn, rec("PLC", f"CH-{RUN}-2", "Mantenimiento de resonancias magnéticas", BUY, d), "placsp-scraper")
new_dl = d + timedelta(days=14)
add_amendment(conn, b.tender_id, kind="deadline_change", new_deadline_at=new_dl, actor="placsp-scraper", source="PLC")
tb = tender(b.tender_id)
ok(tb["deadline_at"] == new_dl and tb["dedupe_hash"] == dedupe.dedupe_hash(tb["title"], BUY, new_dl)
   and q("SELECT count(*) FROM tender_hash_history WHERE tender_id=%s", b.tender_id)[0][0] == 1,
   "deadline amendment: deadline + hash updated, old hash in history")
r = ingest_tender(conn, rec("TED", f"TED-{RUN}-2", "Mantenimiento de resonancias magnéticas", BUY, d), "ted-scraper")
ok(r.outcome == "matched_hash" and r.tender_id == b.tender_id and tender(b.tender_id)["deadline_at"] == new_dl,
   "never-seen platform still showing the OLD deadline matches via hash history (fixes v3 caveat)")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-2", "Mantenimiento de resonancias magnéticas", BUY, d), "placsp-scraper")
ok(not r.changed and r.not_applied == ("deadline_at",) and tender(b.tender_id)["deadline_at"] == new_dl
   and evs(b.tender_id)[-1][1]["not_applied"]["deadline_at"]["reason"] == "unchanged_on_platform",
   "primary listing still showing the pre-amendment deadline does NOT undo the amendment")
n_b = len(evs(b.tender_id))
ingest_tender(conn, rec("PLC", f"CH-{RUN}-2", "Mantenimiento de resonancias magnéticas", BUY, d), "placsp-scraper")
ok(len(evs(b.tender_id)) == n_b, "same standing difference is reported once")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-2", "Mantenimiento de resonancias magnéticas", BUY, new_dl + timedelta(days=1)),
                  "placsp-scraper")
ok(r.changed == ("deadline_at",) and tender(b.tender_id)["deadline_at"] == new_dl + timedelta(days=1),
   "primary platform really changing the deadline is applied")
new_dl = new_dl + timedelta(days=1)
lock_field(conn, b.tender_id, "deadline_at", "review:blaise")
add_amendment(conn, b.tender_id, kind="deadline_change", new_deadline_at=new_dl + timedelta(days=7), actor="placsp-scraper")
ok(tender(b.tender_id)["deadline_at"] == new_dl and evs(b.tender_id)[-1][1]["not_applied"]["deadline_at"]["reason"] == "locked",
   "amendment respects a locked deadline (recorded as not applied)")

# ---------- 8. hash clash ----------
x = ingest_tender(conn, rec("PLC", f"CH-{RUN}-X", "Gases medicinales lote norte", BUY, d), "placsp-scraper")
y = ingest_tender(conn, rec("PLC", f"CH-{RUN}-Y", "Gases medicinales", BUY, d), "placsp-scraper")
x_hash = tender(x.tender_id)["dedupe_hash"]
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-X", "Gases medicinales", BUY, d), "placsp-scraper")
tx = tender(x.tender_id)
h = evs(x.tender_id, "fields_changed")[-1][1]["hash"]
ok(r.changed == ("title",) and tx["title"] == "Gases medicinales" and tx["dedupe_hash"] == x_hash
   and tx["title_norm"] == "gases medicinales", "clash: title applied, old hash kept, title_norm updated")
ok(not h["updated"] and h["reason"] == "conflict" and h["conflict_with"] == y.accession_no, f"clash recorded in event: {h}")
ok(q("SELECT reason FROM tender_possible_duplicate WHERE tender_id=%s AND candidate_id=%s", y.tender_id, x.tender_id)
   == [("hash clash after re-scrape",)], "clashing pair queued for review (newer -> older)")
with conn.cursor() as cur:
    cur.execute("SELECT merge_tender(%s, %s, 'review:blaise')", (y.tender_id, x.tender_id))
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-X", "Gases medicinales", BUY, d), "placsp-scraper")
tx = tender(x.tender_id)
ok(not r.changed and tx["dedupe_hash"] == dedupe.dedupe_hash("Gases medicinales", BUY, d)
   and evs(x.tender_id, "fields_changed")[-1][1]["hash"]["updated"],
   "after merging the clashing tender, the next re-scrape heals the hash (logged)")
r = ingest_tender(conn, rec("PLC", f"CH-{RUN}-Y", "Gases medicinales (Y text)", BUY, d), "placsp-scraper")
ok(r.tender_id == x.tender_id and not r.changed and tender(x.tender_id)["title"] == "Gases medicinales",
   "merged duplicate's own primary ref cannot rewrite the survivor")

# ---------- 9. candidates job re-checks changed tenders ----------
p1 = ingest_tender(conn, rec("PLC", f"CH-{RUN}-P1", "Servicio de limpieza hospitalaria", BUY, d), "placsp-scraper")
p2 = ingest_tender(conn, rec("PLC", f"CH-{RUN}-P2", "Suministro de papel", BUY, d + timedelta(days=1)), "placsp-scraper")
candidates.run_once(conn)
pair = "SELECT count(*) FROM tender_possible_duplicate WHERE %s IN (tender_id, candidate_id) AND %s IN (tender_id, candidate_id)"
ok(q(pair, p1.tender_id, p2.tender_id)[0][0] == 0, "unrelated titles not flagged")
ingest_tender(conn, rec("PLC", f"CH-{RUN}-P2", "Servicio de limpieza hospitalaria 2027", BUY, d + timedelta(days=1)), "placsp-scraper")
s = candidates.run_once(conn)
ok(q(pair, p1.tender_id, p2.tender_id)[0][0] == 1, f"after a title change the job re-checks and flags the pair ({s['flagged']} new)")

# ---------- 10. concurrency: 8 different titles for the same ref ----------
c0 = ingest_tender(conn, rec("PLC", f"CH-{RUN}-C", "Concurrent change base title", BUY, d), "placsp-scraper")
titles = [f"Concurrent change title variant {i}" for i in range(8)]
barrier = threading.Barrier(8)
res, errs = [], []


def work(tt):
    c = psycopg.connect(DSN, autocommit=True)
    try:
        barrier.wait()
        res.append(ingest_tender(c, rec("PLC", f"CH-{RUN}-C", tt, BUY, d), "conc-change"))
    except Exception as e:  # noqa: BLE001
        errs.append(repr(e))
    finally:
        c.close()


ts = [threading.Thread(target=work, args=(tt,)) for tt in titles]
[t.start() for t in ts]
[t.join() for t in ts]
fcs = [p for _, p in evs(c0.tender_id, "fields_changed")]
chain_ok = all(fcs[i]["changes"]["title"]["old"] == (fcs[i - 1]["changes"]["title"]["new"] if i else "Concurrent change base title")
               for i in range(len(fcs)))
tc = tender(c0.tender_id)
ok(not errs and len(res) == 8 and {r.tender_id for r in res} == {c0.tender_id}
   and q("SELECT count(*) FROM tender WHERE buyer=%s AND title LIKE 'Concurrent change%%'", BUY)[0][0] == 1,
   f"8 threads, same ref, different titles -> one tender, no errors ({errs})")
ok(len(fcs) == 8 and chain_ok and all(r.changed == ("title",) for r in res),
   "exactly 8 fields_changed events, each old = previous new (serialised)")
ok(tc["title"] == fcs[-1]["changes"]["title"]["new"] and tc["dedupe_hash"] == dedupe.dedupe_hash(tc["title"], BUY, d)
   and tc["title_norm"] == dedupe.clean_title(tc["title"]),
   "final state = last logged change, hash + title_norm consistent")
hist = {h for (h,) in q("SELECT dedupe_hash FROM tender_hash_history WHERE tender_id=%s", c0.tender_id)}
ok(len(hist) == 8 and tc["dedupe_hash"] not in hist, "8 retired hashes in history, current hash not among them")
same = [rec("PLC", f"CH-{RUN}-C", tc["title"], BUY, d) for _ in range(8)]
barrier = threading.Barrier(8)
res, errs = [], []
ts = [threading.Thread(target=work, args=(tc["title"],)) for _ in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
ok(not errs and len(evs(c0.tender_id, "fields_changed")) == 8, "8 threads re-sending the current title: no new events")

# ---------- 11. review.py history shows diffs ----------
out = subprocess.run([sys.executable, "review.py", "history", a.accession_no], capture_output=True, text=True, env=env)
print("\n".join(out.stdout.splitlines()[:14]))
ok(out.returncode == 0 and "title: Suministro de bombas de infusión  →  Suministro de bombas de infusión y equipos" in out.stdout
   and "(not applied: locked by a person)" in out.stdout and "field_locked" in out.stdout,
   "review.py history prints readable field diffs")
print(f"\nALL {len(PASSED)} CHANGE-TRACKING CHECKS PASSED")
