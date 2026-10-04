"""Platform registry tooling for Tender & RFP Cockpit (v5).

Each tender platform has a three-letter code (source_platform.code) that becomes part of every
accession number (TRC-26-PLC-000001-L), so codes are chosen carefully and never change once used.

Workflow for a list of platforms (CSV or XLSX, one platform per row):

    python platforms.py clean my_platforms.xlsx --out-dir platforms     # optional (v6): deduplicate a raw list
    python platforms.py suggest platforms/platforms_clean.csv --out proposals.csv [--prefill]
    #   open proposals.csv: check status/clash_details, put your choice in approved_code if needed
    python platforms.py apply proposals.csv                 # registers rows as 'proposed'
    python platforms.py apply proposals.csv --activate      # ... or straight to 'active'
    python platforms.py activate SAM                         # ingestion only accepts active platforms

Other commands:
    python platforms.py list [--all]
    python platforms.py retire CODE          # permanent: a retired code is never reused
    python platforms.py rename CODE [--name ..] [--url ..] [--short-name ..]
    python platforms.py update CODE [--priority N] [--country XX] [--data-format F] [--notes ..] [--tenant-key K]
    python platforms.py seed-sql proposals.csv --out 006_seed_platforms.sql   # migration from approved rows
    python platforms.py seed-sql --schema-only --out 006_schema.sql          # only the tenant schema (apply path)

Input columns (header matching ignores case, spaces, '-' and '_'; common synonyms accepted):
    name (required), url, short_name, country, data_format, priority, notes
`suggest` works without a database (it then only knows the built-in seed codes); with
DATABASE_URL set it also compares against every registered platform.

Library:
    suggest_code(name, url=None, taken=(), short_name=None) -> Suggestion   (pure, deterministic)
    resolve_platform(conn, url=None, code=None, name=None) -> Platform      (used by ingest.py)
"""
from __future__ import annotations

import argparse
import csv
import difflib
import io
import itertools
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# ---------- configuration ----------

DATA_FORMATS = ("codice", "eforms", "ocds", "html", "pdf", "other")
STATUSES = ("proposed", "active", "retired")
SEED_CODES = ("PLC", "FTS", "TED", "MAN")          # seeded by 005_platforms_lots.sql
RESERVED = frozenset({"MAN", "TRC"})               # MAN = manual entry, TRC = our own prefix
# Awkward or offensive combinations never suggested (a person may still approve one explicitly)
BLOCKLIST = frozenset({
    "ASS", "CUM", "DIE", "FAG", "FCK", "FUC", "FUK", "GAY", "JEW", "KKK", "KIL", "NAZ", "NIG",
    "PEE", "PIS", "POO", "SEX", "SHT", "SUX", "TIT", "WTF", "XXX", "ZOG", "NUL", "ERR", "UNK", "TMP",
    "BUM", "FAT", "GOD", "HOE", "LSD", "PMS", "STD", "UTI", "IED", "SOB", "PIG", "RIP", "DUD", "HIV",
    "CIA", "KGB", "ISI", "NUN", "WAR", "GUN", "POT", "FAK", "SUK", "VAG", "DIK", "COK", "JAP",
})
# Not blocked, but worth a second look when suggested (common words / loaded abbreviations)
WATCHLIST = frozenset({
    "BAD", "BAN", "BUG", "BUY", "CON", "COP", "CRY", "DEB", "DOG", "DUM", "EGO", "EVL", "FUN",
    "HAG", "ILL", "LIE", "MAD", "MOB", "NAG", "NIL", "PAN", "PEN", "PHD", "POW", "RAT", "SAD", "SIN",
    "SPY", "TAX", "VAT", "VIP", "WET", "WIN", "ZAP", "LOL", "OMG", "BRB", "IOU", "TBD", "TBA",
})
DEFAULT_PRIORITY = 10
COUNTRY_FIXES = {"UK": "GB", "EL": "GR"}            # common non-ISO codes -> ISO 3166-1 alpha-2
# Country names (English + a few local forms) and alpha-3 codes -> ISO 3166-1 alpha-2. Parts of a
# country (Scotland, Wales, ...) map to the country; the part is kept as a region note.
COUNTRY_NAMES = {
    "andorra": "AD", "argentina": "AR", "australia": "AU", "austria": "AT", "osterreich": "AT",
    "belgium": "BE", "belgique": "BE", "belgie": "BE", "brazil": "BR", "brasil": "BR", "bulgaria": "BG",
    "canada": "CA", "chile": "CL", "china": "CN", "colombia": "CO", "croatia": "HR", "cyprus": "CY",
    "czech republic": "CZ", "czechia": "CZ", "denmark": "DK", "danmark": "DK", "estonia": "EE",
    "finland": "FI", "suomi": "FI", "france": "FR", "germany": "DE", "deutschland": "DE", "greece": "GR",
    "hong kong": "HK", "hungary": "HU", "iceland": "IS", "india": "IN", "indonesia": "ID", "iraq": "IQ",
    "ireland": "IE", "republic of ireland": "IE", "israel": "IL", "italy": "IT", "italia": "IT",
    "japan": "JP", "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE", "latvia": "LV", "liechtenstein": "LI",
    "lithuania": "LT", "luxembourg": "LU", "malaysia": "MY", "malta": "MT", "mexico": "MX",
    "monaco": "MC", "morocco": "MA", "netherlands": "NL", "the netherlands": "NL", "holland": "NL",
    "nederland": "NL", "new zealand": "NZ", "nigeria": "NG", "norway": "NO", "norge": "NO",
    "philippines": "PH", "poland": "PL", "polska": "PL", "portugal": "PT", "qatar": "QA",
    "romania": "RO", "saudi arabia": "SA", "serbia": "RS", "singapore": "SG", "slovakia": "SK",
    "slovenia": "SI", "south africa": "ZA", "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "spain": "ES", "espana": "ES", "sri lanka": "LK", "sweden": "SE", "sverige": "SE",
    "switzerland": "CH", "schweiz": "CH", "suisse": "CH", "taiwan": "TW", "thailand": "TH",
    "turkey": "TR", "turkiye": "TR", "ukraine": "UA", "united arab emirates": "AE", "uae": "AE",
    "united kingdom": "GB", "great britain": "GB", "britain": "GB", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB", "united states": "US", "united states of america": "US",
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "america": "US", "united states virgin islands": "VI",
    "us virgin islands": "VI", "vietnam": "VN", "viet nam": "VN",
}
COUNTRY_PARTS = {"england", "scotland", "wales", "northern ireland"}
ALPHA3 = {"AUT": "AT", "BEL": "BE", "BRA": "BR", "CAN": "CA", "CHE": "CH", "CZE": "CZ", "DEU": "DE",
          "DNK": "DK", "ESP": "ES", "EST": "EE", "FIN": "FI", "FRA": "FR", "GBR": "GB", "GRC": "GR",
          "HRV": "HR", "IRL": "IE", "ITA": "IT", "LTU": "LT", "LVA": "LV", "NLD": "NL", "NOR": "NO",
          "NZL": "NZ", "POL": "PL", "PRT": "PT", "ROU": "RO", "SVK": "SK", "SVN": "SI", "SWE": "SE",
          "USA": "US", "AUS": "AU", "HKG": "HK", "TWN": "TW", "AND": "AD", "LUX": "LU"}
# Not countries: kept as a region note, country left empty
REGIONS = {"europe", "eu", "european union", "global", "worldwide", "international", "nordics",
           "nordic", "scandinavia", "emea", "apac", "latam", "dach", "benelux", "north america",
           "americas", "asia", "africa", "middle east", "oceania"}
# Country-code TLDs that are used as generic TLDs: never used to infer a country
GENERIC_CCTLDS = {"io", "co", "me", "tv", "ai", "cc", "ws", "fm", "ly", "to", "gg", "la", "sh", "ac",
                  "im", "nu", "md", "so", "vc", "ag", "app", "eu", "su", "tk", "ml", "ga", "cf"}
# Query parameters that name a customer ("tenant") on a shared procurement host
TENANT_PARAMS = ("companyid", "custorg", "customerorg", "agencyenglishabbreviation", "b", "orgid",
                 "tenant", "tenantid", "buyerid", "orgname")
# First path segments that never identify a tenant (language, login pages, product folders)
GENERIC_PATH = {"", "en", "fr", "de", "es", "it", "pt", "pl", "nl", "home", "inicio", "start",
                "index", "default", "login", "logon", "signin", "public", "content", "search",
                "portal", "netserver", "apps", "app", "page.aspx", "web", "webapp", "irj", "psc",
                "bso", "vmpcenter", "sle_internet", "-", "ctm", "epps", "esop", "oa_html", "pages",
                "module", "opportunities", "tenders", "bids", "welcome", "main", "site", "s",
                "procurement", "purchasing", "supplier", "vendor", "eprocurement", "auth", "callback"}

STOPWORDS = frozenset("""
the of and for a an in on to by at with from
de del la las el los y e en para por con al lo
le les des du d l et pour au aux sur
der die das dem den und fur fuer von vom zu zur zum im am bei
il gli di della dei delle degli ed nel nella
o os as do da dos das em no na nos nas
""".split())
GENERIC = frozenset("""
portal portals portail portale portais platform platforms plataforma plataformas plateforme plattform
piattaforma sector sektor settore secteur site website web online www gov com org net service services
servicio servicios servizi servizio system sistema systeme official oficial
aka only also formerly known including includes etc inc ltd llc gmbh
""".split())
# Domain labels that never identify a platform
GENERIC_LABELS = frozenset({"www", "www2", "web", "app", "apps", "portal", "service", "services", "my",
                            "gov", "go", "co", "com", "org", "net", "eu", "e", "en", "es", "fr", "de",
                            "it", "pt", "uk", "nl", "ie", "public", "tenders", "tender"})
TLD_LIKE = frozenset({"gov", "gob", "gouv", "co", "com", "org", "net", "eu", "uk", "es", "fr", "de",
                      "it", "pt", "nl", "ie", "be", "at", "ch", "lu", "se", "dk", "fi", "no", "pl",
                      "us", "int", "edu", "info", "biz", "io"})


class PlatformError(ValueError):
    """Unknown or unusable platform, or a registry rule was broken."""


# ---------- text helpers ----------

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _host_of(url: str | None) -> tuple[str | None, bool]:
    """-> (host without www./port, repaired?). Repairs junk glued to the host by broken exports,
    e.g. 'https://bieterportal.dfg.de-tid=b4496...' -> bieterportal.dfg.de (repaired=True)."""
    if not url or not str(url).strip():
        return None, False
    s = str(url).strip().strip("()<>\"' ")
    m = re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*:)?//([^/?#\s]*)", s) or re.match(r"^([^/?#\s]*)", s)
    host = m.group(1) if m else ""
    host = host.rsplit("@", 1)[-1]
    host = re.sub(r":\d*$", "", host).strip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    host = re.sub(r"^www\d*\.", "", host)
    ok = lambda h: bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.([a-z]{2,63}|xn--[a-z0-9-]+)", h))
    if ok(host):
        return host, False
    cut = re.match(r"[a-z0-9.-]*", host).group(0).strip(".-")
    labels = cut.split(".")
    if len(labels) >= 2 and "-" in labels[-1]:
        labels[-1] = labels[-1].split("-", 1)[0]
    cut = re.sub(r"^www\d*\.", "", ".".join(labels))
    return (cut, True) if ok(cut) else (None, False)


def domain_of(url: str | None) -> str | None:
    """'https://WWW.Ted.Europa.EU:443/en/' -> 'ted.europa.eu'. Also accepts a bare host ('SAM.gov')
    and repairs junk glued onto the host ('https://x.dfg.de-tid=...' -> 'x.dfg.de')."""
    return _host_of(url)[0]


_URL_START = re.compile(r"(?i)(?:https?://|www\d*\.)[^\s<>\"'|]+")
_BARE_HOST = re.compile(r"(?i)\(?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?:[/?#][^\s]*)?)\)?")
_HOST_IN_PARENS = re.compile(r"(?i)\(((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63})\)")


def looks_like_url(text: str | None) -> bool:
    """True for 'https://..', 'www...', '(https://..' (not for brand names such as 'eVergabe.de')."""
    return bool(text) and bool(re.match(r"(?i)^\(?\s*(https?://|www\d*\.)", str(text).strip()))


def extract_url(text: str | None) -> str | None:
    """The URL in a cell: 'https://x/y (IRELAND)' -> 'https://x/y'; '(https://x' -> 'https://x';
    'SAM.gov' -> 'SAM.gov'; 'Department of X (sciquest.com)' -> 'sciquest.com'. None if no URL."""
    s = str(text or "").strip()
    if not s:
        return None
    m = _URL_START.search(s)
    if m:
        return m.group(0).rstrip(").,;:")
    m = _BARE_HOST.fullmatch(s)
    if m:
        return m.group(1)
    m = _HOST_IN_PARENS.search(s)
    return m.group(1) if m else None


REDIRECT_PARAMS = ("redirect_uri", "return_to", "returnurl", "redirecturl", "redirect", "target", "url", "u")


def unwrap_url(url: str) -> tuple[str, str | None]:
    """Undo link wrappers: Proofpoint urldefense (v2 and v3), Outlook safelinks, and login / OAuth
    pages whose redirect parameter holds the real (absolute) address, followed up to 3 times.
    -> (url, note or None)."""
    from urllib.parse import parse_qsl, unquote
    orig = url
    for _ in range(3):
        try:
            parts = urlsplit(url if "//" in url else "//" + url)
        except ValueError:
            break
        host = (parts.hostname or "").lower()
        q = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
        nxt = None
        if host.endswith("urldefense.proofpoint.com") and "u" in q:            # v2: u=https-3A__host_path
            nxt = re.sub(r"-([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), q["u"].replace("_", "/"))
        elif host.endswith("urldefense.com") and "/v3/__" in url:
            m = re.search(r"/v3/__(.+?)__;", url)
            nxt = m.group(1) if m else None
        elif host.endswith("safelinks.protection.outlook.com") and "url" in q:
            nxt = q["url"]
        elif re.search(r"(?i)login|logon|signin|sso|oauth|authorize", parts.path):
            for k in REDIRECT_PARAMS:
                v = unquote(q.get(k, ""))
                if re.match(r"(?i)^https?://[^/?#]+", v):
                    nxt = v
                    break
        if not nxt or nxt == url:
            break
        url = nxt
    return (url, None) if url == orig else (url, f"url {orig[:60]!r}... is a wrapper/login redirect; using its target {url[:80]!r}")


def normalise_url(text: str | None) -> tuple[str | None, str | None, list[str]]:
    """-> (url, domain, notes). The url is the one in the text, unwrapped (urldefense, safelinks,
    login redirects) and with a broken host repaired (then scheme://host, path dropped)."""
    url = extract_url(text)
    if not url:
        return None, None, []
    notes = []
    if url != str(text).strip():
        notes.append(f"url taken from {str(text).strip()[:80]!r}")
    url, note = unwrap_url(url)
    if note:
        notes.append(note)
    host, repaired = _host_of(url)
    if repaired and host:
        scheme = re.match(r"(?i)^([a-z][a-z0-9+.-]*:)?//", url)
        notes.append(f"url {url[:80]!r} looked broken; host read as {host}")
        url = (scheme.group(0) if scheme else "https://") + host
    return url, host, notes


def url_tenants(url: str | None) -> list[str]:
    """Tenant keys a URL can match on a shared host: path prefixes '/a', '/a/b', '/a/b/c' (lower
    case) and 'param=value' for the TENANT_PARAMS in its query (lower case)."""
    if not url:
        return []
    s = str(url).strip()
    if "//" not in s:
        s = "//" + s
    try:
        parts = urlsplit(s)
    except ValueError:
        return []
    out = []
    segs = [x.lower() for x in parts.path.strip("/").split("/") if x][:3]
    for i in range(len(segs)):
        out.append("/" + "/".join(segs[:i + 1]))
    for kv in re.split(r"[&;]", parts.query or ""):
        k, _, v = kv.partition("=")
        if k.lower() in TENANT_PARAMS and v:
            out.append(f"{k.lower()}={v.strip().lower()}")
    return out


def tenant_of(url: str | None) -> tuple[str | None, str | None]:
    """The URL's own tenant candidates: (query tenant 'param=value' or None, path tenant
    '/segment' or None when the first segment is generic, see GENERIC_PATH)."""
    keys = url_tenants(url)
    q = next((k for k in keys if "=" in k), None)
    p = None
    for k in (k for k in keys if k.startswith("/")):          # '/a', '/a/b', '/a/b/c'
        last = k.rsplit("/", 1)[-1]
        if "." in last or last in GENERIC_PATH:                # a page ('index.html') ends the search
            if "." in last:
                break
            continue
        p = k
        break
    return q, p


def cctld_country(domain: str | None) -> str | None:
    """ISO country of a country-code TLD (.de -> DE, .uk -> GB); None for gTLDs and ccTLDs used
    generically (.io, .co, .tv, .eu, ...)."""
    if not domain:
        return None
    tld = domain.rsplit(".", 1)[-1].lower()
    if len(tld) != 2 or tld in GENERIC_CCTLDS:
        return None
    return "GB" if tld == "uk" else tld.upper()


def parse_country(text: str | None) -> dict:
    """'Germany' -> DE; 'UK' -> GB; 'Portugal/Spain' -> two countries; 'Europe' -> region.
    -> {'country': ISO if exactly one, 'countries': [...], 'regions': [...], 'unknown': [...]}."""
    countries, regions, unknown = [], [], []
    raw = str(text or "").strip()
    for tok in re.split(r"\s*(?:[;,/|+]|\band\b|&)\s*", raw, flags=re.I):
        t = tok.strip().strip(".")
        if not t:
            continue
        low = strip_accents(t).lower()
        iso = None
        if low in REGIONS:
            regions.append(t)
            continue
        if re.fullmatch(r"[A-Za-z]{2}", t):
            iso = COUNTRY_FIXES.get(t.upper(), t.upper())
        elif low in COUNTRY_NAMES:
            iso = COUNTRY_NAMES[low]
            if low in COUNTRY_PARTS:
                regions.append(t)
        elif t.upper() in ALPHA3 and t.isupper():
            iso = ALPHA3[t.upper()]
        if iso:
            if iso not in countries:
                countries.append(iso)
        elif low in REGIONS:
            regions.append(t)
        else:
            unknown.append(t)
    return {"country": countries[0] if len(countries) == 1 else None, "countries": countries,
            "regions": regions, "unknown": unknown}


def name_key(name: str | None) -> str:
    """Comparison key for names: accents/case/punctuation/stopwords/generic words ignored."""
    keep = [w.lower() for w in _significant(name or "")]
    return " ".join(keep or [w.lower() for w in _raw_words(name or "")])


def _tokens(name: str) -> list[tuple[str, bool]]:
    """(word, standalone) pairs with the original case (accents stripped). Domain-looking chunks
    keep only their meaningful labels ('SAM.gov' -> 'SAM'); camelCase is split ('eTendersNI' ->
    e Tenders NI) and such glued pieces are never treated as stopwords."""
    out = []
    for chunk in re.split(r"[\s/()\[\],;:|–—\-+&]+", strip_accents(name)):
        if not chunk:
            continue
        if "." in chunk.strip("."):
            labels = [x for x in chunk.split(".") if x]
            labels = [x for i, x in enumerate(labels) if not (i > 0 and x.lower() in TLD_LIKE)]
            labels = [x for x in labels if x.lower() not in ("www", "www2")]
        else:
            labels = [chunk]
        for lab in labels:
            parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", lab)    # digits dropped
            out += [(w, len(parts) == 1) for w in parts]
    return out


def _raw_words(name: str) -> list[str]:
    return [w for w, _ in _tokens(name)]


def _significant(name: str) -> list[str]:
    return [w for w, alone in _tokens(name)
            if not alone or (w.lower() not in STOPWORDS and w.lower() not in GENERIC)]


VOWELS = set("AEIOU")


def _one_word(word: str) -> tuple[str | None, bool]:
    """First letter + following consonants; if there are fewer than two, fill with the earliest
    other letters (keeping their order). Returns (code, padded)."""
    w = re.sub(r"[^A-Z]", "", word.upper())
    if len(w) < 3:
        return None, True
    cons = [i for i in range(1, len(w)) if w[i] not in VOWELS]
    if len(cons) >= 2:
        return w[0] + w[cons[0]] + w[cons[1]], False
    rest = [i for i in range(1, len(w)) if i not in cons]
    picks = sorted(cons + rest[:2 - len(cons)])
    return w[0] + w[picks[0]] + w[picks[1]], True


def _two_words(a: str, b: str) -> str | None:
    a, b = re.sub(r"[^A-Z]", "", a.upper()), re.sub(r"[^A-Z]", "", b.upper())
    if not a or not b:
        return None
    for src in (b[1:], a[1:]):
        for ch in src:
            if ch not in VOWELS:
                return a[0] + b[0] + ch
    rest = b[1:] or a[1:]
    return a[0] + b[0] + rest[0] if rest else None


def _generated(words: list[str]) -> list[str]:
    """All codes S[0]+S[i]+S[j] (i<j) from the letters of the significant words, ranked: more word
    initials first, then more consonants, then earlier letters."""
    letters = []
    for w in words:
        w = re.sub(r"[^A-Z]", "", w.upper())
        letters += [(ch, k == 0) for k, ch in enumerate(w)]
    if len(letters) < 3:
        return []
    first = letters[0][0]
    cands = []
    for i, j in itertools.combinations(range(1, len(letters)), 2):
        (a, ia), (b, ib) = letters[i], letters[j]
        score = (-(ia + ib), -((a not in VOWELS) + (b not in VOWELS)), i, j)
        cands.append((score, first + a + b))
    seen, out = set(), []
    for _, c in sorted(cands):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


@dataclass
class Suggestion:
    code: str
    alternatives: list[str]
    reason: str
    method: str                      # short_name | acronym | domain | acronym_long | initials | consonants | bracket_acronym | generated | fallback
    weak: bool = False               # a person should look at it (needs_review)
    preferred: str | None = None     # first choice before clashes were avoided
    skipped: dict = field(default_factory=dict)   # code -> why it was not used (taken/reserved/blocked)


def _clean_code(c) -> str | None:
    c = re.sub(r"[^A-Za-z]", "", strip_accents(str(c or ""))).upper()
    return c if len(c) == 3 else None


def suggest_code(name: str, url: str | None = None, taken=(), short_name: str | None = None,
                 n_alternatives: int = 5) -> Suggestion:
    """Deterministic: same input -> same output. `taken` = codes already used (DB + this file).
    Order of preference: the short_name if given (3 letters as is, longer: first letter +
    consonants, PLACSP -> PLC); a 3-letter acronym in capitals in the name (TED, SAM); a
    3-letter domain label (ted.europa.eu -> TED); a longer acronym (BOAMP -> BMP); initials of the significant words (3+ words: first three
    initials; 2 words: both initials + next consonant; 1 word: first letter + next consonants);
    then other letter combinations; last resort the first two letters + A..Z (codes are letters
    only, ^[A-Z]{3}$, so no digits). Reserved (MAN, TRC), blocklisted and taken codes are skipped."""
    if not name or not str(name).strip():
        raise PlatformError("name is required to suggest a code")
    taken = {str(t).strip().upper() for t in taken if t}
    words = _significant(name)
    raw = _raw_words(name)
    cands: list[tuple[str, str, str, bool]] = []          # (code, method, reason, weak)

    def add(code, method, reason, weak=False):
        code = _clean_code(code)
        if code and all(code != c[0] for c in cands):
            cands.append((code, method, reason, weak))

    sn = strip_accents(str(short_name or "")).strip()
    sn_letters = re.sub(r"[^A-Za-z]", "", sn).upper()
    if len(sn_letters) == 3:
        add(sn_letters, "short_name", f"short name '{sn}'")
    elif len(sn_letters) > 3:               # a short name the user gave outranks anything guessed
        c, padded = _one_word(sn_letters)
        add(c, "short_name", f"first letter + consonants of short name '{sn}'", padded)
    # acronyms in capitals; a bracket that holds more than the acronym ('(SAP Ariba)', '(aka JAGGAER)')
    # names a vendor or a remark, not this platform, so its words are not used as acronyms
    # A bracket holding only an acronym ('Battelle (PNNL)') counts, but after the initials.
    def caps(text):
        return [w for w in _raw_words(text) if w.isupper() and len(w) >= 3 and w.lower() not in STOPWORDS | GENERIC]
    acronyms = caps(re.sub(r"\([^()]*\)", " ", name))
    bracket_acr = [a for m in re.findall(r"\(\s*([A-Z0-9&.-]+)\s*\)", name) for a in caps(m) if a not in acronyms]
    for a in acronyms:
        if len(a) == 3:
            add(a, "acronym", f"acronym '{a}' in the name")
    dom = domain_of(url)
    if dom:
        for lab in dom.split("."):
            if lab in GENERIC_LABELS:
                continue
            if re.fullmatch(r"[a-z]{3}", lab):
                add(lab, "domain", f"domain label '{lab}' of {dom}")
            break                                    # only the first meaningful label
    for a in acronyms:
        if len(a) > 3:
            c, padded = _one_word(a)
            add(c, "acronym_long", f"first letter + consonants of acronym '{a}'", padded)
    if len(words) >= 3:
        add("".join(w[0] for w in words[:3]), "initials",
            "initials of " + " ".join(words[:3]))
    elif len(words) == 2:
        add(_two_words(*words), "initials", f"initials of {words[0]} {words[1]} + next consonant")
    elif len(words) == 1:
        c, padded = _one_word(words[0])
        add(c, "consonants", f"first letter + consonants of '{words[0]}'", padded)
    for a in bracket_acr:
        c, padded = (a, False) if len(a) == 3 else _one_word(a)
        add(c, "bracket_acronym", f"acronym '{a}' in brackets" if len(a) == 3 else
            f"first letter + consonants of acronym '{a}' in brackets", padded)
    for c in _generated(words or raw):
        add(c, "generated", "other letter combination from the name")
    base = re.sub(r"[^A-Z]", "", "".join(words or raw).upper())
    if len(base) >= 2:
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            add(base[:2] + ch, "fallback", f"first two letters '{base[:2]}' + {ch}", True)
    if not cands:
        raise PlatformError(f"cannot build a code from {name!r}: no letters")
    skipped, usable = {}, []
    for code, method, reason, weak in cands:
        if code in RESERVED:
            skipped[code] = "reserved"
        elif code in BLOCKLIST:
            skipped[code] = "blocklisted"
        elif code in taken:
            skipped[code] = "taken"
        else:
            usable.append((code, method, reason, weak))
    if not usable:
        raise PlatformError(f"no free code for {name!r}; choose one by hand")
    code, method, reason, weak = usable[0]
    preferred = cands[0][0]
    if code != preferred:
        why = ", ".join(f"{c} {w}" for c, w in skipped.items() if cands.index(next(x for x in cands if x[0] == c)) < cands.index(usable[0]))
        reason += f" (skipped: {why})" if why else ""
    if not words:
        weak = True
        reason += "; name has no significant words"
    return Suggestion(code, [c[0] for c in usable[1:1 + n_alternatives]], reason, method, weak, preferred, skipped)


# ---------- input files ----------

COLUMNS = {
    "name": ("name", "platform", "platformname", "platform name", "nombre", "nom", "title"),
    "url": ("url", "website", "web", "link", "homepage", "site", "address"),
    "short_name": ("shortname", "short", "abbreviation", "abbrev", "acronym", "alias"),
    "country": ("country", "countrycode", "pais", "pays", "land", "iso"),
    "data_format": ("dataformat", "format", "feedformat"),
    "priority": ("priority", "prio"),
    "notes": ("notes", "note", "comment", "comments", "remarks"),
    "importance": ("importance", "relevance", "tier", "rank", "type", "category"),
    "tenant_key": ("tenantkey", "tenant"),
    "domain": ("domain",),                                    # recomputed from url
    # written by `platforms.py clean`, read back by `suggest`
    "sector": ("sector",), "countries": ("countries",), "country_source": ("countrysource",),
    "aliases": ("aliases",), "source_rows": ("sourcerows",), "review_note": ("reviewnote",),
}
OPTIONAL_REPORTED = ("url", "short_name", "country", "data_format", "priority", "notes")
# Header keywords tried in this order when no synonym matches exactly ('Name of Tender Platform'
# -> name, 'Platform URL' -> url, 'Country of operation' -> country)
KEYWORDS = (("url", ("url", "website", "homepage", "weblink", "enlace", "lien", "adresse", "address")),
            ("country", ("country", "pais", "pays", "nation")),
            ("short_name", ("shortname", "abbrev", "acronym")),
            ("data_format", ("dataformat", "format")),
            ("priority", ("priority",)),
            ("importance", ("importance", "relevance", "tier", "sector", "category")),
            ("notes", ("note", "comment", "remark")),
            ("name", ("name", "platform", "nombre", "plataforma", "plattform", "portal")))
LINK_SUFFIX = re.compile(r"(?:link|hyperlink|href|links)$")


def _hkey(h) -> str:
    return re.sub(r"[\s_\-.()]+", "", strip_accents(str(h or "")).replace("\ufeff", "").strip().lower())


def _field_of(hk: str) -> str | None:
    lookup = {_hkey(x): f for f, syns in COLUMNS.items() for x in syns}
    if hk in lookup:
        return lookup[hk]
    for f, words in KEYWORDS:
        if any(w in hk for w in words):
            return f
    return None


def map_headers(headers: list) -> tuple[dict, list, list]:
    """-> ({field: column index}, missing optional fields, unknown columns). Spreadsheet exports
    with a hyperlink column next to a text column ('URL' + 'URL_link', 'Name' + 'Name_link') map
    the second one to 'url_link' / 'name_link'; the link wins over the text for the URL."""
    found, unknown = {}, []
    for i, h in enumerate(headers):
        hk = _hkey(h)
        if not hk:
            continue
        f = None
        base = LINK_SUFFIX.sub("", hk)
        if base != hk and base:
            bf = _field_of(base)
            if bf in ("url", "name"):
                f = bf + "_link"
        f = f or _field_of(hk)
        if f and f not in found:
            found[f] = i
        else:
            unknown.append(str(h).strip() + (" (duplicate)" if f else ""))
    if "name" not in found:
        raise PlatformError(f"no 'name' column found (headers: {headers})")
    missing = [f for f in OPTIONAL_REPORTED if f not in found and not (f == "url" and "url_link" in found)]
    return found, missing, unknown


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "UTF-16"
    try:
        return raw.decode("utf-8-sig"), "UTF-8" + (" with BOM" if raw.startswith(b"\xef\xbb\xbf") else "")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "Windows-1252"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "Latin-1"


def _delimiter(text: str) -> str:
    """The delimiter that splits the first lines into the most consistent number (>1) of columns."""
    lines = [ln for ln in text.splitlines() if ln.strip()][:60]
    best, best_score = ",", -1.0
    for d in (",", ";", "\t", "|"):
        counts = [len(r) for r in csv.reader(lines, delimiter=d)]
        if not counts or max(counts) < 2:
            continue
        common = max(set(counts), key=counts.count)
        score = counts.count(common) / len(counts) + (0.01 * common if common > 1 else -1)
        if score > best_score:
            best, best_score = d, score
    return best


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "").replace("\u00a0", " ")).strip().strip("\"'").strip()
    return name.rstrip(":;,").strip()


def read_rows(path: str) -> tuple[list[dict], list[str]]:
    """Read CSV (',' ';' tab or '|'; UTF-8 with or without BOM, UTF-16, else Windows-1252/Latin-1)
    or XLSX (first sheet). Returns (rows with the known fields + 'row' number + 'flags', messages).
    Handles spreadsheet exports: hyperlink columns, repeated header rows between sections, URLs
    hidden behind link text, names that are URLs, rows without a name (named after the domain)."""
    msgs = []
    if path.lower().endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError as e:
            raise PlatformError("reading .xlsx needs openpyxl (pip install openpyxl)") from e
        wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
        ws = wb.worksheets[0]
        table = []
        for r in ws.iter_rows():
            vals = []
            for c in r:
                v = c.value
                vals.append("" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)))
            links = [(c.hyperlink.target if getattr(c, "hyperlink", None) and c.hyperlink.target else "") for c in r]
            table.append((vals, links))
        if len(wb.worksheets) > 1:
            msgs.append(f"note: only the first sheet ('{ws.title}') was read")
        wb.close()
        # cell hyperlinks become extra '<header>_link' columns, like a CSV export would have
        if table and any(any(l) for _, l in table[1:]):
            hdr = table[0][0]
            link_cols = [i for i in range(len(hdr)) if any(i < len(l) and l[i] for _, l in table[1:])]
            table = [(v + ([f"{hdr[i]}_link" for i in link_cols] if n == 0 else
                           [l[i] if i < len(l) else "" for i in link_cols]), l) for n, (v, l) in enumerate(table)]
        table = [v for v, _ in table]
        msgs.append("read XLSX")
    else:
        text, enc = _decode(open(path, "rb").read())
        delim = _delimiter(text)
        msgs.append(f"read as {enc}, delimiter {delim!r}")
        table = list(csv.reader(io.StringIO(text), delimiter=delim))
    numbered = [(n, r) for n, r in enumerate(table, start=1) if any(str(c).strip() for c in r)]
    if not numbered:
        raise PlatformError(f"{path} is empty")
    header = numbered[0][1]
    cols, missing, unknown = map_headers(header)
    if missing:
        msgs.append("optional columns not found: " + ", ".join(missing))
    if unknown:
        msgs.append("ignored columns: " + ", ".join(unknown))
    hdr_norm = [_hkey(h) for h in header]
    rows, repeated, skipped, nameless = [], [], [], []
    for n, r in numbered[1:]:                    # n = row number in the spreadsheet (header = 1)
        if [_hkey(c) for c in r][:len(hdr_norm)] == hdr_norm:
            repeated.append(n)
            continue
        rec = {f: (str(r[i]).strip() if i < len(r) and r[i] is not None else "") for f, i in cols.items()}
        rec["raw"] = tuple(re.sub(r"\s+", " ", str(c or "")).strip().casefold() for c in r)
        flags = []
        name = _clean_name(rec.get("name", ""))
        url_text = rec.get("url", "")
        url, dom, unotes = None, None, []
        for src in ("url_link", "url", "name_link"):
            if rec.get(src):
                url, dom, unotes = normalise_url(rec[src])
                if url:
                    if src == "url_link" and url_text and not looks_like_url(url_text) and url_text != rec[src]:
                        rec["url_title"] = url_text
                        unotes = []
                    elif src == "name_link":
                        flags.append("url from the name's hyperlink")
                    break
        if not url and looks_like_url(name):
            url, dom, unotes = normalise_url(name)
        if url_text and not looks_like_url(url_text) and not rec.get("url_title") and \
                not (url and url_text.lower() == url.lower()):
            rec["url_title"] = url_text
        if url_text and not url and not dom:
            flags.append(f"url text {url_text[:60]!r} has no URL")
        flags += unotes
        if dom and any("looked broken" in f for f in unotes):
            bad = re.match(r"(?i)^(?:[a-z]+:)?//([^/?#\s]*)", extract_url(rec.get("url_link") or url_text or name) or "")
            if bad and bad.group(1) in name:
                name = name.replace(bad.group(1), dom)
        m = re.search(r"\(\s*(?:https?://|www\.)[^)]*\)", name)
        if m and name.replace(m.group(0), "").strip():
            inner = m.group(0)[1:-1].strip()
            if not url:                                  # the only URL of the row: use it
                url, dom, unotes2 = normalise_url(inner)
                flags += unotes2
                flags.append(f"url {inner!r} taken from the name")
            else:
                flags.append(f"url {inner!r} removed from the name")
            name = _clean_name(name.replace(m.group(0), ""))
        if looks_like_url(name):
            flags.append(f"name was the URL {name!r}; named after the domain")
            name = domain_of(name) or name
        if not name:
            if not dom:
                skipped.append(n)
                continue
            flags.append("no name in the list; named after the domain")
            nameless.append(f"{n} ({dom})")
            name = dom
        rec.update(name=name, url=url or "", row=n, flags=flags)
        rec.pop("url_link", None)
        rec.pop("name_link", None)
        rows.append(rec)
    if repeated:
        msgs.append(f"skipped {len(repeated)} repeated header row(s): {', '.join(map(str, repeated))}")
    if skipped:
        msgs.append(f"skipped {len(skipped)} row(s) with neither a name nor a URL: {', '.join(map(str, skipped))}")
    for x in nameless:
        msgs.append(f"row {x.split()[0]}: no name; named after the domain {x.split()[1][1:-1]} (needs review)")
    return rows, msgs


# ---------- proposals ----------

PROPOSAL_COLUMNS = ("name", "url", "domain", "suggested_code", "alternatives", "reason", "status",
                    "clash_details", "approved_code", "review_note", "short_name", "country", "data_format",
                    "priority", "notes", "source_row", "tenant_key", "countries", "country_source", "sector",
                    "importance", "aliases", "source_rows")
STATUS_ORDER = ("duplicate_platform", "clash_with_existing", "clash_in_file", "needs_review", "ok")


def _similar(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.92)


def _validate_extras(rec: dict) -> list[str]:
    """Normalises country/data_format/priority in place; returns problems for needs_review."""
    probs = []
    c = rec.get("country", "").strip()
    if c and not re.fullmatch(r"[A-Za-z]{2}", c):          # a country name ('Germany') or a list
        ci = parse_country(c)
        if ci["country"] and not ci["unknown"]:
            rec.setdefault("_notes", []).append(f"country {c!r} read as {ci['country']}")
            c = ci["country"]
        elif len(ci["countries"]) > 1:
            rec.setdefault("_notes", []).append(f"several countries {c!r}: country left empty")
            rec["countries"] = rec.get("countries") or ";".join(ci["countries"])
            c = ""
    c = c.upper()
    if c:
        if c in COUNTRY_FIXES:
            probs.append(f"country {c} is not ISO 3166-1 alpha-2, use {COUNTRY_FIXES[c]}")
        elif not re.fullmatch(r"[A-Z]{2}", c):
            probs.append(f"country {rec['country']!r} is not a two-letter ISO code")
    rec["country"] = c
    f = rec.get("data_format", "").strip().lower()
    if f and f not in DATA_FORMATS:
        probs.append(f"data_format {rec['data_format']!r} not one of {'/'.join(DATA_FORMATS)}")
    rec["data_format"] = f
    p = rec.get("priority", "").strip()
    if p and not re.fullmatch(r"\d{1,4}", p):
        probs.append(f"priority {p!r} is not a whole number 0-1000")
    return probs


def _site(dom, tenant) -> tuple:
    return (dom or None, (tenant or "").strip().lower() or None)


METHOD_STRENGTH = {"short_name": 0, "acronym": 0, "domain": 0, "acronym_long": 1, "initials": 1,
                   "bracket_acronym": 2, "consonants": 2, "generated": 3, "fallback": 4}


def build_proposals(rows: list[dict], registered: list[dict], *, prefill: bool = False) -> list[dict]:
    """registered: rows of source_platform (code, name, domain, tenant_key, status). Pure function.
    A platform is identified by (domain, tenant_key); tenant_key is only set for one customer on a
    shared host (see platform_clean.py). prefill=True writes the chosen code into approved_code
    (registered code for a known platform, nothing for an in-file duplicate).
    Codes are handed out strongest claim first, so a well-known code goes to its owner whatever the
    row order: (1) a short name, a 3-letter acronym or domain label (SAM.gov -> SAM), (2) initials,
    (3) consonants of a single word, (4) other combinations; file order within each group."""
    reg_codes = {r["code"]: r for r in registered}
    by_site, by_key = {}, []                  # earlier rows of this file
    out, pending = [], []
    for idx, rec in enumerate(rows):
        rec = dict(rec)
        details, status, review = [], "ok", []
        dom = domain_of(rec.get("url")) if rec.get("url") else None
        tenant = (rec.get("tenant_key") or "").strip().lower()
        site = _site(dom, tenant)
        key = name_key(rec["name"])
        probs = _validate_extras(rec)
        if rec.get("review_note"):
            review.append(rec["review_note"])
        for f in rec.get("flags") or []:               # from read_rows: repaired URL, nameless row, ...
            review.append(f"row {rec.get('row')}: {f}")
            if re.search(r"no name in the list|looked broken|has no URL", f):
                probs.append(f)
        prop = {"name": rec["name"], "url": rec.get("url", ""), "domain": dom or "", "approved_code": "",
                "short_name": rec.get("short_name", ""), "country": rec["country"],
                "data_format": rec["data_format"], "priority": rec.get("priority", ""),
                "notes": "; ".join(filter(None, [rec.get("notes", "")] + rec.get("_notes", []))),
                "source_row": rec.get("row", ""), "tenant_key": tenant, "clash_details": "",
                "_ref": (f"list row {rec['source_rows'].split()[0]}" if rec.get("source_rows") else f"row {rec.get('row', '?')}"),
                **{k: rec.get(k, "") for k in ("countries", "country_source", "sector", "importance", "aliases",
                                               "source_rows")}}
        if rec.get("url") and not dom:
            probs.append(f"url {rec['url']!r} has no usable domain")
        prop["_probs"], prop["_review"], prop["_details"] = probs, review, details
        dup = next((r for r in registered if dom and _site(r.get("domain"), r.get("tenant_key")) == site), None)
        how = "same domain" + (f" and tenant {tenant}" if tenant else "")
        if not dup and not tenant:            # a registered platform under another domain: hold it back
            dup = next((r for r in registered if _similar(key, name_key(r["name"]))), None)
            how = "near-identical name"
        if dup:
            prop["status"] = "duplicate_platform"
            details.append(f"{how} as registered platform {dup['code']} ({dup['name']}, {dup['status']})")
            review.append(f"recognised as the registered platform {dup['code']} ({dup['name']}); no new code")
            prop["_sug"] = Suggestion(dup["code"], [], f"already registered as {dup['code']}", "registered")
            if prefill:
                prop["approved_code"] = dup["code"]
        else:
            earlier = by_site.get(site) if dom else None
            how = "same domain" + (f" and tenant {tenant}" if tenant else "")
            if not earlier and not tenant:
                earlier = next((p for k, p in by_key if _similar(key, k) and not (dom and p["domain"] and p["domain"] != dom)), None)
                how = "near-identical name"
            if earlier:
                prop["status"] = "duplicate_platform"
                prop["_dup_of"] = earlier
                details.append(f"{how} as {earlier['_ref']} ({earlier['name']})")
                review.append(f"duplicate of {earlier['_ref']}: not registered unless you fill approved_code")
            else:
                prop["status"] = "ok"
                first = suggest_code(rec["name"], rec.get("url"), set(reg_codes), rec.get("short_name"))
                pending.append((METHOD_STRENGTH.get(first.method, 5), idx, rec, prop))
                if dom:
                    by_site[site] = prop
                by_key.append((key, prop))
        out.append(prop)
    # hand out codes, strongest claim first
    taken = set(reg_codes)
    assigned: dict[str, dict] = {}
    for _, idx, rec, prop in sorted(pending, key=lambda x: (x[0], x[1])):
        sug = suggest_code(rec["name"], rec.get("url"), taken, rec.get("short_name"))
        prop["_sug"] = sug
        details, review = prop["_details"], prop["_review"]
        pref = sug.preferred
        if pref in reg_codes and pref != sug.code:
            prop["status"] = "clash_with_existing"
            r = reg_codes[pref]
            details.append(f"preferred {pref} is already used by {r['name']} ({r['status']}); suggested {sug.code}")
            review.append(f"clash resolved: {pref} belongs to {r['name']}; chose {sug.code}"
                          + (f" (other options {' '.join(sug.alternatives[:3])})" if sug.alternatives else ""))
        elif pref in assigned and pref != sug.code:
            prop["status"] = "clash_in_file"
            other = assigned[pref]
            details.append(f"preferred {pref} is also wanted by {other['_ref']} ({other['name']}); suggested {sug.code}")
            review.append(f"clash resolved: {pref} went to {other['_ref']} ({other['name'][:40]}); chose {sug.code}"
                          + (f" (other options {' '.join(sug.alternatives[:3])})" if sug.alternatives else ""))
            other["clash_details"] = "; ".join(filter(None, [other["clash_details"],
                                             f"{prop['_ref']} ({rec['name']}) also wanted {pref}"]))
        elif sug.weak:
            prop["status"] = "needs_review"
            details.append("code is not based on clear initials or a known short name")
        if sug.weak:
            review.append(f"weak code {sug.code}: {sug.reason}")
        if sug.method == "fallback":
            review.append(f"last-resort code {sug.code} (first two letters + a letter)")
        if sug.code in WATCHLIST:
            review.append(f"awkward-looking code {sug.code}: consider {' / '.join(sug.alternatives[:2])}")
        taken.add(sug.code)
        assigned[sug.code] = prop
        if prefill:
            prop["approved_code"] = sug.code
    for prop in out:
        if "_dup_of" in prop:
            e = prop.pop("_dup_of")
            prop["_sug"] = Suggestion(e["_sug"].code, [], f"duplicate of {e['_ref']}", "duplicate")
    for prop in out:
        prop.pop("_ref", None)
        sug, probs = prop.pop("_sug"), prop.pop("_probs")
        details, review = prop.pop("_details"), prop.pop("_review")
        if probs:
            details += probs
            review += probs
            if prop["status"] == "ok":
                prop["status"] = "needs_review"
        prop.update(suggested_code=sug.code, alternatives=" ".join(sug.alternatives), reason=sug.reason,
                    clash_details="; ".join(filter(None, details + ([prop["clash_details"]] if prop["clash_details"] else []))),
                    review_note="; ".join(dict.fromkeys(review)))
    return out


def write_proposals(path_or_file, proposals: list[dict]) -> None:
    fh = open(path_or_file, "w", newline="", encoding="utf-8") if isinstance(path_or_file, str) else path_or_file
    try:
        w = csv.DictWriter(fh, fieldnames=PROPOSAL_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for p in proposals:
            w.writerow(p)
    finally:
        if isinstance(path_or_file, str):
            fh.close()


def summary(proposals: list[dict]) -> str:
    counts = {s: sum(p["status"] == s for p in proposals) for s in STATUS_ORDER}
    lines = [f"{len(proposals)} platform(s): " + ", ".join(f"{n} {s}" for s, n in counts.items() if n)]
    for p in proposals:
        flag = (f"  [{p['status']}] {p['clash_details']}" if p["status"] != "ok"
                else f"  (note: {p['clash_details']})" if p["clash_details"] else "")
        lines.append(f"  {p['suggested_code']}  {p['name'][:55]:<55}{flag}")
    return "\n".join(lines)


# ---------- database ----------

@dataclass(frozen=True)
class Platform:
    code: str
    name: str
    short_name: str | None
    url: str | None
    domain: str | None
    country: str | None
    data_format: str
    priority: int
    status: str


_PCOLS = "code, name, short_name, url, domain, country, data_format, priority, status"
_TENANT_CACHE: dict = {}


def has_tenant_key(conn) -> bool:
    """True when 006_seed_platforms.sql added source_platform.tenant_key (cached per connection)."""
    k = id(conn)
    if k not in _TENANT_CACHE:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = "
                        "'source_platform' AND column_name = 'tenant_key' AND table_schema = current_schema())")
            _TENANT_CACHE[k] = cur.fetchone()[0]
    return _TENANT_CACHE[k]


def registered_platforms(conn) -> list[dict]:
    tk = ", tenant_key" if has_tenant_key(conn) else ", NULL::text AS tenant_key"
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_PCOLS}, frozen_at{tk} FROM source_platform ORDER BY code")
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _platform(row) -> Platform:
    return Platform(*row)


def resolve_platform(conn, url: str | None = None, code: str | None = None, name: str | None = None,
                     *, active_only: bool = True) -> Platform:
    """Which registered platform is this? By code if given; else by the URL's domain (the most
    specific registered domain wins: 'https://www.sub.ted.europa.eu:8443/x' -> ted.europa.eu; on a
    shared host a platform registered for the URL's tenant - path prefix or tenant parameter, see
    url_tenants - wins over the site-level platform: a tenant parameter first, then the longest path prefix);
    else by exact name or short name (case, accents and punctuation ignored). Only active
    platforms unless active_only=False. Never invents a code: unknown -> PlatformError."""
    with conn.cursor() as cur:
        how = None
        if code is not None and str(code).strip():
            c = str(code).strip().upper()
            cur.execute(f"SELECT {_PCOLS} FROM source_platform WHERE code = %s", (c,))
            row, how = cur.fetchone(), f"code {c}"
            if not row:
                raise PlatformError(f"unknown platform code {c!r}: register it with platforms.py suggest/apply")
        elif url is not None and str(url).strip():
            url = unwrap_url(extract_url(url) or str(url).strip())[0]
            dom = domain_of(url)
            if not dom:
                raise PlatformError(f"cannot read a domain from url {url!r}")
            labels = dom.split(".")
            options = [".".join(labels[i:]) for i in range(len(labels) - 1)]     # never a bare TLD
            if has_tenant_key(conn):
                tenants = url_tenants(url)
                cur.execute(f"SELECT {_PCOLS} FROM source_platform WHERE domain = ANY(%s) "
                            "AND (tenant_key IS NULL OR tenant_key = ANY(%s)) "
                            "ORDER BY length(domain) DESC, (tenant_key IS NOT NULL) DESC, "
                            "(tenant_key LIKE '%%=%%') DESC, length(coalesce(tenant_key, '')) DESC LIMIT 1", (options, tenants))
            else:
                cur.execute(f"SELECT {_PCOLS} FROM source_platform WHERE domain = ANY(%s) "
                            "ORDER BY length(domain) DESC LIMIT 1", (options,))
            row, how = cur.fetchone(), f"domain {dom}"
            if not row:
                raise PlatformError(f"no registered platform for domain {dom!r} (url {url!r}); "
                                    "register it with platforms.py suggest/apply")
        elif name is not None and str(name).strip():
            k = re.sub(r"[^a-z0-9]", "", strip_accents(str(name)).lower())
            cur.execute(f"SELECT {_PCOLS} FROM source_platform")
            hits = [r for r in cur.fetchall()
                    if k and k in {re.sub(r"[^a-z0-9]", "", strip_accents(x or "").lower()) for x in (r[1], r[2])}]
            if len(hits) > 1:
                raise PlatformError(f"platform name {name!r} matches several platforms: {[h[0] for h in hits]}")
            row, how = (hits[0] if hits else None), f"name {name!r}"
            if not row:
                raise PlatformError(f"no registered platform named {name!r}; use its code or register it "
                                    "with platforms.py suggest/apply")
        else:
            raise PlatformError("a platform code, url or name is required")
    p = _platform(row)
    if active_only and p.status != "active":
        raise PlatformError(f"platform {p.code} ({p.name}, matched by {how}) is {p.status}, not active"
                            + ("; run: python platforms.py activate " + p.code if p.status == "proposed" else ""))
    return p


def apply_proposals(conn, path: str, *, activate: bool = False, actor_note: str | None = None) -> dict:
    """Register the rows of a proposals CSV. A row is used when its status is 'ok' or a person
    filled approved_code (that code wins over suggested_code). Refuses codes that are invalid,
    reserved, already used by another platform, or used twice in the file. Idempotent: a platform
    already registered (same domain, else same name) with the same code is skipped (or activated
    with --activate)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if rows and not {"name", "suggested_code", "status"} <= set(rows[0]):
        raise PlatformError(f"{path} is not a proposals file (run platforms.py suggest first)")
    result = {"inserted": [], "activated": [], "already": [], "skipped": [], "refused": []}
    seen_codes: dict[str, str] = {}
    for r in rows:
        name = (r.get("name") or "").strip()
        approved = (r.get("approved_code") or "").strip()
        code = (approved or r.get("suggested_code") or "").strip().upper()
        label = f"row {r.get('source_row') or '?'} {name[:40]!r}"
        if not approved and r.get("status") != "ok":
            result["skipped"].append(f"{label}: status {r.get('status')} (fill approved_code to accept)")
            continue
        if not re.fullmatch(r"[A-Z]{3}", code):
            result["refused"].append(f"{label}: code {code!r} is not three letters A-Z")
            continue
        if code in RESERVED:
            result["refused"].append(f"{label}: {code} is reserved")
            continue
        if code in seen_codes:
            result["refused"].append(f"{label}: {code} is also used by {seen_codes[code]} in this file")
            continue
        seen_codes[code] = label
        url = (r.get("url") or "").strip() or None
        dom = domain_of(url)
        tenant = (r.get("tenant_key") or "").strip().lower() or None
        if tenant and not dom:
            result["refused"].append(f"{label}: tenant_key {tenant!r} needs a url"); continue
        if tenant and not re.fullmatch(r"(/[^/?#\s]+){1,3}|[a-z0-9_]+=[^&#\s]+", tenant):
            result["refused"].append(f"{label}: tenant_key {tenant!r} is not '/path' or 'param=value'"); continue
        if tenant and not has_tenant_key(conn):
            result["refused"].append(f"{label}: tenant_key needs the 006 schema: apply 006_seed_platforms.sql, "
                                     "or `platforms.py seed-sql --schema-only --out 006_schema.sql` and run it with psql"); continue
        country = (r.get("country") or "").strip().upper() or None
        country = COUNTRY_FIXES.get(country, country)
        fmt = (r.get("data_format") or "").strip().lower() or "other"
        prio = (r.get("priority") or "").strip()
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            result["refused"].append(f"{label}: country {country!r} is not ISO 3166-1 alpha-2"); continue
        if fmt not in DATA_FORMATS:
            result["refused"].append(f"{label}: data_format {fmt!r} unknown"); continue
        if prio and not re.fullmatch(r"\d{1,4}", prio):
            result["refused"].append(f"{label}: priority {prio!r} is not a number"); continue
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("LOCK TABLE source_platform IN SHARE ROW EXCLUSIVE MODE")   # one apply at a time
            same = None
            if dom and has_tenant_key(conn):
                cur.execute("SELECT code, status, name FROM source_platform WHERE domain = %s "
                            "AND tenant_key IS NOT DISTINCT FROM %s", (dom, tenant))
                same = cur.fetchone()
            elif dom:
                cur.execute("SELECT code, status, name FROM source_platform WHERE domain = %s", (dom,))
                same = cur.fetchone()
            if not same and not tenant:            # by name only when one side has no domain
                cur.execute("SELECT code, status, name, domain FROM source_platform")
                same = next((x for x in cur.fetchall() if name_key(x[2]) == name_key(name)
                             and (not dom or x[3] is None)), None)
            if same:
                if same[0] != code:
                    result["refused"].append(f"{label}: already registered as {same[0]} ({same[2]})")
                elif activate and same[1] == "proposed":
                    cur.execute("UPDATE source_platform SET status = 'active' WHERE code = %s", (code,))
                    result["activated"].append(code)
                else:
                    result["already"].append(f"{code} ({same[1]})")
                continue
            cur.execute("SELECT name, status FROM source_platform WHERE code = %s", (code,))
            other = cur.fetchone()
            if other:
                result["refused"].append(f"{label}: code {code} is already used by {other[0]} ({other[1]})")
                continue
            origin = "suggested" if code == (r.get("suggested_code") or "").strip().upper() else "manual"
            cols = ["code", "name", "short_name", "url", "domain", "country", "data_format", "priority",
                    "status", "code_origin", "notes"]
            vals = [code, name, (r.get("short_name") or "").strip() or None, url, dom, country, fmt,
                    int(prio) if prio else DEFAULT_PRIORITY, "active" if activate else "proposed", origin,
                    "; ".join(filter(None, [(r.get("notes") or "").strip(),
                                            actor_note or f"added by platforms.py apply {os.path.basename(path)}"]))]
            if tenant:
                cols.append("tenant_key")
                vals.append(tenant)
            cur.execute(f"INSERT INTO source_platform ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", vals)
            result["inserted"].append(code)
    return result


def set_status(conn, code: str, status: str) -> str:
    if status not in STATUSES:
        raise PlatformError(f"status must be one of {STATUSES}")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT status FROM source_platform WHERE code = %s FOR UPDATE", (code.upper(),))
        row = cur.fetchone()
        if not row:
            raise PlatformError(f"unknown platform code {code!r}")
        if row[0] == status:
            return f"{code.upper()} is already {status}"
        cur.execute("UPDATE source_platform SET status = %s WHERE code = %s", (status, code.upper()))
    return f"{code.upper()}: {row[0]} -> {status}"


def update_platform(conn, code: str, **fields) -> str:
    allowed = {"name", "short_name", "url", "country", "data_format", "priority", "notes", "tenant_key"}
    vals = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "tenant_key" in vals:
        vals["tenant_key"] = vals["tenant_key"].strip().lower() or None
    if not vals:
        raise PlatformError("nothing to change")
    if "url" in vals:
        vals["domain"] = domain_of(vals["url"])
        if vals["url"] and not vals["domain"]:
            raise PlatformError(f"url {vals['url']!r} has no usable domain")
    if "country" in vals:
        c = vals["country"].strip()
        c = parse_country(c)["country"] or c if len(c) > 2 else c
        vals["country"] = COUNTRY_FIXES.get(c.upper(), c.upper()) or None
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"UPDATE source_platform SET {', '.join(k + ' = %s' for k in vals)} WHERE code = %s",
                    (*vals.values(), code.upper()))
        if not cur.rowcount:
            raise PlatformError(f"unknown platform code {code!r}")
    return f"{code.upper()} updated: " + ", ".join(f"{k}={v}" for k, v in vals.items())


# ---------- 006 seed migration ----------

SEED_SQL_HEADER = """-- 006_seed_platforms.sql : Tender & RFP Cockpit v6 - shared-host tenants + the platform list.
-- GENERATED by `python platforms.py seed-sql {src} --out 006_seed_platforms.sql`
-- from {n} approved rows of {src} (sha256 {sha}). Regenerate instead of editing by hand.
--
-- Part 1 (schema): source_platform.tenant_key - a customer on a shared host
--   ('/universityofleeds' on in-tendhost.co.uk, 'companyid=42510' on planetbids.com). A platform is
--   now unique by (domain, tenant_key) instead of by domain. resolve_platform() prefers the tenant.
-- Part 2 (data): registers every approved platform as ACTIVE. Safe to re-run and safe on a database
--   that already has some of them:
--   * same domain + tenant already registered -> left as it is (a 'proposed' one is activated; a
--     retired one is not touched). Its code, name, URL and priority are never changed, so frozen
--     codes stay frozen and earlier renames survive.
--   * the code is already used by a different platform -> that row is NOT registered (WARNING);
--     pick another code and add it with `platforms.py apply`.
--   * otherwise inserted, status active.
-- Priority rule (platform_clean.py): 100 official national portal with a structured feed already
--   parsed (PLC codice, FTS ocds); TED keeps its 005 value 50 (it republishes national notices, so the
--   national source wins); 70 official portal (sector 'Public', government domain or body name);
--   30 private / aggregator / marketplace; 50 not classified (the list says neither); MAN keeps 10.
--   Seeds already in the database are never changed by this file.
-- Runs after 005_platforms_lots.sql (apply.sh order).

BEGIN;

ALTER TABLE source_platform ADD COLUMN IF NOT EXISTS tenant_key text;
COMMENT ON COLUMN source_platform.tenant_key IS
  'Customer on a shared host: a lower-case path prefix (/a, /a/b, /a/b/c) or a tenant parameter (param=value). NULL = the whole host.';
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'source_platform_tenant_format') THEN
    ALTER TABLE source_platform ADD CONSTRAINT source_platform_tenant_format CHECK (
      tenant_key IS NULL OR (domain IS NOT NULL AND tenant_key = lower(tenant_key)
        AND tenant_key ~ '^((/[^/?#[:space:]]+){{1,3}}|[a-z0-9_]+=[^&#[:space:]]+)$'));
  END IF;
END $$;
DROP INDEX IF EXISTS source_platform_domain_uq;
CREATE UNIQUE INDEX IF NOT EXISTS source_platform_site_uq
  ON source_platform (domain, coalesce(tenant_key, '')) WHERE domain IS NOT NULL;

CREATE TEMP TABLE _trc_seed (
  ord int, code text, name text, short_name text, url text, domain text, tenant_key text, country text,
  data_format text, priority int, code_origin text, notes text
) ON COMMIT DROP;

INSERT INTO _trc_seed VALUES
"""

SEED_SQL_FOOTER = """;

DO $$
DECLARE
  r record; ex record;
  n_new int := 0; n_act int := 0; n_same int := 0; n_other int := 0; n_skip int := 0;
  other_list text := ''; skip_list text := '';
BEGIN
  FOR r IN SELECT * FROM _trc_seed ORDER BY ord LOOP
    SELECT code, status, name INTO ex FROM source_platform
     WHERE domain = r.domain AND tenant_key IS NOT DISTINCT FROM r.tenant_key;
    IF FOUND THEN
      IF ex.code <> r.code THEN
        n_other := n_other + 1;
        other_list := other_list || format(' %s=%s', r.code, ex.code);
        RAISE NOTICE '006: % (%) is already registered as % (%) - kept as it is', r.name, r.domain, ex.code, ex.status;
      ELSIF ex.status = 'proposed' THEN
        UPDATE source_platform SET status = 'active' WHERE code = ex.code;
        n_act := n_act + 1;
      ELSE
        n_same := n_same + 1;
      END IF;
      CONTINUE;
    END IF;
    SELECT code, status, name, domain INTO ex FROM source_platform WHERE code = r.code;
    IF FOUND THEN
      n_skip := n_skip + 1;
      skip_list := skip_list || ' ' || r.code;
      RAISE WARNING '006: code % is already used by % (%, %); % (%) was NOT registered - choose another code and add it with platforms.py apply',
        r.code, ex.name, coalesce(ex.domain, 'no domain'), ex.status, r.name, r.domain;
      CONTINUE;
    END IF;
    INSERT INTO source_platform (code, name, short_name, url, domain, tenant_key, country, data_format,
                                 priority, status, code_origin, notes)
    VALUES (r.code, r.name, r.short_name, r.url, r.domain, r.tenant_key, r.country, r.data_format,
            r.priority, 'active', r.code_origin, r.notes);
    n_new := n_new + 1;
  END LOOP;
  RAISE NOTICE '006: % registered, % activated, % already present, % present under another code, % not registered (code taken)',
    n_new, n_act, n_same, n_other, n_skip;
  IF n_other > 0 THEN
    RAISE WARNING '006: % platform(s) were already registered under a different code and were kept (list code=registered code):%',
      n_other, other_list;
  END IF;
END $$;

COMMIT;
"""


def _sql(v) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def seed_sql(proposals_path: str, src_label: str | None = None) -> tuple[str, dict]:
    """Build 006_seed_platforms.sql from a proposals CSV: every row with approved_code filled.
    Refuses (PlatformError) malformed/reserved/duplicate codes, duplicate (domain, tenant) pairs,
    bad countries, formats or priorities, so the SQL itself never fails half-way."""
    import hashlib
    raw = open(proposals_path, "rb").read()
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    if rows and not {"name", "approved_code"} <= set(rows[0]):
        raise PlatformError(f"{proposals_path} has no approved_code column (run suggest --prefill)")
    vals, codes, sites, errors, skipped = [], {}, {}, [], []
    for r in rows:
        code = (r.get("approved_code") or "").strip().upper()
        label = f"row {r.get('source_row') or '?'} {(r.get('name') or '')[:40]!r}"
        if not code:
            skipped.append(label)
            continue
        url = (r.get("url") or "").strip() or None
        dom = domain_of(url) if url else None
        tenant = (r.get("tenant_key") or "").strip().lower() or None
        country = (r.get("country") or "").strip().upper() or None
        country = COUNTRY_FIXES.get(country, country) if country else None
        fmt = (r.get("data_format") or "").strip().lower() or "other"
        prio = (r.get("priority") or "").strip()
        problems = []
        if not re.fullmatch(r"[A-Z]{3}", code):
            problems.append(f"code {code!r} is not three letters")
        if code in RESERVED:
            problems.append(f"{code} is reserved")
        if code in codes:
            problems.append(f"{code} also used by {codes[code]}")
        if not dom:
            problems.append("no usable domain (006 only registers platforms that resolve by URL)")
        if (dom, tenant) in sites:
            problems.append(f"same domain/tenant as {sites[(dom, tenant)]}")
        if tenant and not re.fullmatch(r"(/[^/?#\s]+){1,3}|[a-z0-9_]+=[^&#\s]+", tenant):
            problems.append(f"bad tenant_key {tenant!r}")
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            problems.append(f"country {country!r} not ISO alpha-2")
        if fmt not in DATA_FORMATS:
            problems.append(f"data_format {fmt!r}")
        if prio and not re.fullmatch(r"\d{1,4}", prio):
            problems.append(f"priority {prio!r}")
        if problems:
            errors.append(f"{label}: " + "; ".join(problems))
            continue
        codes[code] = label
        sites[(dom, tenant)] = label
        origin = "suggested" if code == (r.get("suggested_code") or "").strip().upper() else "manual"
        notes = "; ".join(filter(None, [(r.get("notes") or "").strip(),
                                        f"seeded by 006 from {os.path.basename(proposals_path)} rows {r.get('source_rows') or r.get('source_row')}"]))
        vals.append("  (" + ", ".join([str(len(vals) + 1), _sql(code), _sql(r.get("name", "").strip()),
                                       _sql(r.get("short_name")), _sql(url), _sql(dom), _sql(tenant), _sql(country),
                                       _sql(fmt), str(int(prio) if prio else DEFAULT_PRIORITY), _sql(origin),
                                       _sql(notes)]) + ")")
    if errors:
        raise PlatformError("cannot generate the seed migration:\n  " + "\n  ".join(errors))
    if not vals:
        raise PlatformError("no rows with approved_code")
    src = src_label or os.path.basename(proposals_path)
    head = SEED_SQL_HEADER.format(src=src, n=len(vals), sha=hashlib.sha256(raw).hexdigest()[:16])
    return head + ",\n".join(vals) + SEED_SQL_FOOTER, {"rows": len(vals), "skipped": skipped}


def seed_schema_sql() -> str:
    """Only Part 1 of 006 (tenant_key column, check, (domain, tenant_key) uniqueness), for the
    `platforms.py apply` path on a 005 database. Idempotent; 006 later finds it in place."""
    head = SEED_SQL_HEADER
    part1 = head[head.index("BEGIN;"):head.index("CREATE TEMP TABLE")].replace("{{", "{").replace("}}", "}")
    return ("-- Part 1 of 006_seed_platforms.sql only (schema for shared-host tenants), generated by\n"
            "-- `python platforms.py seed-sql --schema-only`. Safe to re-run.\n\n" + part1.rstrip() + "\n\nCOMMIT;\n")


# ---------- CLI ----------

def _connect(required: bool):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        if required:
            raise SystemExit("DATABASE_URL is not set")
        return None
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Tender platform registry", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suggest", help="suggest codes for a CSV/XLSX list of platforms")
    s.add_argument("input")
    s.add_argument("--out", help="proposals CSV (default: INPUT_proposals.csv)")
    s.add_argument("--no-db", action="store_true", help="ignore DATABASE_URL")
    s.add_argument("--prefill", action="store_true", help="write the chosen code into approved_code")
    cl = sub.add_parser("clean", help="deduplicate a raw platform list (see platform_clean.py)")
    cl.add_argument("input")
    cl.add_argument("--out-dir", default="platforms")
    sq = sub.add_parser("seed-sql", help="generate 006_seed_platforms.sql from approved proposals")
    sq.add_argument("proposals", nargs="?")
    sq.add_argument("--out", default="006_seed_platforms.sql")
    sq.add_argument("--schema-only", action="store_true",
                    help="only the tenant_key schema part (for the `apply` path on a 005 database)")
    a = sub.add_parser("apply", help="register approved rows of a proposals CSV")
    a.add_argument("proposals")
    a.add_argument("--activate", action="store_true", help="register as active instead of proposed")
    li = sub.add_parser("list")
    li.add_argument("--all", action="store_true", help="include retired platforms")
    for c in ("activate", "retire"):
        sub.add_parser(c).add_argument("code")
    rn = sub.add_parser("rename")
    rn.add_argument("code")
    rn.add_argument("--name"); rn.add_argument("--url"); rn.add_argument("--short-name")
    up = sub.add_parser("update")
    up.add_argument("code")
    up.add_argument("--priority", type=int); up.add_argument("--country"); up.add_argument("--data-format")
    up.add_argument("--notes"); up.add_argument("--tenant-key", help="'/path' or 'param=value'; '' to clear")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "clean":
            import platform_clean
            return platform_clean.main([args.input, "--out-dir", args.out_dir])
        if args.cmd == "seed-sql" and args.schema_only:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(seed_schema_sql())
            print(f"wrote {args.out}: schema part of 006 only")
            return 0
        if args.cmd == "seed-sql":
            if not args.proposals:
                raise PlatformError("seed-sql needs a proposals CSV (or --schema-only)")
            sql, info = seed_sql(args.proposals)
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(sql)
            print(f"wrote {args.out}: {info['rows']} platforms"
                  + (f"; {len(info['skipped'])} rows without approved_code left out" if info["skipped"] else ""))
            return 0
        if args.cmd == "suggest":
            rows, msgs = read_rows(args.input)
            conn = None if args.no_db else _connect(False)
            if conn:
                registered = registered_platforms(conn)
                conn.close()
            else:
                registered = [{"code": c, "name": n, "domain": d, "status": "active", "tenant_key": None} for c, n, d in (
                    ("PLC", "Plataforma de Contratación del Sector Público", "contrataciondelestado.es"),
                    ("FTS", "Find a Tender", "find-tender.service.gov.uk"),
                    ("TED", "Tenders Electronic Daily", "ted.europa.eu"), ("MAN", "Manual entry", None))]
                msgs.append("no database: compared against the built-in seed codes only (PLC, FTS, TED, MAN)")
            props = build_proposals(rows, registered, prefill=args.prefill)
            out = args.out or re.sub(r"\.(csv|xlsx|xlsm)$", "", args.input, flags=re.I) + "_proposals.csv"
            write_proposals(out, props)
            for m in msgs:
                print(m)
            print(summary(props))
            print(f"wrote {out}")
            return 0
        conn = _connect(True)
        if args.cmd == "apply":
            res = apply_proposals(conn, args.proposals, activate=args.activate)
            for k in ("inserted", "activated", "already", "skipped", "refused"):
                if res[k]:
                    print(f"{k}: " + ("\n  ".join([""] + res[k]) if k in ("skipped", "refused") else ", ".join(res[k])))
            return 1 if res["refused"] else 0
        if args.cmd == "list":
            rows = registered_platforms(conn)
            print(f"{'code':<5}{'status':<10}{'prio':>5}  {'frozen':<11}{'domain (tenant)':<44}name")
            for r in rows:
                if r["status"] == "retired" and not args.all:
                    continue
                site = (r["domain"] or "-") + (f" {r['tenant_key']}" if r.get("tenant_key") else "")
                print(f"{r['code']:<5}{r['status']:<10}{r['priority']:>5}  "
                      f"{(r['frozen_at'].strftime('%Y-%m-%d') if r['frozen_at'] else '-'):<11}{site[:43]:<44}{r['name']}")
            return 0
        if args.cmd in ("activate", "retire"):
            print(set_status(conn, args.code, "active" if args.cmd == "activate" else "retired"))
            return 0
        if args.cmd == "rename":
            print(update_platform(conn, args.code, name=args.name, url=args.url, short_name=args.short_name))
            return 0
        if args.cmd == "update":
            print(update_platform(conn, args.code, priority=args.priority, country=args.country,
                                  data_format=args.data_format, notes=args.notes, tenant_key=args.tenant_key))
            return 0
    except PlatformError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:                       # database rule broken (frozen code, retired, ...)
        import psycopg
        if isinstance(e, psycopg.Error):
            print(f"error: {e.diag.message_primary or e}", file=sys.stderr)
            return 2
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
