"""Platform registry + lot tests (005). Safe to run on a fresh database or after the other tests:
    DATABASE_URL="dbname=trc_v5_test host=/var/run/postgresql" python test_platforms_lots.py
Creates platforms with codes it picks from the free Q??/X?? range (registry codes are permanent
once used, so a re-run picks new ones) and names/domains carrying a run id.
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lots  # noqa: E402
import platforms  # noqa: E402
from ingest import (IngestError, add_lot, confirm_lot, ingest_lots, ingest_tender,  # noqa: E402
                    resolve_source)

DSN = os.environ.get("DATABASE_URL", "dbname=trc_v5_test host=/var/run/postgresql")
RUN = uuid.uuid4().hex[:6]
FIX = os.path.join(HERE, "tests", "fixtures")
PASSED: list[str] = []


def ok(cond, msg):
    if not cond:
        print(f"FAIL  {msg}")
        raise SystemExit(1)
    PASSED.append(msg)
    print(f"PASS  {msg}")


def raises(fn, exc, text=None):
    try:
        fn()
    except exc as e:
        return text is None or text in str(e)
    return False


conn = psycopg.connect(DSN, autocommit=True)


def q(sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else cur.rowcount


def free_code(prefix="Q") -> str:
    used = {r[0] for r in q("SELECT code FROM source_platform")}
    for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            c = prefix + a + b
            if c not in used and c not in platforms.BLOCKLIST:
                return c
    raise RuntimeError("no free code")


def new_platform(status="active", **kw) -> str:
    code = kw.pop("code", None) or free_code()
    q("INSERT INTO source_platform (code, name, url, domain, status, priority) VALUES (%s,%s,%s,%s,%s,%s)",
      code, kw.get("name", f"Test platform {code} {RUN}"), kw.get("url"), kw.get("domain"), status, kw.get("priority", 10))
    return code


def cli(*args, env_extra=None):
    env = {**os.environ, "DATABASE_URL": DSN, "TRC_ACTOR": "review:blaise", **(env_extra or {})}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, cwd=HERE)


d = datetime(2027, 5, 1, 12, 0, tzinfo=timezone.utc)

# ============================== 1. code suggester (pure) ==============================
SEED = {"PLC", "FTS", "TED", "MAN"}
cases = [("Plataforma de Contratación del Sector Público", "https://contrataciondelestado.es", "PLACSP"),
         ("Public Contracts Scotland", None, None), ("SAM.gov", "https://sam.gov", None),
         ("Contract Opportunities", "https://sam.gov/content/opportunities", None),
         ("Some Platform", "https://ted.europa.eu", None), ("eTendersNI", None, None), ("Mercell", None, None)]
first = [platforms.suggest_code(n, u, set(), s) for n, u, s in cases]
again = [platforms.suggest_code(n, u, set(), s) for n, u, s in cases]
ok([(x.code, x.alternatives, x.reason) for x in first] == [(x.code, x.alternatives, x.reason) for x in again],
   "suggester is deterministic (same input, same code, alternatives and reason)")
taken = ["PLC", "PCS", "PBC", "XYZ", "SAM"]
a1 = platforms.suggest_code("Public Contracts Scotland", None, taken)
random.Random(7).shuffle(taken)
a2 = platforms.suggest_code("Public Contracts Scotland", None, set(taken))
ok(a1.code == a2.code == "PBS" and a1.alternatives == a2.alternatives,
   f"result does not depend on the order of `taken` (PCS, PBC taken -> {a1.code})")
ok([x.code for x in first] == ["PLC", "PCS", "SAM", "SAM", "TED", "ETN", "MRC"],
   f"short name PLACSP -> PLC, initials PCS, acronym SAM, domain label sam/ted, camelCase eTendersNI -> ETN, "
   f"one word Mercell -> MRC: {[x.code for x in first]}")
ok(first[0].method == "short_name" and first[2].method == "acronym" and first[3].method == "domain"
   and first[4].method == "domain" and "ted.europa.eu" in first[4].reason, "method and reason explain each choice")
b = platforms.suggest_code("Association Sanitaire Suisse", None, set())
ok(b.code != "ASS" and b.skipped.get("ASS") == "blocklisted" and "ASS blocklisted" in b.reason,
   f"blocklisted ASS avoided -> {b.code} ({b.reason})")
m = platforms.suggest_code("Manual Award Notices", None, set())
t = platforms.suggest_code("Tender Records Centre", None, set())
ok(m.code != "MAN" and m.skipped.get("MAN") == "reserved" and t.code != "TRC" and t.skipped.get("TRC") == "reserved",
   f"reserved MAN / TRC avoided -> {m.code}, {t.code}")
c = platforms.suggest_code("Public Contracts Scotland", None, SEED | {"PCS"})
ok(c.code != "PCS" and c.preferred == "PCS" and c.alternatives and "PCS taken" in c.reason,
   f"taken code -> next alternative {c.code}, ranked alternatives {c.alternatives}")
f = platforms.suggest_code("Abc", None, {"ABC"} | {"AB" + x for x in "ABCDEFGHIJ"})
ok(f.code == "ABK" and f.method == "fallback" and f.weak,
   f"last resort: first two letters + letter (codes are letters only) -> {f.code}, marked weak")
ok(raises(lambda: platforms.suggest_code("  ", None), platforms.PlatformError), "empty name rejected")
ok(platforms.domain_of("HTTPS://WWW.Ted.Europa.EU:8443/en/notice?x=1") == "ted.europa.eu"
   and platforms.domain_of("www2.boamp.fr") == "boamp.fr" and platforms.domain_of("not a url") is None,
   "domain_of: case, www, port, path, bare host, junk")

# ============================== 2. proposals: clashes and duplicates (pure) ==============================
registered = [{"code": "PLC", "name": "Plataforma de Contratación del Sector Público", "domain": "contrataciondelestado.es", "status": "active"},
              {"code": "FTS", "name": "Find a Tender", "domain": "find-tender.service.gov.uk", "status": "active"},
              {"code": "CFN", "name": "Some other platform", "domain": "other.example", "status": "retired"}]
rows = [{"name": "Contracts Finder", "url": "https://www.contractsfinder.service.gov.uk", "row": 2},
        {"name": "Public Contracts Scotland", "url": "https://www.publiccontractsscotland.gov.uk", "row": 3},
        {"name": "Public Contracts Somewhere", "url": "https://pcs.example", "row": 4},
        {"name": "Find a Tender service", "url": "https://www.find-tender.service.gov.uk/Search", "row": 5},
        {"name": "Plataforma de Contratacion del Sector Publico", "url": "https://contrataciondelsectorpublico.gob.es", "row": 6},
        {"name": "Scotland contracts portal", "url": "https://publiccontractsscotland.gov.uk/search", "row": 7},
        {"name": "Public Contracts Scotland", "url": "", "row": 8},
        {"name": "Portal BASE", "url": "https://www.base.gov.pt", "country": "UK", "row": 9},
        {"name": "Mercell", "url": "https://www.mercell.com", "data_format": "xml", "row": 10}]
props = platforms.build_proposals(rows, registered)
st = {p["source_row"]: (p["status"], p["suggested_code"]) for p in props}
ok(st[2][0] == "clash_with_existing" and st[2][1] != "CFN" and "CFN is already used by Some other platform (retired)" in props[0]["clash_details"],
   f"code already used by a different (even retired) platform in the DB -> clash_with_existing, alternative {st[2][1]}")
# v6: the strongest claim wins (short name / acronym / exact domain label beat initials), file order breaks ties;
# here pcs.example's domain label beats the initials of row 3, which then takes the next alternative
ok(st[4] == ("ok", "PCS") and st[3][0] == "clash_in_file" and st[3][1] != "PCS"
   and "also wanted by row 4" in props[1]["clash_details"] and "row 3" in props[2]["clash_details"],
   f"two input rows wanting PCS -> the stronger claim (domain label) wins, the other is clash_in_file ({st[3][1]}), noted on both")
ok(st[5] == ("duplicate_platform", "FTS") and "same domain as registered platform FTS" in props[3]["clash_details"],
   "same domain as a registered platform -> duplicate_platform with the existing code")
ok(st[6] == ("duplicate_platform", "PLC") and "near-identical name" in props[4]["clash_details"],
   "near-identical name (accents aside) as a registered platform -> duplicate_platform")
ok(st[7][0] == "duplicate_platform" and "same domain as row 3" in props[5]["clash_details"]
   and st[8][0] == "duplicate_platform" and "near-identical name as row 3" in props[6]["clash_details"],
   "same domain / same name twice in the file -> duplicate_platform")
ok(st[9][0] == "needs_review" and "use GB" in props[7]["clash_details"] and "not based on clear initials" in props[7]["clash_details"]
   and st[10][0] == "needs_review" and "data_format 'xml'" in props[8]["clash_details"],
   "weak initials (BASE -> padded), non-ISO country UK, unknown data_format -> needs_review")

# ============================== 3. reading CSV / XLSX ==============================
rows_m, msgs = platforms.read_rows(os.path.join(FIX, "platforms_input_messy.csv"))
# v6: a nameless row with a URL is kept, named after its domain and flagged (v5 skipped it)
ok([r["name"] for r in rows_m] == ["Find a Tender", "Public Contracts Scotland", "Public Contracts Scotland", "nameless.example", "BOAMP"]
   and rows_m[3]["flags"] == ["no name in the list; named after the domain"]
   and rows_m[0]["url"].startswith("https://www.find-tender") and rows_m[0]["country"] == "UK",
   "messy CSV: ';' delimiter, header synonyms (Platform Name/Website/Abbreviation/Format/Comment) mapped")
ok(any("optional columns not found: priority" in m for m in msgs) and any("row 5: no name" in m for m in msgs),
   f"missing optional columns and nameless rows reported: {msgs}")
try:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["NAME", "URL", "Short Name", "Colour"])
    ws.append(["TenderNed", "https://www.tenderned.nl", None, "orange"])
    ws.append(["Sell2Wales", "https://www.sell2wales.gov.wales", None, None])
    xp = os.path.join(tempfile.mkdtemp(), "platforms.xlsx")
    wb.save(xp)
    rows_x, msgs_x = platforms.read_rows(xp)
    ok([r["name"] for r in rows_x] == ["TenderNed", "Sell2Wales"] and any("ignored columns: Colour" in m for m in msgs_x),
       "XLSX input read (case-insensitive headers, unknown column reported)")
except ImportError:
    ok(True, "openpyxl not installed: XLSX test skipped")
ok(raises(lambda: platforms.map_headers(["url", "country"]), platforms.PlatformError, "no 'name' column"),
   "a file without a name column is refused")

# ============================== 4. suggest/apply through the CLI and the DB ==============================
tmp = tempfile.mkdtemp()
os.chmod(tmp, 0o777)
inp = os.path.join(tmp, "in.csv")
want = free_code("Q")                                        # both names want these free initials
RL = "".join(chr(97 + int(ch, 16)) for ch in RUN)        # run-unique letters: names of earlier runs are not near-identical
c1 = f"{want[0]}{RL}ilo {want[1].lower()}{RL}arbor {want[2]}elta Network".title()
c2 = f"{want[0]}uma{RL} {want[1].lower()}stra{RL} {want[2]}ivo Group".title()
with open(inp, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Name", "URL", "Country", "Priority"])
    w.writerow([c1, f"https://www.one-{RUN}.example", "ES", "20"])
    w.writerow([c2, f"https://two-{RUN}.example", "FR", ""])
    w.writerow(["Find a Tender", "https://www.find-tender.service.gov.uk", "GB", ""])
propfile = os.path.join(tmp, "proposals.csv")
out = cli("platforms.py", "suggest", inp, "--out", propfile)
ok(out.returncode == 0 and "1 duplicate_platform" in out.stdout and "1 clash_in_file" in out.stdout
   and f"{want}  {c1}" in out.stdout,
   "platforms.py suggest compares with the DB and the file: "
   + next((l for l in out.stdout.splitlines() if "platform(s):" in l), out.stdout + out.stderr))
props = list(csv.DictReader(open(propfile)))
ok(list(props[0].keys())[:8] == ["name", "url", "domain", "suggested_code", "alternatives", "reason", "status", "clash_details"]
   and "approved_code" in props[0], "proposals CSV has the documented columns plus approved_code")
code1, code2 = props[0]["suggested_code"], props[1]["suggested_code"]
out = cli("platforms.py", "apply", propfile)
ok(out.returncode == 0 and f"inserted: {code1}" in out.stdout and "status clash_in_file" in out.stdout
   and "status duplicate_platform" in out.stdout,
   "apply inserts only 'ok' rows, skips clash/duplicate rows without approved_code")
ok(q("SELECT status, code_origin, priority, country, domain FROM source_platform WHERE code=%s", code1)
   == [("proposed", "suggested", 20, "ES", f"one-{RUN}.example")], "inserted as proposed, origin suggested, CSV extras kept")
out = cli("platforms.py", "apply", propfile)
ok(out.returncode == 0 and f"already: {code1} (proposed)" in out.stdout and "inserted" not in out.stdout,
   "apply is idempotent (same domain + same code -> already registered)")
# a person resolves the clash: approved_code = an existing code -> refused; a free code -> accepted
rows_p = list(csv.DictReader(open(propfile)))
rows_p[1]["approved_code"] = "FTS"
with open(propfile, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=rows_p[0].keys()); w.writeheader(); w.writerows(rows_p)
out = cli("platforms.py", "apply", propfile)
ok(out.returncode == 1 and "code FTS is already used by Find a Tender" in out.stdout, "apply refuses an approved code that clashes with the DB")
manual = free_code("X")
rows_p[1]["approved_code"] = manual
rows_p[2]["approved_code"] = manual               # same code twice in the file
with open(propfile, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=rows_p[0].keys()); w.writeheader(); w.writerows(rows_p)
out = cli("platforms.py", "apply", propfile, "--activate")
ok(out.returncode == 1 and f"inserted: {manual}" in out.stdout and f"{manual} is also used by row" in out.stdout
   and f"activated: {code1}" in out.stdout,
   "approved_code accepted (manual origin), the same code twice in one file refused, --activate activates")
ok(q("SELECT status, code_origin FROM source_platform WHERE code=%s", manual) == [("active", "manual")],
   "approved_code row registered active with code_origin manual")
rows_p[1]["approved_code"] = "TRC"
with open(propfile, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=rows_p[0].keys()); w.writeheader(); w.writerows(rows_p[1:2])
out = cli("platforms.py", "apply", propfile)
ok(out.returncode == 1 and "TRC is reserved" in out.stdout, "apply refuses reserved codes")
out = cli("platforms.py", "list")
ok(out.returncode == 0 and code1 in out.stdout and manual in out.stdout, "platforms.py list")

# ============================== 5. frozen / retired rules (DB triggers) ==============================
fresh = new_platform(status="proposed", domain=f"fresh-{RUN}.example")
newc = free_code()
q("UPDATE source_platform SET code=%s WHERE code=%s", newc, fresh)
ok(q("SELECT count(*) FROM source_platform WHERE code=%s", newc)[0][0] == 1, "an unused, unfrozen code can still be corrected")
ok(q("DELETE FROM source_platform WHERE code=%s", newc) == 1, "an unused, unfrozen code can be deleted")
used = new_platform(domain=f"used-{RUN}.example")
ok(q("SELECT frozen_at FROM source_platform WHERE code=%s", used)[0][0] is None, "new platform not frozen yet")
r = ingest_tender(conn, {"source": used, "source_ref": f"U-{RUN}", "title": f"Frozen code test {RUN}", "deadline": d}, "test")
ok(r.accession_no.split("-")[2] == used and q("SELECT frozen_at FROM source_platform WHERE code=%s", used)[0][0] is not None,
   f"first tender with the code freezes it ({r.accession_no})")


def db_error(sql, *args):
    try:
        q(sql, *args)
    except psycopg.Error as e:
        return e.diag.message_primary
    return None


ok("is frozen" in (db_error("UPDATE source_platform SET code=%s WHERE code=%s", free_code(), used) or ""),
   "frozen code cannot be changed")
ok("cannot be deleted" in (db_error("DELETE FROM source_platform WHERE code=%s", used) or ""), "frozen code cannot be deleted")
ok("set automatically" in (db_error("UPDATE source_platform SET frozen_at=NULL WHERE code=%s", used) or ""),
   "frozen_at cannot be cleared")
out = cli("platforms.py", "rename", used, "--name", f"Renamed {RUN}", "--url", f"https://www.renamed-{RUN}.example/x")
ok(out.returncode == 0 and q("SELECT name, domain FROM source_platform WHERE code=%s", used) == [(f"Renamed {RUN}", f"renamed-{RUN}.example")],
   "renaming name/url of a frozen platform is allowed (domain recomputed)")
ok("cannot be truncated" in (db_error("TRUNCATE source_platform CASCADE") or ""), "TRUNCATE of the registry is refused")
ret = new_platform(domain=f"retire-{RUN}.example")
out = cli("platforms.py", "retire", ret)
ok(out.returncode == 0 and q("SELECT status FROM source_platform WHERE code=%s", ret)[0][0] == "retired", "platforms.py retire")
out = cli("platforms.py", "activate", ret)
ok(out.returncode == 2 and "cannot be reactivated" in out.stderr, "a retired platform cannot be reactivated")
ok("retired" in (db_error("DELETE FROM source_platform WHERE code=%s", ret) or ""), "a retired code cannot be deleted (so never reused)")
ok("retired" in (db_error("UPDATE source_platform SET code=%s WHERE code=%s", free_code(), ret) or ""), "a retired code cannot be renamed away")
ok(db_error("INSERT INTO source_platform (code, name) VALUES (%s, 'reuse')", ret) is not None, "inserting the retired code again fails")
reuse = [{"name": "Reuse attempt", "url": "https://reuse.example", "row": 2}]
ok(platforms.build_proposals(reuse, [{"code": ret, "name": "x", "domain": None, "status": "retired"}])
   and all(p["suggested_code"] != ret for p in platforms.build_proposals(
       [{"name": f"Retired {ret}", "url": "", "short_name": ret, "row": 2}],
       [{"code": ret, "name": "x", "domain": None, "status": "retired"}])),
   "the suggester never proposes a retired code")
ok("source_platform_code_format" in (db_error("INSERT INTO source_platform (code, name) VALUES ('ab1', 'bad')") or ""),
   "code format ^[A-Z]{3}$ enforced")
ok(db_error("INSERT INTO source_platform (code, name, domain) VALUES (%s, 'dup domain', %s)", free_code(), f"renamed-{RUN}.example") is not None,
   "domain is unique")

# ============================== 6. resolve_platform + ingestion lookup ==============================
sub = new_platform(domain=f"plat-{RUN}.example", name=f"Plataforma Ejemplo {RUN}")
prop = new_platform(status="proposed", domain=f"proposed-{RUN}.example")
sub2 = new_platform(domain=f"eu.plat-{RUN}.example", name=f"Plataforma Ejemplo EU {RUN}")
R = platforms.resolve_platform
ok(R(conn, url=f"https://www.plat-{RUN}.example:8443/licitacion?id=1").code == sub
   and R(conn, url=f"http://buyers.plat-{RUN}.example/x").code == sub
   and R(conn, url=f"https://notices.eu.plat-{RUN}.example/").code == sub2,
   "resolve_platform by URL: www, port, subdomains; the most specific registered domain wins")
ok(R(conn, code=sub.lower()).code == sub and R(conn, name=f"plataforma ejemplo {RUN}".upper()).code == sub
   and R(conn, name="PLACSP").code == "PLC" and R(conn, name="find-a-tender").code == "FTS",
   "resolve_platform by code, by exact name or short name (case/punctuation ignored)")
ok(raises(lambda: R(conn, url="https://unknown-platform.example"), platforms.PlatformError, "no registered platform for domain"),
   "unknown domain -> clear error (no code invented)")
ok(raises(lambda: R(conn, code=ret), platforms.PlatformError, "is retired, not active")
   and raises(lambda: R(conn, code=prop), platforms.PlatformError, "proposed, not active")
   and raises(lambda: R(conn, url=f"https://proposed-{RUN}.example/x"), platforms.PlatformError, "proposed, not active"),
   "retired / proposed platforms are not resolved for ingestion")
r1 = ingest_tender(conn, {"source_url": f"https://licitaciones.plat-{RUN}.example/detalle/77", "source_ref": f"E-{RUN}",
                          "title": f"Servicio de limpieza {RUN}", "buyer": "Ayuntamiento", "deadline": d}, "plat-scraper")
ok(r1.created and r1.accession_no.split("-")[2] == sub, f"ingest by source_url only -> {r1.accession_no}")
r2 = ingest_tender(conn, {"platform": f"Plataforma Ejemplo {RUN}", "source_ref": f"E-{RUN}",
                          "title": f"Servicio de limpieza {RUN}", "buyer": "Ayuntamiento", "deadline": d}, "plat-scraper")
ok(r2.outcome == "matched_source_ref" and r2.tender_id == r1.tender_id, "ingest by platform name matches the same tender")
ok(raises(lambda: ingest_tender(conn, {"source_url": "https://nowhere.example/x", "title": "x"}, "t"), IngestError, "no registered platform"),
   "ingest from an unknown platform URL rejected")
ok(raises(lambda: ingest_tender(conn, {"source": "QQQ" if "QQQ" not in {x[0] for x in q("SELECT code FROM source_platform")} else "ZQZ",
                                       "title": "x"}, "t"), IngestError, "no registered platform named"),
   "ingest with an unknown 3-letter code rejected")
ok(raises(lambda: ingest_tender(conn, {"source": prop, "title": "x"}, "t"), IngestError, "proposed, not active"),
   "ingest from a proposed platform rejected")
ok(raises(lambda: ingest_tender(conn, {"source": "TED", "source_url": f"https://plat-{RUN}.example/", "title": "x"}, "t"),
          IngestError, "disagree"), "source and source_url pointing at different platforms rejected")
ok(resolve_source(conn, {"source": "PLC", "source_url": "https://some-document-host.example/pliego.pdf"}) == "PLC",
   "a source_url on an unregistered host does not override a known source")
ok("not active" in (db_error("INSERT INTO tender (source, dedupe_hash, title) VALUES (%s, 'h', 'direct')", prop) or ""),
   "database also refuses a direct INSERT for a non-active platform")
ok("violates foreign key" in (db_error("INSERT INTO tender_source_ref (tender_id, source, source_ref) VALUES (%s, 'ZQQ', 'x')", r1.tender_id) or "violates foreign key")
   or "unknown platform code" in (db_error("INSERT INTO tender_source_ref (tender_id, source, source_ref) VALUES (%s, 'ZQQ', 'x')", r1.tender_id) or ""),
   "tender_source_ref.source must be a registered platform")

# ============================== 7. priorities come from the registry ==============================
hi = new_platform(domain=f"hi-{RUN}.example", priority=10)
base = ingest_tender(conn, {"source": "PLC", "source_ref": f"PR-{RUN}", "title": f"Prioridad {RUN}", "buyer": "B", "deadline": d}, "plc")
m1 = ingest_tender(conn, {"source": hi, "source_ref": f"H-{RUN}", "title": f"Prioridad {RUN}", "buyer": "B", "deadline": d}, "hi")
m2 = ingest_tender(conn, {"source": hi, "source_ref": f"H-{RUN}", "title": f"Prioridad {RUN} (v2)", "buyer": "B", "deadline": d}, "hi")
ok(m1.tender_id == base.tender_id and "title" in m2.not_applied, "priority 10 platform cannot overwrite a PLC (100) tender")
out = cli("platforms.py", "update", hi, "--priority", "100")
m3 = ingest_tender(conn, {"source": hi, "source_ref": f"H-{RUN}", "title": f"Prioridad {RUN} (v3)", "buyer": "B", "deadline": d}, "hi")
ok(out.returncode == 0 and "title" in m3.changed and q("SELECT title FROM tender WHERE id=%s", base.tender_id)[0][0] == f"Prioridad {RUN} (v3)",
   "after `platforms.py update --priority 100` the same platform may overwrite (read from source_platform)")
ev = q("SELECT payload->'precedence'->'priorities' FROM tender_event WHERE tender_id=%s AND event_type='fields_changed' ORDER BY id DESC LIMIT 1", base.tender_id)[0][0]
ok(ev == {"PLC": 100, hi: 100}, f"fields_changed event records the priorities used {ev}")

# ============================== 8. lot parsers on the fixtures ==============================
atom = open(os.path.join(FIX, "placsp_codice_sample.atom"), "rb").read()
ok(raises(lambda: lots.lots_from_codice(atom), ValueError, "pass folder_id"), "CODICE feed with 2 tenders needs folder_id")
cl = lots.lots_from_codice(atom, folder_id="SAMPLE/EXP-900")
ok([(x["source_lot_ref"], x["title"], x["estimated_value"], x["currency"]) for x in cl] ==
   [("1", "Anticuerpos monoclonales para citometría", Decimal("60000.00"), "EUR"),
    ("2", "Kits de extracción de ácidos nucleicos", Decimal("90000.00"), "EUR"),
    ("3", "Material fungible", Decimal("15000.00"), "EUR")]
   and cl[0]["cpv_codes"] == ["33651520", "33696500"] and cl[2]["cpv_codes"] is None and all(x["origin"] == "structured" for x in cl),
   "CODICE: 3 lots, TaxExclusiveAmount preferred (else EstimatedOverall), currency, CPV codes")
ok(lots.lots_from_codice(atom, folder_id="SAMPLE/EXP-901") == [], "CODICE tender without lots -> []")
ef = lots.lots_from_eforms(open(os.path.join(FIX, "ted_eforms_sample.xml"), encoding="utf-8").read())
ok([(x["source_lot_ref"], x["source_lot_label"], x["title"]) for x in ef] ==
   [("1", "LOT-0001", "Monoclonal antibodies for flow cytometry"), ("2", "LOT-0002", "Nucleic acid extraction kits"),
    ("3", "LOT-0003", "Verbrauchsmaterial")]
   and ef[0]["cpv_codes"] == ["33651520", "33696500"] and ef[0]["estimated_value"] == Decimal("60000.00")
   and ef[0]["description"] == "Antibody panels, delivered monthly." and ef[2]["estimated_value"] is None,
   "eForms: LotsGroup skipped, ENG name preferred over FRA, other language as fallback, value/CPV")
oc = lots.lots_from_ocds(open(os.path.join(FIX, "fts_ocds_sample.json")).read())
ok([(x["source_lot_ref"], x["title"], x["estimated_value"], x["currency"], x["cpv_codes"]) for x in oc] ==
   [("1", "Monoclonal antibodies", Decimal("60000.00"), "GBP", ["33651520", "33696500"]),
    ("2", "Nucleic acid extraction kits", Decimal("90000.00"), "GBP", ["33696300"]),
    ("3", "Consumables", Decimal("15000.00"), "GBP", ["33140000"])],
   "OCDS: latest release used (3 lots), CPV from items[].relatedLot incl. additionalClassifications, non-CPV ignored")
rec = {"records": [{"compiledRelease": json.load(open(os.path.join(FIX, "fts_ocds_sample.json")))["releases"][0]}]}
ok(len(lots.lots_from_ocds(rec)) == 2, "OCDS record package (compiledRelease) supported")
ok(all(fn(x) == [] for fn in (lots.lots_from_codice, lots.lots_from_eforms) for x in (None, "", b"  ", "<a/>", "<not xml"))
   and all(lots.lots_from_ocds(x) == [] for x in (None, "", "{}", "[oops", {"releases": []}, {"releases": [{"tender": {}}]}))
   and lots.HeuristicTextLotExtractor().extract("") == [] and lots.HeuristicTextLotExtractor().extract(None) == [],
   "empty / missing / unparseable input -> []")
x = lots.HeuristicTextLotExtractor()
es = x.extract(open(os.path.join(FIX, "tender_document_es.txt"), encoding="utf-8").read())
en = x.extract(open(os.path.join(FIX, "tender_document_en.txt"), encoding="utf-8").read())
ok([(l["source_lot_ref"], l["source_lot_label"], l["title"], l["estimated_value"], l["currency"]) for l in es] ==
   [("1", "Lote 1", "Reactivos de bioquímica clínica", Decimal("120000.00"), "EUR"),
    ("2", "Lote 2", "Material fungible de laboratorio", Decimal("45500.00"), "EUR"),
    ("3", "Lote nº 3", "Mantenimiento preventivo de analizadores", None, None)]
   and es[0]["description"].startswith("Incluye reactivos") and all(l["origin"] == "ai_extracted" for l in es),
   "text heuristics (es): 'Lote 1:', 'Lote 2 -', 'Lote nº 3'; values 120.000,00 EUR / 45.500,00 €; mentions in prose ignored; repeat in summary ignored")
ok([(l["source_lot_ref"], l["title"], l["estimated_value"], l["currency"]) for l in en] ==
   [("4", "Cold-chain logistics", Decimal("80000.00"), "GBP"), ("5", "Staff training on new analysers", None, None)]
   and x.mentions_lots("The contract is divided into the following lots:"),
   "text heuristics (en): bullets, 'LOT 4 -', 'Lot 5:', £80,000; 'divided into the following lots' detected")
ok(issubclass(lots.HeuristicTextLotExtractor, lots.TextLotExtractor) and x.name == "heuristic-regex-v1"
   and raises(lambda: lots.extract_lots("x", "pdf"), ValueError, "HeuristicTextLotExtractor"),
   "extractor interface for a future LLM extractor; extract_lots dispatches only structured formats")

# ============================== 9. canonical lot refs (Python == SQL) ==============================
labels = ["Lote 1", "Lot 1", "LOT-0001", "1", "01", "Lot nº 1", "Lote Nº1", "lot no. 1", "LOTE NÚM. 1", "Lotto 1", "Los 1",
          "Lot 1 - Hardware", "Lot 1a", "Lot A", "Lot A - Software", "Lot North", "Hardware maintenance", "Partida única",
          "LOT", "Lots 2 y 3", "lot 12b3", "0", "LOT-0000", "", " - ", None, "Lote 2 (Ávila)", "Lot° 7", "Lotes 4"]
py = [lots.canonical_lot_ref(l) for l in labels]
sql = [q("SELECT trc_canonical_lot_ref(%s)", l)[0][0] for l in labels]
ok(py == sql, f"lots.canonical_lot_ref and SQL trc_canonical_lot_ref agree on {len(labels)} labels")
ok(len({lots.canonical_lot_ref(l) for l in ["Lote 1", "Lot 1", "LOT-0001", "1", "Lot nº 1", "LOTE NÚM. 1", "Lotto 01"]}) == 1
   and lots.canonical_lot_ref("Lot 1") != lots.canonical_lot_ref("Lot 1a") and lots.canonical_lot_ref("Lot A") == "A",
   "'Lote 1', 'Lot 1', 'LOT-0001', '1', 'Lot nº 1' -> one canonical ref; '1a' and 'A' stay distinct")

# ============================== 10. ingest_lots ==============================
T = ingest_tender(conn, {"source": "PLC", "source_ref": f"LOTS-{RUN}", "title": f"Reactivos lotes {RUN}",
                         "buyer": "Hospital", "deadline": d}, "plc")
res1 = ingest_lots(conn, T.tender_id, cl, "placsp-scraper", source="PLC")
ev1 = q("SELECT count(*) FROM tender_event WHERE tender_id=%s", T.tender_id)[0][0]
res2 = ingest_lots(conn, T.tender_id, cl, "placsp-scraper", source="PLC")
ok([r.outcome for r in res1] == ["created"] * 3 and [r.outcome for r in res2] == ["existing"] * 3
   and [r.accession_no for r in res1] == [r.accession_no for r in res2]
   and q("SELECT count(*) FROM tender_event WHERE tender_id=%s", T.tender_id)[0][0] == ev1,
   "ingest_lots is idempotent: second run -> existing, same numbers, no new events")
res3 = ingest_lots(conn, T.tender_id, ef, "ted-scraper", source="TED")
ok([r.outcome for r in res3] == ["existing"] * 3 and [r.id for r in res3] == [r.id for r in res1],
   "TED 'LOT-0001' / 'LOT-0002' / 'LOT-0003' map onto the PLACSP lots '1' / '2' / '3'")
row = q("SELECT source_lot_ref, source_lot_label, lot_origin, estimated_value, cpv_codes FROM tender_lot WHERE id=%s", res1[0].id)[0]
ok(row == ("1", "1", "structured", Decimal("60000.00"), ["33651520", "33696500"]), f"lot row: canonical ref, raw label, origin, value, CPV {row}")
one = add_lot(conn, T.tender_id, title="x", source_lot_ref="Lote 1", actor="manual")
ok(one.outcome == "existing" and one.id == res1[0].id, "add_lot('Lote 1') finds lot '1'")
dup_in_notice = ingest_lots(conn, T.tender_id, [{"source_lot_label": "Lot 9", "title": "Nine"}, {"source_lot_label": "Lote 9", "title": "Nine again"}], "x")
ok(len(dup_in_notice) == 1 and dup_in_notice[0].outcome == "created", "the same lot twice in one notice is added once")
ok(raises(lambda: ingest_lots(conn, T.tender_id, [{"title": "bad", "source_lot_label": "Lot 10", "cpv_codes": ["nope"]}], "x"),
          IngestError, "CPV") and q("SELECT count(*) FROM tender_lot WHERE tender_id=%s AND source_lot_ref='10'", T.tender_id)[0][0] == 0,
   "a bad lot value rejects the batch (one transaction)")

# ============================== 11. AI-extracted lots: confirm flow ==============================
A = ingest_tender(conn, {"source": "PLC", "source_ref": f"AI-{RUN}", "title": f"Pliego con lotes {RUN}",
                         "buyer": "Hospital", "deadline": d}, "plc")
ai = ingest_lots(conn, A.tender_id, es, "doc-extractor", source="PLC", extractor=x.name)
ok([r.outcome for r in ai] == ["created"] * 3 and
   q("SELECT count(*) FROM tender_lot WHERE tender_id=%s AND lot_origin='ai_extracted' AND confirmed_at IS NULL", A.tender_id)[0][0] == 3,
   "text-extracted lots stored as ai_extracted, unconfirmed")
ok(q("SELECT payload->>'extractor', payload->>'lot_origin' FROM tender_event WHERE tender_id=%s AND event_type='lot_added' LIMIT 1", A.tender_id)
   == [("heuristic-regex-v1", "ai_extracted")], "lot_added event records origin and extractor")
out = cli("review.py", "lots", A.accession_no)
ok(out.returncode == 0 and out.stdout.count("NEEDS CONFIRMATION") == 3 and "3 AI-extracted lot(s) waiting" in out.stdout,
   "review.py lots shows the unconfirmed AI lots")
out = cli("review.py", "confirm-lot", ai[0].accession_no)
ok(out.returncode == 0 and "confirmed" in out.stdout and
   q("SELECT confirmed_by FROM tender_lot WHERE id=%s", ai[0].id) == [("review:blaise",)], "review.py confirm-lot")
ok(q("SELECT payload->>'via' FROM tender_event WHERE tender_id=%s AND event_type='lot_confirmed'", A.tender_id) == [("manual",)],
   "lot_confirmed event logged")
ok(confirm_lot(conn, ai[0].accession_no, "review:blaise") is False, "confirming twice -> already confirmed (no new event)")
ok(raises(lambda: confirm_lot(conn, res1[0].accession_no, "review:blaise"), IngestError, "only ai_extracted"),
   "a structured lot cannot be 'confirmed'")
promo = add_lot(conn, A.tender_id, title="Reactivos de bioquímica", source_lot_ref="2", actor="placsp-scraper",
                source="PLC", cpv_codes=["33696500"])
ok(promo.outcome == "confirmed" and promo.id == ai[1].id and
   q("SELECT lot_origin, confirmed_by, cpv_codes FROM tender_lot WHERE id=%s", ai[1].id) == [("structured", "placsp-scraper", ["33696500"])],
   "a structured lot from the platform confirms the AI guess (origin -> structured, gaps filled)")
out = cli("review.py", "history", A.accession_no)
ok("lot_confirmed" in out.stdout and "[ai_extracted]" in out.stdout and "heuristic-regex-v1" in out.stdout,
   "review.py history shows lot origin, extractor and confirmations")
out = cli("ingest.py", "lots", A.accession_no, os.path.join(FIX, "tender_document_en.txt"), "--format", "text", "--actor", "doc-extractor")
ok(out.returncode == 0 and "2 lot(s) found (text)" in out.stdout and out.stdout.count("created") == 2,
   "ingest.py lots CLI with --format text adds the document's lots")
out = cli("ingest.py", "lots", T.accession_no, os.path.join(FIX, "ted_eforms_sample.xml"), "--format", "eforms", "--actor", "ted-scraper")
ok(out.returncode == 0 and out.stdout.count("existing") == 3, "ingest.py lots CLI with an eForms notice: lots already there -> existing")

# ============================== 12. merge folding 'Lote 1' vs 'Lot 1' ==============================
S = ingest_tender(conn, {"source": "PLC", "source_ref": f"MS-{RUN}", "title": f"Ecógrafos {RUN}", "buyer": "H", "deadline": d}, "plc")
D = ingest_tender(conn, {"source": "TED", "source_ref": f"MD-{RUN}", "title": f"Ultrasound scanners {RUN}", "buyer": "H", "deadline": d}, "ted")
sl = add_lot(conn, S.tender_id, title="Ecógrafos portátiles", source_lot_ref="Lote 1", actor="plc", source="PLC")
dl = add_lot(conn, D.tender_id, title="Portable scanners", source_lot_ref="Lot 1", actor="ted", source="TED",
             description="Portable ultrasound", estimated_value="50000", currency="EUR")
raw = q("INSERT INTO tender_lot (tender_id, lot_no, accession_no, source_lot_ref, title) "
        "SELECT %s, n.child_no, n.child_accession, 'Lote nº 2', 'old raw row' FROM issue_child_no(%s, 'L') n RETURNING id", S.tender_id, S.tender_id)[0][0]
d2 = add_lot(conn, D.tender_id, title="Probes", source_lot_ref="LOT-0002", actor="ted", source="TED")
d3 = add_lot(conn, D.tender_id, title="Training", source_lot_ref="LOT-0003", actor="ted", source="TED")
mv = q("SELECT merge_tender(%s, %s, 'review:blaise')", D.tender_id, S.tender_id)
left = q("SELECT id, source_lot_ref, description, estimated_value FROM tender_lot WHERE tender_id=%s ORDER BY lot_no", S.tender_id)
ok([r[0] for r in left] == [sl.id, raw, d3.id] and left[0][2] == "Portable ultrasound" and left[0][3] == Decimal("50000.00"),
   "merge folds 'Lot 1' into 'Lote 1' (gaps filled from the folded lot) and 'LOT-0002' into a raw pre-005 'Lote nº 2'; LOT-0003 moves")
ok(q("SELECT child_id FROM resolve_child_accession(%s)", dl.accession_no) == [(sl.id,)]
   and q("SELECT child_id FROM resolve_child_accession(%s)", d2.accession_no) == [(raw,)],
   "old lot IDs of the folded lots resolve to the survivor's lots")

# ============================== 13. concurrency: first tenders on a brand-new code ==============================
import threading  # noqa: E402
cc = new_platform(domain=f"conc-{RUN}.example")
errs, res = [], []


def worker(i):
    try:
        with psycopg.connect(DSN, autocommit=True) as c:
            res.append(ingest_tender(c, {"source_url": f"https://www.conc-{RUN}.example/n/{i}", "source_ref": f"C{i}-{RUN}",
                                         "title": f"Concurrent first use {i} {RUN}", "deadline": d}, "conc"))
            res.append(ingest_lots(c, res[-1].tender_id, [{"source_lot_label": "Lot 1", "title": "One"},
                                                          {"source_lot_label": "LOT-0001", "title": "One again"}], "conc"))
    except Exception as e:      # noqa: BLE001
        errs.append(repr(e))


ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
ok(not errs and q("SELECT count(*) FROM tender WHERE source=%s", cc)[0][0] == 8
   and q("SELECT frozen_at IS NOT NULL FROM source_platform WHERE code=%s", cc)[0][0]
   and q("SELECT count(*) FROM tender_lot l JOIN tender t ON t.id=l.tender_id WHERE t.source=%s", cc)[0][0] == 8,
   f"8 threads: first tenders on a new code freeze it once, no errors/deadlocks, one lot each ({errs})")

print(f"\nALL {len(PASSED)} PLATFORM/LOT CHECKS PASSED")
