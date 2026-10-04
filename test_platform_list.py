"""v6 tests: the real platform list (platforms/proposals.csv + 006_seed_platforms.sql), the CSV
robustness fixes in platforms.read_rows, the cleaner rules (platform_clean.py) and seed_sql/006 safety.

    DATABASE_URL="dbname=trc_v6_test host=/var/run/postgresql" python test_platform_list.py
The database must have schema + 002 … 006 applied. Part 5 runs a small generated seed file against
it using codes/domains unique to this run (it adds a few test platforms, like the other suites).
"""
from __future__ import annotations

import csv
import os
import re
import sys
import tempfile
import uuid

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import platform_clean  # noqa: E402
import platforms as P  # noqa: E402

DSN = os.environ.get("DATABASE_URL", "dbname=trc_v6_test host=/var/run/postgresql")
RUN = uuid.uuid4().hex[:6]
PASSED: list[str] = []
TMP = tempfile.mkdtemp()
os.chmod(TMP, 0o777)


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


def write(name, data: bytes) -> str:
    p = os.path.join(TMP, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


conn = psycopg.connect(DSN, autocommit=True)


def q(sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else cur.rowcount


PROPS = list(csv.DictReader(open(os.path.join(HERE, "platforms", "proposals.csv"), encoding="utf-8")))
CLEAN = list(csv.DictReader(open(os.path.join(HERE, "platforms", "platforms_clean.csv"), encoding="utf-8")))

# ============================== 1. the approved codes ==============================
codes = [r["approved_code"] for r in PROPS]
ok(len(PROPS) == len(CLEAN) and all(codes), f"every one of the {len(PROPS)} clean platforms has an approved_code")
ok(all(re.fullmatch(r"[A-Z]{3}", c) for c in codes), "every approved code is three capital letters")
ok(len(set(codes)) == len(codes), "approved codes are unique")
ok(not (set(codes) & P.RESERVED) and not (set(codes) & P.BLOCKLIST), "no reserved (MAN/TRC) or blocklisted code")
sites = [(r["domain"], r["tenant_key"]) for r in PROPS]
ok(all(d for d, _ in sites) and len(set(sites)) == len(sites), "every platform has a domain; (domain, tenant) unique")
seeds = {r["domain"]: r["approved_code"] for r in PROPS if r["status"] == "duplicate_platform"}
ok(seeds == {"contrataciondelestado.es": "PLC", "find-tender.service.gov.uk": "FTS", "ted.europa.eu": "TED"},
   f"the list's PLACSP / Find a Tender / TED rows are recognised as the seeds, not new codes: {seeds}")
ok(all(r["review_note"] for r in PROPS if r["status"] != "ok"), "every non-ok proposal explains itself in review_note")
ok(all(r["country_source"] in ("list", "inferred_cctld", "") for r in CLEAN)
   and all(("inferred from" in r["notes"]) for r in CLEAN if r["country_source"] == "inferred_cctld"),
   "countries come from the list or are marked as inferred from the ccTLD")

# ============================== 2. 006 registered them; each resolves by URL ==============================
reg = {r[0]: r[1:] for r in q("SELECT code, domain, tenant_key, status, priority FROM source_platform")}
ok(all(c in reg and reg[c][0] == r["domain"] and (reg[c][1] or "") == r["tenant_key"] and reg[c][2] == "active"
       for c, r in zip(codes, PROPS)),
   f"006 registered all {len(PROPS)} list platforms as active with their domain/tenant ({len(reg)} in the registry)")
ok(reg["MAN"][3] == 10 and reg["PLC"][3] == 100 and reg["FTS"][3] == 100 and reg["TED"][3] == 50,
   "seeds keep their priorities (PLC/FTS 100, TED 50, MAN 10)")
ok({reg[c][3] for c in codes} | {reg["MAN"][3]} <= {10, 30, 50, 70, 100}, "priorities follow the documented rule (10/30/50/70/100)")


def R(url):
    return P.resolve_platform(conn, url).code


bad = []
for r in PROPS:
    c, url, dom, ten = r["approved_code"], r["url"], r["domain"], r["tenant_key"]
    tries = [url]
    if not ten:
        tries += [f"https://www.{dom}/some/deeper/page?x=1", f"HTTP://{dom.upper()}:443/"]
    elif ten.startswith("/"):
        tries += [f"https://{dom}{ten}/deeper/page?x=1"]
    else:
        tries += [url + ("&" if "?" in url else "?") + "zz=1"]
    for t in tries:
        try:
            got = R(t)
        except P.PlatformError as e:
            got = f"error {e}"
        if got != c:
            bad.append((c, t, got))
ok(not bad, f"every platform resolves to its code by its URL and by www / deeper-path / port variants {bad[:5]}")
site_doms = {d for d, t in sites if not t}


def parent_site(h):
    labels = h.split(".")
    return next((".".join(labels[i:]) for i in range(1, len(labels) - 1) if ".".join(labels[i:]) in site_doms), None)


tenant_hosts = {d for d, t in sites if t} - site_doms
with_parent = sorted(h for h in tenant_hosts if parent_site(h))
tenant_hosts = {h for h in tenant_hosts if not parent_site(h)}
if with_parent:
    h = with_parent[0]
    pc_ = next(r["approved_code"] for r in PROPS if r["domain"] == parent_site(h) and not r["tenant_key"])
    ok(R(f"https://{h}/no-such-tenant-{RUN}") == pc_,
       f"an unknown tenant on {h} falls back to the registered parent domain {parent_site(h)} ({pc_})")
if tenant_hosts:
    h = sorted(tenant_hosts)[0]
    ok(raises(lambda: R(f"https://{h}/no-such-tenant-{RUN}"), P.PlatformError, "no registered platform"),
       f"an unknown tenant on a shared host with no site-level platform ({h}) is refused, never guessed")
shared = sorted({d for d, t in sites if t} & {d for d, t in sites if not t})
if shared:
    h = shared[0]
    site = next(r["approved_code"] for r in PROPS if r["domain"] == h and not r["tenant_key"])
    ok(R(f"https://{h}/not-a-tenant-{RUN}") == site, f"on {h} an unknown path falls back to the site platform {site}")
ok(R("https://urldefense.com/v3/__https://www.tenderned.nl/aankondigingen__;!!abc$") == R("https://www.tenderned.nl"),
   "a Proofpoint-wrapped URL resolves to the wrapped site")

# ============================== 3. reading messy files (pure) ==============================
hdr = "Name of Tender Platform,Name of Tender Platform_link,Country,Importance,URL,URL_link\n"
body = ("Plataforma Contratación,,Spain,Public,Home,https://contrataciondelestado.es/wps/portal\n"
        + hdr +                                                     # a repeated header (sections)
        ",,Australia,Private,australiantenders,https://www.australiantenders.com.au/search\n"
        "BCeID,,,,,\n"
        "https://www.merx.com/,,,H,,\n"
        "Bund Portal (https://www.service.bund.de),,Germany;Austria,high,,\n"
        "SAM,,USA,Public,SAM.gov,\n"
        "Broken host,,UK,,,https://www.ukri.org-tid=1234/opportunity\n"
        "Paren,,EU,,(https://www.paren.example/x),\n"
        "IE row,,,,https://www.etenders.gov.ie (IRELAND),\n"
        "Wrapped,,,,,https://urldefense.com/v3/__https://www.planetbids.com/portal.cfm?CompanyID=42__;!!x$\n"
        "Oauth,,,,,https://login.example.com/oauth2/authorize?redirect_uri=https%3A%2F%2Fportal.realsite.example%2Fhome\n")
text = hdr + body
variants = {"utf8": text.encode("utf-8"), "utf8_bom": b"\xef\xbb\xbf" + text.encode("utf-8"),
            "cp1252": text.encode("cp1252"), "utf16": text.encode("utf-16"),
            "semicolon": text.replace(",", ";").encode("utf-8"), "tab": text.replace(",", "\t").encode("utf-8")}
results = {}
for k, data in variants.items():
    rows, msgs = P.read_rows(write(f"list_{k}.csv", data))
    results[k] = (rows, msgs)
names0 = [r["name"] for r in results["utf8"][0]]
ok(all([r["name"] for r in rows] == names0 for rows, _ in results.values()) and names0[0] == "Plataforma Contratación",
   f"UTF-8, UTF-8 BOM, cp1252/latin-1, UTF-16, ';' and tab files read the same (accents intact): {sorted(results)}")
ok(any("delimiter ';'" in m for m in results["semicolon"][1]) and any("delimiter '\\t'" in m or "delimiter tab" in m
                                                                       for m in results["tab"][1]),
   "the delimiter is detected and reported")
rows, msgs = results["utf8"]
by = {r["row"]: r for r in rows}
ok(any("repeated header row(s): 3" in m for m in msgs) and 3 not in by and by[4]["row"] == 4,
   "a repeated header row is skipped; row numbers stay the spreadsheet's")
ok(by[2]["url"].startswith("https://contrataciondelestado.es") and by[2].get("url_title") == "Home",
   "Excel hyperlink export: URL taken from the _link column, the link text kept as url_title")
ok(by[4]["name"] == "australiantenders.com.au" and "no name in the list" in by[4]["flags"][0],
   "a row with no name but a URL is named after its domain and flagged")
ok(by[5]["name"] == "BCeID" and not by[5]["url"], "a name-only row is read (the cleaner holds it back)")
ok(by[6]["name"] == "merx.com" and by[6]["url"].startswith("https://www.merx.com"), "a name that is a URL becomes the URL + domain name")
ok(by[7]["name"] == "Bund Portal" and P.domain_of(by[7]["url"]) == "service.bund.de", "a URL in brackets inside the name is moved out")
ok(by[8]["url"] == "https://sam.gov" or P.domain_of(by[8]["url"]) == "sam.gov", f"bare 'SAM.gov' read as a URL ({by[8]['url']})")
ok(P.domain_of(by[9]["url"]) == "ukri.org" and any("looked broken" in f for f in by[9]["flags"]),
   "'-tid=…' glued to the host repaired and flagged")
ok(P.domain_of(by[10]["url"]) == "paren.example" and P.domain_of(by[11]["url"]) == "etenders.gov.ie",
   "'(https://…' and '… (IRELAND)' URL text cleaned")
ok(P.domain_of(by[12]["url"]) == "planetbids.com" and "companyid=42" in P.url_tenants(by[12]["url"]),
   "urldefense-wrapped URL unwrapped, tenant parameter kept")
ok(P.domain_of(by[13]["url"]) == "portal.realsite.example", "login/OAuth redirect wrapper unwrapped to the real site")
ok(raises(lambda: P.read_rows(write("noname.csv", b"Website,Country\nhttps://x.example,ES\n")), P.PlatformError, "name"),
   "a file without any name column is refused")
syn, _ = P.read_rows(write("syn.csv", "Portal;Web address;Land;Kürzel\nTenderNed;www.tenderned.nl;Netherlands;TN\n".encode("cp1252")))
ok(syn and syn[0]["name"] == "TenderNed" and P.domain_of(syn[0]["url"]) == "tenderned.nl",
   f"header synonyms / keywords mapped (Portal, Web address) in a cp1252 ';' file: {syn[:1]}")

# country parsing
pc = P.parse_country
ok(pc("Spain")["country"] == "ES" and pc("UK")["country"] == "GB" and pc("USA")["country"] == "US"
   and pc("DEU")["country"] == "DE", "country names, UK/USA and alpha-3 codes -> ISO")
m = pc("Germany; Austria and Switzerland")
ok(m["country"] in ("", None) and m["countries"] == ["DE", "AT", "CH"], f"multi-country cell -> countries list {m}")
ok(pc("EU")["regions"] and not pc("EU")["country"] and pc("Global")["regions"], "regions (EU, Global) are not countries")
ok(pc("RLTD")["unknown"], "unexplained codes (RLTD) reported as unknown, not guessed")
ok(P.cctld_country("www.marches.fr") == "FR" and P.cctld_country("x.co.uk") == "GB" and P.cctld_country("x.com") is None
   and P.cctld_country("x.eu") is None, "ccTLD inference (.fr, .co.uk) but none for generic/EU domains")

# ============================== 4. cleaner rules (pure) ==============================
H = "Name,URL,Country,Importance\n"
src = (H +
       "Foo Portal,https://www.foo.example/,France,Public\n"
       "Foo Portal,https://www.foo.example/,France,Public\n"               # exact duplicate
       "foo portal search,https://foo.example/search?q=1,,high\n"          # same site, merged
       "University A,https://shared.example/unia,UK,\n"                    # tenants on a shared host
       "University B,https://shared.example/unib/tenders,UK,\n"
       "Bar Market,https://bar.example,,Private\n"                         # same name, other host: flagged only
       "Bar Market,https://bar-market.example,,Private\n"
       "Foo Portal,,,Public\n"                                             # no URL, folds into Foo Portal
       "Lonely Heading,,,\n"                                               # name only: held back
       "Baz Nouveau,https://baz.fr,,\n")                                   # country from .fr (inferred)
s = platform_clean.clean_file(write("clean_in.csv", src.encode()), os.path.join(TMP, "clean_out"))
c = s["cleaner"]
clean, removed = c.clean_rows(), c.removed_rows()
kinds = {(r["source_row"], r["kind"], r["merged_into_row"]) for r in removed}
ok((3, "exact_duplicate", 2) in kinds, f"exact duplicate removed, pointing at the kept row {kinds}")
ok(any(k[0] == 4 and k[2] == 2 for k in kinds), "same domain, site-level URL merged into the most complete row")
foo = next(r for r in clean if r["domain"] == "foo.example")
ok(foo["country"] == "FR" and "high" in foo["importance"].lower() and foo["url"] == "https://www.foo.example/",
   f"merged row keeps the simplest URL and gains the non-conflicting fields {foo['country']}/{foo['importance']}")
ok(any(k[0] == 9 and k[1] == "same_name_no_url" for k in kinds), "a row without URL folds into the platform of the same name")
ten = sorted((r["tenant_key"], r["name"]) for r in clean if r["domain"] == "shared.example")
ok(ten == [("/unia", "University A"), ("/unib", "University B")], f"different names on one shared host -> tenants {ten}")
bars = [r for r in clean if r["name"] == "Bar Market"]
ok(len(bars) == 2 and any(v["action"].startswith("kept_flagged") and "Bar Market" in v["name"] for v in c.review),
   "same name on different hosts: both kept, flagged in needs_review (never merged)")
ok(any(v["name"] == "Lonely Heading" and v["action"] == "held_back" for v in c.review)
   and not any(r["name"] == "Lonely Heading" for r in clean), "a name-only row is held back for review")
baz = next(r for r in clean if r["domain"] == "baz.fr")
ok(baz["country"] == "FR" and baz["country_source"] == "inferred_cctld", "country inferred from the ccTLD is marked as inferred")
ok(next(r for r in clean if r["name"] == "Bar Market")["priority"] == "30" and foo["priority"] == "70"
   and baz["priority"] == "50", "priority rule: Private 30, Public 70, unclassified 50")

# ============================== 5. seed_sql refusals and 006 safety ==============================
PC = P.PROPOSAL_COLUMNS


def props_file(name, rows):
    p = os.path.join(TMP, name)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PC, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: "" for k in PC} | r)
    return p


def free(prefix):
    used = {r[0] for r in q("SELECT code FROM source_platform")}
    import itertools
    for a, b in itertools.product("ABCDEFGHIJKLMNOPQRSTUVWXYZ", repeat=2):
        cc = prefix + a + b
        if cc not in used and cc not in P.BLOCKLIST and cc not in P.RESERVED:
            used.add(cc)
            yield cc


gen = free("Z")
k1, k2, k3, k4, k5 = (next(gen) for _ in range(5))
base = {"data_format": "other", "priority": "50", "country": ""}
ok(raises(lambda: P.seed_sql(props_file("bad1.csv", [base | {"name": "X", "url": "https://x.example", "domain": "x.example",
                                                             "approved_code": "AB1"}])), P.PlatformError),
   "seed_sql refuses a malformed code")
ok(raises(lambda: P.seed_sql(props_file("bad2.csv", [base | {"name": "X", "url": "https://x.example", "domain": "x.example",
                                                             "approved_code": "MAN"}])), P.PlatformError),
   "seed_sql refuses a reserved code")
ok(raises(lambda: P.seed_sql(props_file("bad3.csv", [
    base | {"name": "X", "url": "https://x.example", "domain": "x.example", "approved_code": k1},
    base | {"name": "Y", "url": "https://y.example", "domain": "y.example", "approved_code": k1}])), P.PlatformError),
   "seed_sql refuses a duplicate code")
ok(raises(lambda: P.seed_sql(props_file("bad4.csv", [base | {"name": "X", "url": "", "domain": "", "approved_code": k1}])),
          P.PlatformError), "seed_sql refuses a row without a domain")

ss = P.seed_schema_sql()
ok("{1,3}" in ss and "{{" not in ss and "CREATE TEMP TABLE" not in ss and ss.strip().endswith("COMMIT;"),
   "seed-sql --schema-only gives only the tenant schema part of 006 (for the apply path)")
d1, d2, d3, d4 = (f"{n}-{RUN}.example" for n in ("one", "two", "three", "four"))
q("INSERT INTO source_platform (code, name, url, domain, status, priority, code_origin) VALUES "
  "(%s, 'Kept name', %s, %s, 'active', 60, 'manual'), (%s, 'Proposed one', %s, %s, 'proposed', 40, 'suggested'), "
  "(%s, 'Holder of the code', %s, %s, 'active', 50, 'manual')",
  k1, f"https://{d1}", d1, k2, f"https://{d2}", d2, k3, f"https://{d4}", d4)
q("UPDATE source_platform SET frozen_at = now() WHERE code = %s", k1)
sql, info = P.seed_sql(props_file("mini.csv", [
    base | {"name": "Same site, other code", "url": f"https://{d1}", "domain": d1, "approved_code": k4},
    base | {"name": "Proposed one", "url": f"https://{d2}", "domain": d2, "approved_code": k2},
    base | {"name": "Code taken", "url": f"https://{d3}", "domain": d3, "approved_code": k3},
    base | {"name": "Brand new", "url": f"https://www.{RUN}-new.example", "domain": f"{RUN}-new.example",
            "approved_code": k5, "tenant_key": "/tenant-a"}]))
notices = []
conn.add_notice_handler(lambda d: notices.append(d.message_primary))
with conn.cursor() as cur:
    cur.execute("SET client_min_messages = notice")
    cur.execute(sql)
reg = {r[0]: r[1:] for r in q("SELECT code, name, domain, status, priority, frozen_at IS NOT NULL, tenant_key FROM source_platform "
                              "WHERE code = ANY(%s)", [k1, k2, k3, k4, k5])}
ok(reg[k1] == ("Kept name", d1, "active", 60, True, None) and k4 not in reg
   and any(f"{k4}={k1}" in n for n in notices), "same site already registered under another (frozen) code: kept unchanged, WARNING")
ok(reg[k2][2] == "active" and reg[k2][3] == 40, "a proposed platform is activated; its other fields untouched")
ok(reg[k3][1] == d4 and any(f"code {k3} is already used" in n for n in notices) and
   not q("SELECT 1 FROM source_platform WHERE domain = %s", d3), "code taken by another domain: WARNING, row not registered")
ok(reg[k5][2] == "active" and reg[k5][5] == "/tenant-a" and P.resolve_platform(conn, f"https://{RUN}-new.example/tenant-a/x").code == k5,
   "a new platform (with a tenant) is inserted active and resolves")
before = q("SELECT code, name, status, priority, tenant_key FROM source_platform ORDER BY code")
with conn.cursor() as cur:
    cur.execute(sql)
ok(q("SELECT code, name, status, priority, tenant_key FROM source_platform ORDER BY code") == before, "re-running the seed file is a no-op")
ok(raises(lambda: q("INSERT INTO source_platform (code, name, domain, tenant_key, status) VALUES (%s, 'x', %s, '/Upper', 'active')",
                    next(gen), d3), psycopg.errors.CheckViolation),
   "tenant_key must be lower-case /path or param=value (check constraint)")
conn.close()
print(f"\nALL {len(PASSED)} PLATFORM-LIST CHECKS PASSED")
