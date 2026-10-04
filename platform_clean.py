"""Clean a raw list of tender platforms before `platforms.py suggest` (v6).

    python platform_clean.py MY_LIST.csv --out-dir platforms        # or: python platforms.py clean ...

Reads CSV/XLSX like `platforms.py suggest` (see platforms.read_rows) and writes, in --out-dir:
    platforms_clean.csv     one row per platform (input for `platforms.py suggest`)
    duplicates_removed.csv  every input row folded into another, with the row it went into and why
    needs_review.csv        rows held back (no URL, name only) and platforms kept but worth a look
The input file is never modified. Everything is deterministic (same file -> same output).

Rules (documented in README "Cleaning a platform list"):
 1. Exact duplicates: rows whose cells are all equal (case and spacing ignored) -> first one kept.
 2. Rows are grouped by host (lower case, no www., no port; link wrappers such as urldefense and
    login redirects are unwrapped first). On one host:
    * a row with a tenant parameter in its URL (CompanyID=, CustOrg=, CustomerOrg=, B=, ...) is its
      own platform for that tenant (shared SaaS hosts such as planetbids.com, sciquest.com);
    * "site-level" rows (URL without a meaningful first path segment, or a name that is the host /
      a host label, e.g. 'subreport (dtvp.de)', 'Biddingo' on biddingo.com) are one platform: the
      same address means the same platform, even when the names differ (that case is flagged);
    * a row under its own first path segment (in-tendhost.co.uk/universityofleeds) joins the
      site-level platform when its name matches one there, else it is a tenant platform of its
      own (registered with tenant_key '/segment'), unless it is the only group on the host (then it
      is the site) - except on a whole-government host such as gov.br or gov.pl, where the path
      always names the platform ('/compras', '/web/uzp').
 3. Merging keeps the most complete row (URL, country, sector, importance, a real name; ties -> the
    earliest row), fills its empty fields from the others and notes every conflict. The simplest URL
    of the group is kept. Other names become aliases.
 4. A row without a URL is folded into the one platform with the same name (or the name in its
    brackets); otherwise it is held back (needs_review), as are rows with only a name.
 5. Platforms on different hosts are never merged. Near-identical names on different hosts are kept
    and flagged as possible duplicates.
 6. Country: names and codes -> ISO 3166-1 alpha-2 (UK -> GB, Scotland -> GB, ...). Several
    countries -> country left empty, list in 'countries'. Regions ('Europe', 'Global') and unknown
    values are kept in the notes. With no country in the list, a country-code TLD gives one,
    marked country_source=inferred_cctld (.io/.co/.tv/... and gTLDs never do).
 7. Priority: the seeds with a structured feed keep their 005 values (PLC 100, FTS 100, TED 50 because
    TED republishes national notices and the national source should win);
    70 official (sector 'Public' in the list, a government domain such as .gov/.gob/.gouv/.gv.at/
    .gc.ca/europa.eu, or a government name such as 'State of', 'City of', 'County'); 30 private
    (sector 'Private...' in the list); 50 when the list gives no evidence either way.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import os
import re
import sys
from collections import OrderedDict

import platforms as P

SEED_DOMAINS = {"contrataciondelestado.es": ("PLC", "codice", 100), "find-tender.service.gov.uk": ("FTS", "ocds", 100),
                "ted.europa.eu": ("TED", "eforms", 50)}
GOV_LABELS = {"gov", "gob", "gouv", "govt", "gv", "gc", "mil", "gub", "overheid"}
GOV_NAME = re.compile(r"(?i)\b(government|gouvernement|state of|city of|county|commonwealth of|province of|"
                      r"ministry|ministerio|department of|municipal|gobierno|junta de|generalitat|regierung|"
                      r"freistaat|landes\w*|ayuntamiento|diputaci[oó]n|council|stadt|land hessen|hansestadt)\b")
WEB_WORDS = {"home", "homepage", "login", "log", "in", "page", "search", "portal", "welcome", "site", "index",
             "find", "posting", "avis", "du", "jour", "public", "quote", "tender", "tenders", "notices",
             "for", "of", "the", "electronic", "tendering"}
LEVELS = (("high", re.compile(r"(?i)^(h|high)\b")), ("medium", re.compile(r"(?i)^(m|medium|med)\b")),
          ("low", re.compile(r"(?i)^(l|low)\b")))

CLEAN_COLUMNS = ("name", "url", "domain", "tenant_key", "short_name", "country", "countries", "country_source",
                 "sector", "importance", "priority", "data_format", "aliases", "source_rows", "review_note", "notes")
REMOVED_COLUMNS = ("source_row", "name", "url", "country", "importance", "kind", "merged_into_row",
                   "merged_into_name", "reason")
REVIEW_COLUMNS = ("source_rows", "name", "url", "domain", "issue", "action", "related", "suggestion")


def sq(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", P.strip_accents(str(x or "")).lower())


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def host_labels(host: str | None) -> list[str]:
    if not host:
        return []
    labs = host.split(".")
    return [l for i, l in enumerate(labs) if l not in P.GENERIC_LABELS and not (i > 0 and l in P.TLD_LIKE)
            and i < len(labs) - 1]


def reg_domain(host: str | None) -> str:
    """Registrable part: x.y.example.co.uk -> example.co.uk, a.b.example.com -> example.com."""
    labs = (host or "").split(".")
    n = 3 if len(labs) >= 3 and labs[-2] in P.TLD_LIKE | {"ac", "gv", "gc", "com", "org", "net", "edu"} else 2
    return ".".join(labs[-n:])


def is_domain_string(r: dict) -> bool:
    """The name is (or wraps) the host itself: 'subreport (dtvp.de)', 'evergabe-online.de'."""
    h = r["domain"]
    return bool(h) and (h in r["name"].lower() or any("named after the domain" in f for f in r["flags"]))


def names_host(r: dict) -> bool:
    """The name identifies the host: a domain string, or the name equals a host label
    ('Biddingo' on biddingo.com, 'Ontario Tenders' on ontariotenders.bravosolution.com)."""
    if is_domain_string(r):
        return True
    n = sq(r["name"])
    return any(n == l.replace("-", "") or (len(n) >= 5 and ratio(n, l.replace("-", "")) >= 0.85)
               for l in host_labels(r["domain"]))


def alt_texts(r: dict) -> list[str]:
    """The name, the bracketed parts of the name and the useful parts of the link text."""
    out = [r["name"]]
    out += [m.strip() for m in re.findall(r"\(([^()]+)\)", r["name"]) if m.strip()]
    for part in re.split(r"\s+[|:–-]+\s+|::|\s\|\s", r.get("url_title") or ""):
        part = part.strip()
        words = {w.lower() for w in re.findall(r"[A-Za-z]+", part)}
        if part and words and not words <= WEB_WORDS:
            out.append(part)
    return out


def _tokset(x: str) -> set[str]:
    return {w.lower() for w in P._significant(x) if len(w) >= 2}


def text_compat(a: str, b: str, loose: bool) -> bool:
    sa, sb = sq(a), sq(b)
    if not sa or not sb:
        return False
    if sa == sb or (min(len(sa), len(sb)) >= 4 and ratio(sa, sb) >= (0.85 if loose else 0.92)):
        return True
    ka, kb = P.name_key(a), P.name_key(b)
    if ka and ka == kb:
        return True
    if not loose:
        return False
    ta, tb = _tokset(a), _tokset(b)
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return bool(small) and small <= big and any(len(w) >= 3 for w in small)


def compat(r1: dict, r2: dict, loose: bool = True) -> bool:
    return any(text_compat(a, b, loose) for a in alt_texts(r1) for b in alt_texts(r2))


def completeness(r: dict) -> tuple:
    return (bool(r["url"]), bool(r["cinfo"]["countries"]), bool(r["sector"]), bool(r["level"]),
            not is_domain_string(r), bool(r.get("url_title")))


def sector_of(text: str) -> str:
    t = text.lower()
    if "private" in t:
        return "private"
    if "public" in t:
        return "public"
    return ""


def level_of(text: str) -> str:
    for lvl, rx in LEVELS:
        if rx.match(text.strip()):
            return lvl
    return ""


def _url_rank(u: str) -> tuple:
    return (not re.match(r"(?i)^https?://", u), len(u.split("?")[0].rstrip("/")), "?" in u, u)


def prepare(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["domain"] = P.domain_of(r["url"]) if r["url"] else None
        r["cinfo"] = P.parse_country(r.get("country", ""))
        imp = r.get("importance", "")
        r["sector"], r["level"] = sector_of(imp), level_of(imp)
        r["qt"], r["pt"] = P.tenant_of(r["url"]) if r["url"] else (None, None)
    return rows


class Cleaner:
    def __init__(self, rows: list[dict]):
        self.rows = prepare(rows)
        self.removed: list[dict] = []
        self.review: list[dict] = []
        self.platforms: list[dict] = []          # {'rows': [...], 'tenant': key|None, 'how': {row: reason}}

    # -- helpers --
    def _remove(self, r, into, kind, reason):
        self.removed.append({"source_row": r["row"], "name": r["name"], "url": r["url"], "country": r.get("country", ""),
                             "importance": r.get("importance", ""), "kind": kind, "merged_into_row": into["row"],
                             "merged_into_name": into["name"], "reason": reason, "_into": into})

    def run(self):
        # 1. exact duplicates
        seen, rows = {}, []
        for r in self.rows:
            k = r["raw"]
            if k in seen:
                self._remove(r, seen[k], "exact_duplicate", "all cells identical (case/spacing ignored)")
            else:
                seen[k] = r
                rows.append(r)
        # 2. by host
        hosts: OrderedDict[str, list] = OrderedDict()
        nourl = []
        for r in rows:
            if r["domain"]:
                hosts.setdefault(r["domain"], []).append(r)
            else:
                nourl.append(r)
        for host, rs in hosts.items():
            self._host(host, rs)
        # 3. merge each platform
        self.final = [self._merge(p) for p in self.platforms]
        # 4. rows without a URL
        for r in nourl:
            self._no_url(r)
        # 5. cross-host near-identical names
        self._cross_host()
        self.final.sort(key=lambda f: f["_first"])
        return self

    def _host(self, host, rs):
        qgroups: OrderedDict[str, list] = OrderedDict()
        site, paths = [], OrderedDict()
        gov_root = host == reg_domain(host) and host.split(".")[0] in GOV_LABELS     # gov.br, gov.pl
        for r in rs:
            if r["qt"]:
                qgroups.setdefault(r["qt"], []).append(r)
            elif (names_host(r) and not gov_root) or not r["pt"]:
                site.append(r)
            else:
                paths.setdefault(r["pt"], []).append(r)
        joined = {}
        for pt in list(paths):
            g = paths[pt]
            if site and any(compat(x, y) for x in g for y in site):
                for x in g:
                    joined[x["row"]] = f"path {pt} on the same site, name matches"
                site += paths.pop(pt)
        if not site and len(paths) == 1 and not gov_root:     # the host's only path-level entry is the site
            site = paths.popitem()[1]                            # (query tenants stay separate)
        shared = len(paths) + len(qgroups) + (1 if site else 0) > 1
        if site:
            self.platforms.append({"rows": site, "tenant": None, "host": host, "joined": joined, "shared": shared})
        for key, g in list(paths.items()) + list(qgroups.items()):
            self.platforms.append({"rows": g, "tenant": key, "host": host, "joined": {}, "shared": shared})

    def _merge(self, p) -> dict:
        rs = p["rows"]
        base = max(rs, key=lambda r: (completeness(r), -r["row"]))
        notes, review = [], []
        names = [r for r in sorted(rs, key=lambda r: (r is not base, -sum(completeness(r)), r["row"]))]
        real = [r for r in names if not is_domain_string(r)]
        name = (real or names)[0]["name"]
        aliases = []
        for r in names:
            for t in [r["name"]]:
                if t != name and sq(t) not in {sq(a) for a in aliases} and sq(t) != sq(name):
                    aliases.append(t)
        # rows folded into base
        for r in rs:
            if r is base:
                continue
            if p["tenant"]:
                why = f"same host {p['host']} and tenant {p['tenant']}"
            elif r["row"] in p["joined"]:
                why = f"same host {p['host']}; {p['joined'][r['row']]}"
            else:
                why = f"same host {p['host']} (site-level URL)"
            kind = "same_domain" if compat(r, base) or names_host(r) or names_host(base) else "same_site_different_name"
            if kind == "same_site_different_name":
                why += f"; names differ ({r['name']!r} vs {base['name']!r}) but the address is the same"
            self._remove(r, base, kind, why)
        nonhost = [r for r in rs if not names_host(r)]
        odd = [r for r in nonhost if not any(compat(r, o) for o in nonhost if o is not r)] if len(nonhost) > 1 else []
        if odd:
            review.append("different names at the same address merged: " + " / ".join(sorted({r['name'] for r in nonhost})))
        # URL: the simplest one
        url = sorted({r["url"] for r in rs}, key=_url_rank)[0]
        for r in rs:
            for f in r["flags"]:
                (review if ("broken" in f or "no name" in f or "named after" in f or "has no URL" in f) else notes).append(f"row {r['row']}: {f}")
        # country
        cvals = [(r, r["cinfo"]) for r in rs]
        isos = []
        for r, ci in sorted(cvals, key=lambda x: x[0] is not base):
            for c in ci["countries"]:
                if c not in isos:
                    isos.append(c)
        regions = sorted({x for _, ci in cvals for x in ci["regions"]})
        unknown = sorted({x for _, ci in cvals for x in ci["unknown"]})
        single = [ci["country"] for _, ci in cvals if ci["country"]]
        country, csrc = "", ""
        if base["cinfo"]["country"]:
            country, csrc = base["cinfo"]["country"], "list"
        elif len(set(single)) == 1 and not any(len(ci["countries"]) > 1 for _, ci in cvals):
            country, csrc = single[0], "list"
        elif len(isos) == 1:
            country, csrc = isos[0], "list"
        if len(isos) > 1 or (country and any(ci["countries"] and ci["countries"] != [country] for _, ci in cvals)):
            vals = sorted({r.get("country", "") for r, ci in cvals if r.get("country")})
            if len(set(single)) > 1 or (country and len(isos) > 1):
                review.append("rows disagree on the country: " + " / ".join(vals))
        if regions:
            notes.append("region in list: " + ", ".join(regions))
        if unknown:
            notes.append("country column value(s) not a country: " + ", ".join(unknown))
        cc = P.cctld_country(p["host"])
        if not isos and cc:
            country, csrc = cc, "inferred_cctld"
            notes.append(f"country {cc} inferred from the .{p['host'].rsplit('.', 1)[-1]} domain (not in the list)")
        elif country and cc and cc != country:
            review.append(f"country {country} in the list but the domain is .{p['host'].rsplit('.', 1)[-1]}")
        # sector / importance
        sectors = OrderedDict((r["sector"], r) for r in sorted(rs, key=lambda r: r is not base) if r["sector"])
        levels = OrderedDict((r["level"], r) for r in sorted(rs, key=lambda r: r is not base) if r["level"])
        sector = next(iter(sectors), "")
        level = next(iter(levels), "")
        if len(sectors) > 1:
            review.append("rows disagree on public/private: " + " / ".join(sectors))
        if len(levels) > 1:
            notes.append(f"importance differs between rows ({' / '.join(levels)}); kept {level}")
        raw_imp = sorted({r.get("importance", "") for r in rs if r.get("importance")})
        # priority
        seed = SEED_DOMAINS.get(p["host"]) if not p["tenant"] else None
        labs = p["host"].split(".")
        gov_dom = bool(set(labs[1:] if len(labs) > 2 else labs) & GOV_LABELS) or p["host"].endswith("europa.eu") or \
            bool(re.search(r"\.state\.[a-z]{2}\.us$", p["host"]))
        gov_name = any(GOV_NAME.search(re.sub(r"\([^)]*\.[a-z]{2,}[^)]*\)", "", r["name"])) for r in rs)
        if seed:
            prio, basis = seed[2], (f"seed platform {seed[0]} with a structured feed ({seed[1]}); keeps its 005 priority"
                                    + (" (TED republishes national notices, so the national source wins)" if seed[0] == "TED" else ""))
        elif sector == "private":
            prio, basis = 30, "sector 'Private' in the list"
        elif sector == "public":
            prio, basis = 70, "sector 'Public' in the list"
        elif gov_dom:
            prio, basis = 70, f"government domain {p['host']}"
        elif gov_name:
            prio, basis = 70, "government body in the name"
        else:
            prio, basis = 50, "not classified (no public/private information in the list)"
        notes.append(f"priority {prio}: {basis}")
        if raw_imp:
            notes.append("importance in list: " + " / ".join(raw_imp))
        if p["tenant"]:
            review.append(f"tenant {p['tenant']} on shared host {p['host']}: URLs resolve to it only with that "
                          f"{'path' if p['tenant'].startswith('/') else 'parameter'}")
        titles = sorted({r["url_title"] for r in rs if r.get("url_title")})
        if titles:
            notes.append("link text: " + " | ".join(t[:80] for t in titles))
        return {"name": name, "url": url, "domain": p["host"], "tenant_key": p["tenant"] or "", "short_name": "",
                "country": country, "countries": ";".join(isos) if len(isos) > 1 else "",
                "country_source": csrc, "sector": sector, "importance": level, "priority": str(prio),
                "data_format": seed[1] if seed else "", "aliases": " | ".join(aliases),
                "source_rows": " ".join(str(r["row"]) for r in sorted(rs, key=lambda r: r["row"])),
                "review": review, "notes_l": notes, "_first": min(r["row"] for r in rs), "_base": base, "_rows": rs}

    def _no_url(self, r):
        ci = r["cinfo"]
        only_name = not (r.get("country") or r.get("importance") or r.get("url_title"))
        hits = [f for f in self.final if any(compat(r, x, loose=False) for x in f["_rows"])]
        if len(hits) == 1 and not only_name:
            f = hits[0]
            self._remove(r, f["_base"], "same_name_no_url", f"no URL; same name as row {f['_base']['row']} ({f['name']})")
            f["source_rows"] = " ".join(sorted(f["source_rows"].split() + [str(r["row"])], key=int))
            if r["name"] != f["name"] and sq(r["name"]) not in {sq(a) for a in f["aliases"].split(" | ")}:
                f["aliases"] = " | ".join(filter(None, [f["aliases"], r["name"]]))
            if not f["sector"] and r["sector"]:
                f["sector"] = r["sector"]
            return
        if only_name:
            issue = "only a name (no URL, country or importance): a section heading or a platform without details"
        elif hits:
            issue = "no URL; the name matches several platforms: " + ", ".join(h["name"] for h in hits)
        else:
            issue = "no URL in the list: cannot be resolved by URL, so not registered"
        self.review.append({"source_rows": str(r["row"]), "name": r["name"], "url": "", "domain": "",
                            "issue": issue, "action": "held_back", "related": ", ".join(h["name"] for h in hits),
                            "suggestion": "add the URL (then re-run clean) or delete the row"})

    def _cross_host(self):
        """Never merges across hosts; flags near-identical names. 'X (host)' names (how the list
        notes an aggregator or vendor) only count when the two hosts share a registrable domain."""
        F = self.final
        for f in F:
            plain, branded = [], []
            for t in [f["name"]] + [a for a in f["aliases"].split(" | ") if a]:
                m = re.fullmatch(r"(.+?)\s*\(\s*([a-z0-9.-]+\.[a-z]{2,})[^)]*\)", t.strip(), re.I)
                if m:
                    branded.append(m.group(1))
                elif f["domain"] in t.lower() or P.looks_like_url(t) or re.fullmatch(r"[\w.-]+\.[a-z]{2,}", t, re.I) and t.lower() == f["domain"]:
                    continue
                else:
                    plain.append(t)
                    plain += [x for x in re.findall(r"\(([A-Z][A-Z0-9&]{2,})\)", t)]
            f["_plain"], f["_branded"] = plain, branded
        for i, a in enumerate(F):
            for b in F[i + 1:]:
                if a["domain"] == b["domain"]:
                    continue
                why = None
                if any(text_compat(x, y, loose=False) for x in a["_plain"] for y in b["_plain"]):
                    why = "same or near-identical name on different hosts"
                elif reg_domain(a["domain"]) == reg_domain(b["domain"]) and \
                        any(sq(x) == sq(y) for x in a["_branded"] + a["_plain"] for y in b["_branded"] + b["_plain"]):
                    why = f"same name on two hosts of {reg_domain(a['domain'])}"
                if not why:
                    continue
                for me, other in ((a, b), (b, a)):
                    me["review"].append(f"possible duplicate kept: {other['name']} ({other['domain']}"
                                        f"{' ' + other['tenant_key'] if other['tenant_key'] else ''}) - {why}")
                self.review.append({"source_rows": f"{a['source_rows']} / {b['source_rows']}",
                                    "name": f"{a['name']} / {b['name']}", "url": f"{a['url']} / {b['url']}",
                                    "domain": f"{a['domain']} / {b['domain']}", "issue": why,
                                    "action": "kept_flagged (both registered)", "related": "",
                                    "suggestion": "if they are one platform, keep one row in proposals.csv "
                                                  "(clear the other's approved_code)"})
        for f in F:
            if f["tenant_key"]:
                self.review.append({"source_rows": f["source_rows"], "name": f["name"], "url": f["url"],
                                    "domain": f["domain"], "issue": f"tenant {f['tenant_key']} on shared host {f['domain']}",
                                    "action": "kept_flagged (registered as its own platform)", "related": "",
                                    "suggestion": "fine if this buyer is tracked separately; else clear its approved_code"})
            elif any(x.startswith("different names at the same address") for x in f["review"]):
                self.review.append({"source_rows": f["source_rows"], "name": f["name"], "url": f["url"], "domain": f["domain"],
                                    "issue": next(x for x in f["review"] if x.startswith("different names")),
                                    "action": "merged (same address), flagged", "related": f["aliases"],
                                    "suggestion": "rename with platforms.py rename if another name is better"})

    # -- output --
    def clean_rows(self) -> list[dict]:
        out = []
        for f in self.final:
            row = {k: f.get(k, "") for k in CLEAN_COLUMNS}
            row["review_note"] = "; ".join(dict.fromkeys(f["review"]))
            row["notes"] = "; ".join(dict.fromkeys(f["notes_l"]))
            out.append(row)
        return out

    def removed_rows(self) -> list[dict]:
        final_by_base = {id(f["_base"]): f for f in self.final}
        out = []
        for r in sorted(self.removed, key=lambda x: x["source_row"]):
            f = final_by_base.get(id(r["_into"]))
            if f:
                r["merged_into_name"] = f"{r['_into']['name']} (platform '{f['name']}')" if f["name"] != r["_into"]["name"] else f["name"]
            out.append({k: r[k] for k in REMOVED_COLUMNS})
        return out


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def clean_file(path: str, out_dir: str) -> dict:
    rows, msgs = P.read_rows(path)
    c = Cleaner(rows).run()
    os.makedirs(out_dir, exist_ok=True)
    clean, removed = c.clean_rows(), c.removed_rows()
    write_csv(os.path.join(out_dir, "platforms_clean.csv"), CLEAN_COLUMNS, clean)
    write_csv(os.path.join(out_dir, "duplicates_removed.csv"), REMOVED_COLUMNS, removed)
    write_csv(os.path.join(out_dir, "needs_review.csv"), REVIEW_COLUMNS, c.review)
    kinds = {}
    for r in removed:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {"messages": msgs, "input_rows": len(rows), "platforms": len(clean), "removed": len(removed),
            "kinds": kinds, "held_back": sum(1 for r in c.review if r["action"] == "held_back"),
            "flagged": sum(1 for r in c.review if r["action"] != "held_back"), "cleaner": c}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--out-dir", default="platforms")
    a = ap.parse_args(argv)
    try:
        res = clean_file(a.input, a.out_dir)
    except P.PlatformError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for m in res["messages"]:
        print(m)
    print(f"{res['input_rows']} data rows -> {res['platforms']} platforms; {res['removed']} rows folded "
          f"({', '.join(f'{n} {k}' for k, n in sorted(res['kinds'].items()))}); "
          f"{res['held_back']} held back, {res['flagged']} flagged for review")
    print(f"wrote {a.out_dir}/platforms_clean.csv, duplicates_removed.csv, needs_review.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
