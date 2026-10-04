"""Upgrade-path tests. Each scenario builds an older schema, fills it the way that version's code
did (plain SQL calls to its functions), upgrades with apply.sh, and checks the backfills.

    python test_upgrade.py [PREFIX]     # default trc_upgrade; DROPS AND RECREATES PREFIX_v2, _v3, _v4, _v5
Needs psql on PATH with rights to create databases (same as apply.sh).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import accession  # noqa: E402
import dedupe  # noqa: E402
from ingest import IngestError, add_lot, ingest_tender  # noqa: E402

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "trc_upgrade"
HOST = os.environ.get("PGHOST", "/var/run/postgresql")
PASSED = []


def ok(cond, msg):
    if not cond:
        print(f"FAIL  {msg}")
        raise SystemExit(1)
    PASSED.append(msg)
    print(f"PASS  {msg}")


def apply(db, *args, upto=None):
    env = {**os.environ, **({"UPTO": upto} if upto else {})}
    out = subprocess.run([os.path.join(HERE, "apply.sh"), db, *args], capture_output=True, text=True, env=env)
    if out.returncode:
        print(out.stdout, out.stderr)
    return out


def q(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


# --- old-version writers: exactly the SQL those versions ran ---------------------------------
def fc(conn, source, ref, title, buyer, deadline, actor):
    """find_or_create_tender has had the same 11-argument signature since 002."""
    return q(conn, "SELECT tender_id, accession_no, outcome FROM find_or_create_tender(%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,'{}')",
             source, dedupe.clean_source_ref(ref), dedupe.dedupe_hash(title, buyer, deadline), title, buyer, deadline,
             dedupe.clean_title(title), dedupe.clean_buyer(buyer) or None, actor)[0]


def child(conn, tid, child_kind, **cols):
    no, acc = q(conn, "SELECT child_no, child_accession FROM issue_child_no(%s, %s)", tid, child_kind)[0]
    table, numcol = {"L": ("tender_lot", "lot_no"), "A": ("tender_amendment", "amendment_no"),
                     "S": ("tender_submission", "submission_no")}[child_kind]
    names = ["tender_id", numcol, "accession_no", *cols]
    return q(conn, f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) RETURNING id, accession_no",
             tid, no, acc, *cols.values())[0]


def unresolved(conn, url):
    import platforms
    try:
        platforms.resolve_platform(conn, url)
        return False
    except platforms.PlatformError:
        return True


def snapshot(conn):
    return q(conn, "SELECT (SELECT count(*) FROM tender), (SELECT count(*) FROM tender_event), "
                   "(SELECT count(*) FROM tender_source_ref), (SELECT count(*) FROM child_accession_alias), "
                   "(SELECT count(*) FROM tender_hash_history), (SELECT count(*) FROM tender_lot)")[0]


d = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)

# =========================== Scenario A: v2 database -> latest ===========================
DB = f"{PREFIX}_v2"
DSN = f"dbname={DB} host={HOST}"
out = apply(DB, "--fresh", upto="002")
ok(out.returncode == 0 and "003" not in out.stdout, "[v2] built v2 database (schema, dedupe, review, 002)")
conn = psycopg.connect(DSN, autocommit=True)
A = fc(conn, "PLC", "EXP-1", "Suministro de guantes de nitrilo", "Hospital La Fe", d, "placsp-scraper")
t1 = fc(conn, "TED", "TED-1", "SUMINISTRO GUANTES NITRILO", "Hospital La Fe", d, "ted-scraper")
t2 = fc(conn, "TED", "TED-1", "SUMINISTRO GUANTES NITRILO", "Hospital La Fe", d, "ted-scraper")
D = fc(conn, "FTS", "FTS-9", "Suministro guantes nitrilo 2027", "Hospital La Fe", d + timedelta(days=2), "fts-scraper")
ok(A[2] == "created" and t1[2] == t2[2] == "matched_hash" and D[2] == "created", "[v2] created, TED copy matched by hash twice")
a_lot1 = child(conn, A[0], "L", source_lot_ref="Lote 1", title="Guantes S")
d_lot1 = child(conn, D[0], "L", source_lot_ref="Lote 1", title="Guantes S")
d_lot3 = child(conn, D[0], "L", source_lot_ref="Lote 3", title="Guantes XL")
d_am = child(conn, D[0], "A", kind="clarification", document_sha256="g1")
d_sub = child(conn, D[0], "S", lot_id=d_lot1[0], opco="Abcam")
q(conn, "SELECT merge_tender(%s, %s, 'review:blaise')", D[0], A[0])
ok(q(conn, "SELECT count(*) FROM tender_lot WHERE tender_id=%s", D[0])[0][0] == 2, "[v2] v2 merge left children behind")
direct = q(conn, "INSERT INTO tender (source, source_ref, dedupe_hash, title) VALUES ('TED','DIRECT-1','direct-h','Direct row') RETURNING id")[0][0]
conn.close()

out = apply(DB)
ok(out.returncode == 0 and "004_change_tracking.sql" in out.stdout, "[v2] upgrade ran 002 + 003 + 004")
conn = psycopg.connect(DSN, autocommit=True)
refs = {(s, r): (t, p, o) for s, r, t, p, o in q(conn, "SELECT source, source_ref, tender_id, is_primary, origin FROM tender_source_ref")}
ok(refs == {("PLC", "EXP-1"): (A[0], True, "primary"), ("TED", "TED-1"): (A[0], False, "hash_match"),
            ("FTS", "FTS-9"): (A[0], False, "merge"), ("TED", "DIRECT-1"): (direct, True, "primary")},
   "[v2] refs backfilled from tender + event payloads, with origin")
ok(q(conn, "SELECT accession_no FROM resolve_child_accession(%s)", d_lot3[1])[0][0] == A[1] + "/L02"
   and q(conn, "SELECT child_id FROM resolve_child_accession(%s)", d_lot1[1])[0][0] == a_lot1[0]
   and q(conn, "SELECT accession_no FROM tender_submission WHERE id=%s", d_sub[0])[0][0] == A[1] + "/S01"
   and q(conn, "SELECT accession_no FROM resolve_child_accession(%s)", d_am[1])[0][0] == A[1] + "/A01",
   "[v2] children of a v2-merged tender moved, renumbered, old IDs resolve")
ok(all(accession.is_valid(a) for (a,) in q(conn, "SELECT accession_no FROM tender_lot UNION ALL SELECT accession_no FROM "
                                                 "tender_amendment UNION ALL SELECT accession_no FROM tender_submission")),
   "[v2] all child numbers valid")
ok(q(conn, "SELECT count(*) FROM tender WHERE scan_at <> created_at OR locked_fields <> '{}'")[0][0] == 0,
   "[v2] scan_at = created_at, no locked fields")
snap = snapshot(conn)
conn.close()
out = apply(DB)
conn = psycopg.connect(DSN, autocommit=True)
ok(out.returncode == 0 and snapshot(conn) == snap, f"[v2] re-running 002 + 003 + 004 changes nothing {snap}")
r = ingest_tender(conn, {"source": "TED", "source_ref": "TED-1", "title": "Guantes de nitrilo (corregido)",
                         "buyer": "Hospital La Fe", "deadline": d}, "ted-scraper")
ok(r.outcome == "matched_source_ref" and r.tender_id == A[0] and r.not_applied == ("title",),
   "[v2] TED re-scrape with new title: matched via backfilled ref, lower priority -> not applied")
r = ingest_tender(conn, {"source": "FTS", "source_ref": "FTS-9", "title": "Otro titulo", "deadline": d}, "fts-scraper")
ok(r.tender_id == A[0] and not r.changed, "[v2] merged duplicate's ref resolves to survivor and cannot rewrite it")
r = ingest_tender(conn, {"source": "PLC", "source_ref": "EXP-1", "title": "Suministro de guantes de nitrilo y vinilo",
                         "buyer": "Hospital La Fe", "deadline": d}, "placsp-scraper")
ok(r.changed == ("title",), "[v2] primary title change applied on upgraded data")
r = ingest_tender(conn, {"source": "FTS", "source_ref": "FTS-NEW", "title": "Suministro de guantes de nitrilo",
                         "buyer": "Hospital La Fe", "deadline": d}, "fts-scraper")
ok(r.outcome == "matched_hash" and r.tender_id == A[0], "[v2] never-seen platform with the old title matches via hash history")
conn.close()

# =========================== Scenario B: v3 database -> latest ===========================
DB = f"{PREFIX}_v3"
DSN = f"dbname={DB} host={HOST}"
out = apply(DB, "--fresh", upto="003")
ok(out.returncode == 0 and "003_source_refs.sql" in out.stdout and "004" not in out.stdout, "[v3] built v3 database (… 003)")
conn = psycopg.connect(DSN, autocommit=True)
B = fc(conn, "PLC", "EXP-7", "Mantenimiento de ecógrafos", "Hospital Clínico San Carlos", d, "placsp-scraper")
tb = fc(conn, "TED", "TED-7", "Mantenimiento de ecógrafos", "Hospital Clínico San Carlos", d, "ted-scraper")
E = fc(conn, "FTS", "FTS-7", "Mantenimiento ecografos 2027", "Hospital Clinico San Carlos", d + timedelta(days=1), "fts-scraper")
lotE = child(conn, E[0], "L", source_lot_ref="Lote 1", title="Ecógrafos")
q(conn, "SELECT merge_tender(%s, %s, 'review:blaise')", E[0], B[0])            # v3 merge moves children
# v3 add_amendment(deadline_change): new deadline + recomputed hash + deadline_changed event
new_dl = d + timedelta(days=10)
old_h = dedupe.dedupe_hash("Mantenimiento de ecógrafos", "Hospital Clínico San Carlos", d)
new_h = dedupe.dedupe_hash("Mantenimiento de ecógrafos", "Hospital Clínico San Carlos", new_dl)
am = child(conn, B[0], "A", kind="deadline_change", new_deadline_at=new_dl)
q(conn, "UPDATE tender SET deadline_at=%s, dedupe_hash=%s WHERE id=%s RETURNING id", new_dl, new_h, B[0])
q(conn, "INSERT INTO tender_event (tender_id, event_type, actor, source, payload) VALUES (%s,'deadline_changed','placsp-scraper','PLC',%s) RETURNING id",
  B[0], Jsonb({"from": d.isoformat(), "to": new_dl.isoformat(), "amendment": am[1], "lot_id": None,
               "old_hash": old_h, "new_hash": new_h, "hash_updated": True, "hash_conflict_with": None}))
ok(tb[2] == "matched_hash" and q(conn, "SELECT count(*) FROM tender_lot WHERE tender_id=%s", B[0])[0][0] == 1,
   "[v3] v3 data: TED ref, merge with children, deadline change")
before = q(conn, "SELECT count(*) FROM tender_event")[0][0]
conn.close()

out = apply(DB)
ok(out.returncode == 0 and "004_change_tracking.sql" in out.stdout, "[v3] upgrade ran 004")
conn = psycopg.connect(DSN, autocommit=True)
ok(q(conn, "SELECT dedupe_hash, tender_id FROM tender_hash_history") == [(old_h, B[0])],
   "[v3] tender_hash_history backfilled from the deadline_changed event")
ok(dict(q(conn, "SELECT source_ref, origin FROM tender_source_ref")) == {"EXP-7": "primary", "TED-7": "hash_match", "FTS-7": "merge"},
   "[v3] ref origins backfilled (primary / hash_match / merge)")
ok(snapshot(conn)[1] == before, "[v3] 004 wrote no events")
r = ingest_tender(conn, {"source": "FTS", "title": "Mantenimiento de ecógrafos", "buyer": "Hospital Clínico San Carlos",
                         "deadline": d}, "fts-scraper")
ok(r.outcome == "matched_hash" and r.tender_id == B[0] and "deadline_at" in r.not_applied,
   "[v3] copy with the pre-amendment deadline matches via backfilled history, not applied (stale)")
bo = subprocess.run([sys.executable, os.path.join(HERE, "ingest.py"), "backfill-observed"], capture_output=True, text=True,
                    env={**os.environ, "DATABASE_URL": DSN})
ok(bo.returncode == 0 and "snapshotted 1 primary" in bo.stdout, f"[v3] ingest.py backfill-observed: {bo.stdout.strip()}")
obs = q(conn, "SELECT observed FROM tender_source_ref WHERE source_ref='EXP-7'")[0][0]
ok(obs["deadline_at"] == d.isoformat() and obs["title"] == "Mantenimiento de ecógrafos",
   "[v3] primary snapshot uses the pre-amendment deadline the platform last showed")
ok(q(conn, "SELECT count(*) FROM tender_source_ref WHERE NOT is_primary AND observed IS NOT NULL")[0][0] == 0,
   "[v3] secondary refs left without a snapshot")
r = ingest_tender(conn, {"source": "PLC", "source_ref": "EXP-7", "title": "Mantenimiento de ecógrafos",
                         "buyer": "Hospital Clínico San Carlos", "deadline": d}, "placsp-scraper")
ok(not r.changed and q(conn, "SELECT deadline_at FROM tender WHERE id=%s", B[0])[0][0] == new_dl,
   "[v3] first primary scrape after upgrade, still showing the old deadline, does not undo the v3 amendment")
bo = subprocess.run([sys.executable, os.path.join(HERE, "ingest.py"), "backfill-observed"], capture_output=True, text=True,
                    env={**os.environ, "DATABASE_URL": DSN})
ok("snapshotted 0" in bo.stdout, "[v3] backfill-observed is idempotent")
snap = snapshot(conn)
conn.close()
out = apply(DB)
conn = psycopg.connect(DSN, autocommit=True)
ok(out.returncode == 0 and snapshot(conn) == snap, f"[v3] re-running 002 + 003 + 004 changes nothing {snap}")
r = ingest_tender(conn, {"source": "TED", "source_ref": "TED-7", "title": "Mantenimiento de ecógrafos",
                         "buyer": "Hospital Clínico San Carlos", "deadline": new_dl}, "ted-scraper")
ok(r.outcome == "matched_source_ref" and not r.changed, "[v3] TED ref still matches after the upgrade")
conn.close()

# =========================== Scenario C: v4 database -> latest (005, 006) ===========================
DB = f"{PREFIX}_v4"
DSN = f"dbname={DB} host={HOST}"
out = apply(DB, "--fresh", upto="004")
ok(out.returncode == 0 and "004_change_tracking.sql" in out.stdout and "005" not in out.stdout,
   "[v4] built v4 database (… 004)")
conn = psycopg.connect(DSN, autocommit=True)
P = fc(conn, "PLC", "EXP-40", "Reactivos de hematología", "Hospital Universitario La Paz", d, "placsp-scraper")
F = fc(conn, "FTS", "FTS-40", "Haematology reagents", "La Paz University Hospital", d, "fts-scraper")
Z = fc(conn, "ZZX", "ZZ-1", "Plataforma regional desconocida", "Ayuntamiento de Ejemplo", d, "zz-scraper")  # v4 took any code
lot_p = child(conn, P[0], "L", source_lot_ref="Lote 1", title="Hemogramas")
lot_f = child(conn, F[0], "L", source_lot_ref="Lot 1", title="Full blood count")
lot_f2 = child(conn, F[0], "L", source_lot_ref="LOT-0002", title="Coagulation")
lot_m = child(conn, P[0], "L", title="Lote añadido a mano")      # no ref, no lot_added event -> manual
q(conn, "SELECT merge_tender(%s, %s, 'review:blaise')", F[0], P[0])   # v4 folding: 'Lot 1' <> 'Lote 1'
ok(q(conn, "SELECT count(*) FROM tender_lot WHERE tender_id=%s", P[0])[0][0] == 4,
   "[v4] v4 merge did not fold 'Lot 1' into 'Lote 1' (the old caveat): 4 lots on the survivor")
before = snapshot(conn)
conn.close()

out = apply(DB)
ok(out.returncode == 0 and "005_platforms_lots.sql" in out.stdout and "006_seed_platforms.sql" in out.stdout,
   "[v4] upgrade ran 005 and 006")
ok("source code ZZX is used in existing data but unknown" in out.stderr,
   "[v4] unknown code in existing data reported with a WARNING")
ok("keeps source_lot_ref" in out.stderr, "[v4] two lots of one tender with the same canonical ref reported with a WARNING")
conn = psycopg.connect(DSN, autocommit=True)
ok(snapshot(conn) == before, f"[v4] 005 keeps every row (tenders/events/refs/aliases/history/lots {before})")
plat = dict((c, (s, f is not None)) for c, s, f in q(conn, "SELECT code, status, frozen_at FROM source_platform "
                                                              "WHERE code IN ('PLC','FTS','TED','MAN','ZZX')"))
ok(plat == {"PLC": ("active", True), "FTS": ("active", True), "TED": ("active", False), "MAN": ("active", False),
            "ZZX": ("proposed", True)},
   f"[v4] registry: seeds active, used codes frozen, unknown ZZX proposed+frozen {plat}")
ok(q(conn, "SELECT count(*) FROM pg_constraint WHERE conname IN ('tender_source_platform_fk','tender_source_ref_platform_fk')")[0][0] == 2,
   "[v4] foreign keys tender.source / tender_source_ref.source -> source_platform added")
lots_now = {r[0]: r[1:] for r in q(conn, "SELECT id, source_lot_ref, source_lot_label, lot_origin FROM tender_lot")}
ok(lots_now[lot_p[0]] == ("1", "Lote 1", "structured") and lots_now[lot_f2[0]] == ("2", "LOT-0002", "structured")
   and lots_now[lot_m[0]] == (None, None, "manual") and lots_now[lot_f[0]] == ("Lot 1", "Lot 1", "structured"),
   f"[v4] lot labels kept, refs canonicalised, origins backfilled; the clashing 'Lot 1' left as is {lots_now}")
again = add_lot(conn, P[0], title="Hemogramas", source_lot_ref="LOT-0001", actor="ted-scraper", source="TED")
ok(again.outcome == "existing" and again.id == lot_p[0], "[v4] after upgrade 'LOT-0001' finds the old 'Lote 1' lot")
try:
    ingest_tender(conn, {"source": "ZZX", "source_ref": "ZZ-2", "title": "Otra", "deadline": d}, "zz-scraper")
    ok(False, "[v4] proposed platform rejected")
except IngestError as e:
    ok("proposed, not active" in str(e), f"[v4] new tender from the proposed ZZX platform rejected: {e}")
snap = snapshot(conn)
conn.close()
out = apply(DB)
conn = psycopg.connect(DSN, autocommit=True)
ok(out.returncode == 0 and snapshot(conn) == snap and "unknown: registered" not in out.stderr,
   "[v4] re-running 002 … 006 changes nothing and warns no more about ZZX")
q(conn, "UPDATE source_platform SET status='active', name='Plataforma regional (example)' WHERE code='ZZX' RETURNING code")
r = ingest_tender(conn, {"source": "ZZX", "source_ref": "ZZ-1", "title": "Plataforma regional desconocida",
                         "buyer": "Ayuntamiento de Ejemplo", "deadline": d}, "zz-scraper")
ok(r.outcome == "matched_source_ref" and r.tender_id == Z[0], "[v4] after activation ZZX scrapes match as before")
# =========================== Scenario D: populated v5 database -> 006 ===========================
import csv  # noqa: E402
import platforms  # noqa: E402
DB = f"{PREFIX}_v5"
DSN = f"dbname={DB} host={HOST}"
LIST = {r["approved_code"]: r for r in csv.DictReader(open(os.path.join(HERE, "platforms", "proposals.csv")))}
by_dom = {r["domain"]: r["approved_code"] for r in LIST.values() if not r["tenant_key"]}
out = apply(DB, "--fresh", upto="005")
ok(out.returncode == 0 and "005_platforms_lots.sql" in out.stdout and "006" not in out.stdout, "[v5] built v5 database (… 005)")
conn = psycopg.connect(DSN, autocommit=True)
# what a v5 user may already have: platforms added with `platforms.py apply` (v5 columns only)
cf_list = by_dom["contractsfinder.service.gov.uk"]            # the list proposes another code for Contracts Finder
tn_code = by_dom["tenderned.nl"]
sam_code = by_dom["sam.gov"]
q(conn, "INSERT INTO source_platform (code, name, url, domain, country, status, priority, code_origin) VALUES "
        "('CFN','Contracts Finder (renamed by Blaise)','https://www.contractsfinder.service.gov.uk','contractsfinder.service.gov.uk','GB','active',60,'manual'),"
        "(%s,'TenderNed','https://www.tenderned.nl','tenderned.nl','NL','proposed',40,'suggested'),"
        "(%s,'Some other SAM platform','https://sam.example','sam.example',NULL,'active',50,'manual') RETURNING code",
  tn_code, sam_code)
q(conn, "UPDATE source_platform SET priority=95 WHERE code='TED' RETURNING code")       # a local priority tweak
P = fc(conn, "PLC", "EXP-50", "Reactivos de bioquímica", "Hospital del Mar", d, "placsp-scraper")
C = ingest_tender(conn, {"source": "CFN", "source_ref": "CF-1", "title": "Biochemistry reagents", "buyer": "NHS Leeds",
                         "deadline": d}, "cf-scraper")
lot = add_lot(conn, P[0], title="Glucosa", source_lot_ref="Lote 1", actor="placsp-scraper", source="PLC")
before = snapshot(conn)
reg_before = {r[0]: r[1:] for r in q(conn, "SELECT code, name, url, domain, priority, status, frozen_at FROM source_platform")}
conn.close()

out = apply(DB)
ok(out.returncode == 0 and "006_seed_platforms.sql" in out.stdout and "005_platforms_lots.sql" in out.stdout,
   "[v5] upgrade ran 002 … 006 (005 re-run is a no-op)")
ok(f"code {sam_code} is already used by Some other SAM platform" in out.stderr and "(sam.gov) was NOT registered" in out.stderr,
   "[v5] list code already used by a different platform -> WARNING, that list row not registered")
ok(f"{cf_list}=CFN" in out.stderr, f"[v5] Contracts Finder already registered as CFN (list says {cf_list}) -> kept, WARNING")
conn = psycopg.connect(DSN, autocommit=True)
ok(snapshot(conn) == before, f"[v5] 006 keeps every tender/event/ref/lot row {before}")
reg = {r[0]: r[1:] for r in q(conn, "SELECT code, name, url, domain, priority, status, frozen_at FROM source_platform")}
ok(all(reg[c][:4] == v[:4] and reg[c][5] == v[5] for c, v in reg_before.items()),
   "[v5] existing platforms keep code, name, URL, domain, priority and frozen_at (CFN, PLC frozen; TED priority 95)")
ok(reg["CFN"][5] is not None and reg["PLC"][5] is not None and cf_list not in reg, "[v5] frozen CFN kept; no second code for its site")
ok(reg[tn_code][4] == "active" and reg[tn_code][3] == 40, f"[v5] proposed {tn_code} (TenderNed) activated, its priority untouched")
expected = len(LIST) - 2 + 2 + 1                     # list - (CF under CFN, SAM.gov skipped) + CFN + sam.example + MAN
ok(len(reg) == expected and all(s == "active" for *_, s, _f in reg.values()),
   f"[v5] {len(reg)} platforms, all active (list {len(LIST)} - 2 conflicts + CFN + other SAM + MAN)")
ok(q(conn, "SELECT count(*) FROM pg_indexes WHERE indexname='source_platform_site_uq'")[0][0] == 1
   and q(conn, "SELECT count(*) FROM pg_indexes WHERE indexname='source_platform_domain_uq'")[0][0] == 0,
   "[v5] unique by (domain, tenant_key) instead of domain")
R = lambda u: platforms.resolve_platform(conn, u)
ok(R("https://www.contractsfinder.service.gov.uk/Notice/1").code == "CFN" and R("https://www.tenderned.nl/x").code == tn_code
   and R("https://sam.example").code == sam_code and unresolved(conn, "https://sam.gov/opp/1"),
   "[v5] URLs resolve to the kept codes; the skipped SAM.gov is not registered")
lid = by_dom.get("in-tendhost.co.uk")
ten = next(r for r in LIST.values() if r["tenant_key"] and r["domain"] == "in-tendhost.co.uk")
ok(R(f"https://{ten['domain']}{ten['tenant_key']}/aspx/Tenders/Current").code == ten["approved_code"],
   f"[v5] tenant on a shared host resolves by its path ({ten['tenant_key']} -> {ten['approved_code']}; site code {lid})")
r = ingest_tender(conn, {"source": tn_code, "source_ref": "TN-1", "title": "Laboratoriumreagentia", "deadline": d}, "tn-scraper")
ok(r.outcome == "created", f"[v5] new list platform {tn_code} accepts tenders after 006")
r = ingest_tender(conn, {"source": "CFN", "source_ref": "CF-1", "title": "Biochemistry reagents", "buyer": "NHS Leeds",
                         "deadline": d}, "cf-scraper")
ok(r.outcome == "matched_source_ref" and r.tender_id == C.tender_id, "[v5] old CFN scrapes still match")
snap, regn = snapshot(conn), q(conn, "SELECT count(*), md5(string_agg(code||status||coalesce(tenant_key,''), ',' ORDER BY code)) FROM source_platform")
conn.close()
out = apply(DB)
conn = psycopg.connect(DSN, autocommit=True)
ok(out.returncode == 0 and snapshot(conn) == snap
   and q(conn, "SELECT count(*), md5(string_agg(code||status||coalesce(tenant_key,''), ',' ORDER BY code)) FROM source_platform") == regn,
   "[v5] re-running 006 changes nothing")
conn.close()
print(f"\nALL {len(PASSED)} UPGRADE CHECKS PASSED")
