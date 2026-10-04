"""Ingestion service for Tender & RFP Cockpit.

One entry point for scrapers:

    from ingest import ingest_tender
    r = ingest_tender(conn, {"source": "PLACSP", "source_ref": "EXP-900", "title": "...",
                             "buyer": "...", "deadline": "2026-11-01T12:00:00Z"}, actor="placsp-scraper")
    r.tender_id, r.accession_no, r.outcome   # outcome: created | matched_source_ref | matched_hash

Python owns validation, cleaning and the dedupe hash (dedupe.py). The database
only does the atomic part: lock, look up, insert (the trigger numbers new rows),
and write events. Every (platform, reference) a tender is known by is kept in
tender_source_ref, so a TED copy matched once by hash is matched by its TED
reference from then on, even if TED later changes the title. Child helpers (add_lot / add_amendment / add_submission /
set_status) number children via issue_child_no() and log an event in the same
transaction. A deadline_change amendment also moves tender.deadline_at.

Re-scrapes (v4): when a scrape matches an existing tender, the tracked fields
(title, buyer, deadline, platform status, description, estimated value, currency,
CPV codes) are compared with what is stored, inside the same transaction and
under the same locks. Differences are applied or only recorded according to
source precedence and manual locks (see changes.py), and one fields_changed
event lists exactly what changed. An unchanged re-scrape writes no event.
Manual edits: edit_field() (locks the field by default), lock_field(), unlock_field().

Platforms (v5): `source` may be a registered platform code ("PLC") or name/short name
("PLACSP", "Find a Tender"); or send `source_url` (any URL on the platform) or `platform`
(its name). platforms.resolve_platform() finds the registered platform; unknown or non-active
platforms are rejected (IngestError). Codes are never invented here.

Lots (v5): ingest_lots(conn, tender_id, lots, actor) takes the dicts produced by lots.py,
canonicalises the lot labels ("Lote 1" / "Lot 1" / "LOT-0001" -> "1") and is idempotent.
AI-extracted lots wait for confirm_lot(); a structured copy of the same lot confirms it.

CLI (DATABASE_URL must be set):
    python ingest.py load FILE.jsonl --actor NAME   ingest one JSON object per line
    python ingest.py backfill-norms                 fill title_norm/buyer_norm on old rows
    python ingest.py backfill-observed              after upgrading to 004 (see README)
    python ingest.py lots ACCESSION FILE [--format codice|eforms|ocds|text] --actor NAME
                                                    extract lots from a notice/document and add them
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import changes
import dedupe
import lots as lotlib
import platforms

OUTCOMES = ("created", "matched_source_ref", "matched_hash")
AMENDMENT_KINDS = ("amendment", "clarification", "deadline_change", "cancellation", "corrigendum")
MAX_TITLE = 2000

FIND_OR_CREATE_SQL = (
    "SELECT tender_id, accession_no, outcome FROM find_or_create_tender("
    "%(source)s, %(source_ref)s, %(hash)s, %(title)s, %(buyer)s, %(deadline)s, "
    "%(title_norm)s, %(buyer_norm)s, %(actor)s, %(captured_at)s, %(payload)s)"
)


class IngestError(ValueError):
    """The scraped record is unusable. Message says which field and why."""


RETRIES = 3


def _retrying(fn):
    """Re-run a write if Postgres aborted it as a deadlock / serialisation victim. Only when this
    call owns the transaction (connection idle before the call); inside a caller's transaction
    the error is raised so the caller can retry its whole unit of work."""
    import functools

    import psycopg

    @functools.wraps(fn)
    def wrapper(conn, *args, **kwargs):
        owns = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        for attempt in range(RETRIES + 1):
            try:
                return fn(conn, *args, **kwargs)
            except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                if not owns or attempt == RETRIES:
                    raise
    return wrapper


@dataclass(frozen=True)
class IngestResult:
    tender_id: Any           # uuid.UUID
    accession_no: str
    outcome: str             # created | matched_source_ref | matched_hash
    changed: tuple = ()      # fields this scrape changed on the tender
    not_applied: tuple = ()  # fields that differ but were not written (precedence / lock / stale)

    @property
    def created(self) -> bool:
        return self.outcome == "created"


@dataclass(frozen=True)
class ChildResult:
    id: Any
    tender_id: Any
    child_no: int
    accession_no: str
    outcome: str             # created | existing


@dataclass(frozen=True)
class CleanTender:
    source: str
    source_ref: str | None
    title: str
    buyer: str | None
    deadline: datetime | None
    captured_at: datetime | None
    title_norm: str
    buyer_norm: str | None
    dedupe_hash: str
    payload: dict
    fields: dict             # tracked fields this scrape provided: {field: cleaned value}


# ---------- validation / normalisation (pure Python, no DB) ----------

def _tidy(text: Any) -> str | None:
    """Display clean-up only: collapse whitespace. Matching uses dedupe.clean_*."""
    if text is None:
        return None
    s = " ".join(str(text).split())
    return s or None


def _source_code(raw: Any) -> str:
    """Offline check only (no database): a three-letter code. ingest_tender resolves names/URLs."""
    if not raw or not str(raw).strip():
        raise IngestError("source is required")
    code = str(raw).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise IngestError(f"source {raw!r} is not a three-letter platform code")
    return code


def resolve_source(conn, scraped: dict) -> str:
    """The registered, ACTIVE platform code for a scraped record. Accepts `source` (code, or the
    platform's name / short name), `platform` (name or code) and/or `source_url` (any URL on the
    platform; subdomains, www and ports are handled). If several are given they must agree."""
    if not isinstance(scraped, dict):
        raise IngestError("scraped record must be a dict")
    found: dict[str, str] = {}
    try:
        for key in ("source", "platform"):
            v = scraped.get(key)
            if v is None or not str(v).strip():
                continue
            v = str(v).strip()
            if re.fullmatch(r"[A-Za-z]{3}", v):
                try:
                    found[key] = platforms.resolve_platform(conn, code=v).code
                    continue
                except platforms.PlatformError as e:
                    if "unknown platform code" not in str(e):
                        raise                       # a known code that is not active
            found[key] = platforms.resolve_platform(conn, name=v).code
        if scraped.get("source_url"):
            try:
                found["source_url"] = platforms.resolve_platform(conn, url=scraped["source_url"]).code
            except platforms.PlatformError:
                if not found:
                    raise
                # the platform is known from source/platform; a URL on another site (mirror,
                # document host) does not change it
    except platforms.PlatformError as e:
        raise IngestError(str(e)) from e
    if not found:
        raise IngestError("source is required (a platform code or name, or source_url)")
    codes = set(found.values())
    if len(codes) > 1:
        raise IngestError("source fields disagree: " + ", ".join(f"{k} -> {c}" for k, c in found.items()))
    return codes.pop()


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as e:
            raise IngestError(f"{field} {value!r} is not an ISO date/time") from e
    else:
        raise IngestError(f"{field} must be a datetime, date or ISO string, got {type(value).__name__}")
    # Naive times are treated as UTC, the same rule dedupe.clean_deadline uses
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalise(scraped: dict, source: str | None = None) -> CleanTender:
    """Validate a scraped record and compute everything the database stores. `source` is the
    resolved platform code (ingest_tender passes it); without it `scraped["source"]` must be a
    three-letter code (offline use)."""
    if not isinstance(scraped, dict):
        raise IngestError("scraped record must be a dict")
    source = _source_code(source if source is not None else scraped.get("source"))
    title = _tidy(scraped.get("title"))
    if not title:
        raise IngestError("title is required")
    if len(title) > MAX_TITLE:
        raise IngestError(f"title is longer than {MAX_TITLE} characters")
    title_norm = dedupe.clean_title(title)
    if not title_norm:
        raise IngestError(f"title {title!r} has no matchable words")
    buyer = _tidy(scraped.get("buyer"))
    deadline = _timestamp(scraped.get("deadline"), "deadline")
    captured_at = _timestamp(scraped.get("captured_at"), "captured_at")
    source_ref = dedupe.clean_source_ref(scraped.get("source_ref"))
    payload = dict(scraped.get("payload") or {})
    if scraped.get("url"):
        payload["url"] = scraped["url"]
    if scraped.get("source_url") and scraped.get("source_url") != scraped.get("url"):
        payload["source_url"] = scraped["source_url"]
    try:
        extra = {
            "status": changes.clean_status(scraped.get("status")),
            "description": changes.clean_description(scraped.get("description")),
            "estimated_value": changes.clean_value(scraped.get("estimated_value")),
            "currency": changes.clean_currency(scraped.get("currency")),
            "cpv_codes": changes.clean_cpv(scraped.get("cpv_codes")),
        }
    except changes.FieldError as e:
        raise IngestError(str(e)) from e
    # A field the scrape did not provide (missing or null) is "unknown", never "cleared"
    fields = {"title": title, "buyer": buyer, "deadline_at": deadline, **extra}
    fields = {k: v for k, v in fields.items() if v is not None}
    return CleanTender(
        source=source, source_ref=source_ref, title=title, buyer=buyer, deadline=deadline,
        captured_at=captured_at, title_norm=title_norm,
        buyer_norm=dedupe.clean_buyer(buyer) or None,
        dedupe_hash=dedupe.dedupe_hash(title, buyer, deadline), payload=payload, fields=fields,
    )


# ---------- the entry point ----------

@_retrying
def ingest_tender(conn, scraped: dict, actor: str) -> IngestResult:
    """Find or create the tender for one scraped record. Commits unless the caller
    already has a transaction open (then it runs as a savepoint inside it)."""
    if not actor or not str(actor).strip():
        raise IngestError("actor is required (e.g. the scraper name)")
    t = normalise(scraped, resolve_source(conn, scraped))
    params = {
        "source": t.source, "source_ref": t.source_ref, "hash": t.dedupe_hash,
        "title": t.title, "buyer": t.buyer, "deadline": t.deadline,
        "title_norm": t.title_norm, "buyer_norm": t.buyer_norm,
        "actor": actor.strip(), "captured_at": t.captured_at, "payload": Jsonb(t.payload),
    }
    with conn.transaction(), conn.cursor() as cur:
        # Takes the reference + hash advisory locks; they stay held until this transaction ends,
        # so the change tracking below is serialised with every other scrape of this tender.
        cur.execute(FIND_OR_CREATE_SQL, params)
        tender_id, accession_no, outcome = cur.fetchone()
        assert outcome in OUTCOMES, outcome
        if outcome == "created":
            _fill_new(cur, tender_id, t)
            return IngestResult(tender_id, accession_no, outcome)
        tender_id, accession_no, res = _track_rescrape(cur, tender_id, t, outcome, actor.strip())
    return IngestResult(tender_id, accession_no, outcome, tuple(res.changes), tuple(res.not_applied))


def _observed(t: CleanTender, reported: dict | None = None) -> dict:
    """What this platform reference showed, plus the not-applied differences already reported
    ("_reported": {field: [ours, theirs]}), so the same standing difference is logged once."""
    snap = {f: changes.jsonable(v) for f, v in t.fields.items()}
    if reported:
        snap["_reported"] = reported
    return snap


def _fill_new(cur, tender_id, t: CleanTender) -> None:
    """Optional tracked fields on a brand-new tender (part of 'created', no extra event)."""
    extra = {f: v for f, v in t.fields.items() if f not in ("title", "buyer", "deadline_at")}
    if extra:
        cur.execute(f"UPDATE tender SET {', '.join(f + ' = %s' for f in extra)} WHERE id = %s",
                    (*extra.values(), tender_id))
    if t.source_ref:
        cur.execute("UPDATE tender_source_ref SET observed = %s WHERE source = %s AND source_ref = %s",
                    (Jsonb(_observed(t)), t.source, t.source_ref))


def _track_rescrape(cur, tender_id, t: CleanTender, outcome: str, actor: str):
    row = changes.lock_row(cur, tender_id)     # already held from find_or_create; follows a merge
    ref_origin, prev = None, None
    if t.source_ref:
        cur.execute("SELECT origin, observed FROM tender_source_ref WHERE source = %s AND source_ref = %s",
                    (t.source, t.source_ref))
        ref_origin, prev = cur.fetchone() or (None, None)
    else:                           # no reference: what this platform showed last time we logged it
        cur.execute("""SELECT payload->'observed' FROM tender_event
                       WHERE tender_id = %s AND event_type = 'fields_changed' AND source = %s
                         AND payload->>'source_ref' IS NULL ORDER BY id DESC LIMIT 1""", (row["id"], t.source))
        prev = (cur.fetchone() or (None,))[0]
    prev = prev or {}
    prev_reported = prev.get("_reported") or {}
    # Matched through a hash the tender no longer has (old title/deadline, or a merged duplicate's)
    stale = outcome == "matched_hash" and row["dedupe_hash"] != t.dedupe_hash
    prios = changes.load_priorities(cur, (row["source"], t.source))
    allow, allowed, reason = changes.field_rule(row["source"], t.source, ref_origin=ref_origin, stale=stale,
                                                previously_observed=prev, priorities=prios)
    res = changes.apply_changes(cur, row, t.fields, allow=allow, held_hashes={t.dedupe_hash})
    # A standing difference (same ours/theirs pair) reported before is not logged again
    standing = {f: [v["old"], v["new"]] for f, v in res.not_applied.items()}
    res.not_applied = {f: v for f, v in res.not_applied.items() if prev_reported.get(f) != standing[f]}
    if res.changes or res.not_applied or res.hash_updated:
        payload = {"match": outcome, "source_ref": t.source_ref, "precedence": {
                       "primary_source": row["source"], "allowed": allowed, "reason": reason,
                       "priorities": prios},
                   "changes": res.changes, "not_applied": res.not_applied}
        if res.hash:
            payload["hash"] = res.hash
        if not t.source_ref:
            payload["observed"] = _observed(t, standing)
        _event(cur, row["id"], "fields_changed", actor, payload, t.source)
    if t.source_ref:
        cur.execute("UPDATE tender_source_ref SET observed = %s WHERE source = %s AND source_ref = %s",
                    (Jsonb(_observed(t, standing)), t.source, t.source_ref))
    return row["id"], row["accession_no"], res


# ---------- children and status ----------

def _event(cur, tender_id, event_type: str, actor: str, payload: dict, source: str | None = None):
    cur.execute(
        "INSERT INTO tender_event (tender_id, event_type, actor, source, payload) VALUES (%s, %s, %s, %s, %s)",
        (tender_id, event_type, actor, source, Jsonb(payload)),
    )


def _lock_live_tender(cur, tender_id):
    """Follow a merge to the survivor and lock it, so children always hang off a live tender."""
    cur.execute("SELECT 1 FROM tender WHERE id = %s", (tender_id,))
    if not cur.fetchone():
        raise IngestError(f"tender {tender_id} not found")
    # FOR UPDATE before inserting children/events that reference it (see trc_lock_tender in 004)
    cur.execute("SELECT trc_lock_tender(%s)", (tender_id,))
    tid = cur.fetchone()[0]
    cur.execute("SELECT id, accession_no FROM tender WHERE id = %s", (tid,))
    return cur.fetchone()


def _next_child(cur, tender_id, kind: str) -> tuple[int, str]:
    cur.execute("SELECT child_no, child_accession FROM issue_child_no(%s, %s)", (tender_id, kind))
    return cur.fetchone()


def _require_actor(actor: str) -> str:
    if not actor or not str(actor).strip():
        raise IngestError("actor is required")
    return actor.strip()


LOT_ORIGINS = ("structured", "ai_extracted", "manual")
_LOT_COLS = "id, lot_no, accession_no, lot_origin, title, description, cpv_codes, estimated_value, currency, confirmed_at"


def _clean_lot_values(description, cpv_codes, estimated_value, currency):
    try:
        return (changes.clean_description(description), changes.clean_cpv(cpv_codes),
                changes.clean_value(estimated_value), changes.clean_currency(currency))
    except changes.FieldError as e:
        raise IngestError(f"lot: {e}") from e


@_retrying
def add_lot(conn, tender_id, *, title: str, actor: str, source_lot_ref: str | None = None,
            description: str | None = None, cpv_codes: list[str] | None = None,
            estimated_value=None, currency: str | None = None, source: str | None = None,
            source_lot_label: str | None = None, origin: str | None = None,
            extractor: str | None = None) -> ChildResult:
    """Add one lot (or return the existing one). The platform's lot label is canonicalised
    (lots.canonical_lot_ref: "Lote 1" / "Lot 1" / "LOT-0001" / "1" -> "1") and stored in
    source_lot_ref; the raw label goes to source_lot_label. origin defaults to 'structured' when
    a platform (source) or lot ref is given, else 'manual'. A structured lot arriving for an
    existing AI-extracted lot confirms it (outcome 'confirmed', event lot_confirmed)."""
    actor = _require_actor(actor)
    title = _tidy(title)
    if not title:
        raise IngestError("lot title is required")
    label = _tidy(source_lot_label) or _tidy(source_lot_ref)
    ref = lotlib.canonical_lot_ref(source_lot_ref if source_lot_ref is not None else label)
    origin = origin or ("structured" if (source or ref) else "manual")
    if origin not in LOT_ORIGINS:
        raise IngestError(f"lot origin must be one of {LOT_ORIGINS}")
    description, cpv_codes, estimated_value, currency = _clean_lot_values(description, cpv_codes,
                                                                          estimated_value, currency)
    with conn.transaction(), conn.cursor() as cur:
        tid, _ = _lock_live_tender(cur, tender_id)
        row = None
        if ref:   # re-scrape of a lot we already have (old rows may still hold a raw label)
            cur.execute(f"SELECT {_LOT_COLS} FROM tender_lot WHERE tender_id = %s AND "
                        "(source_lot_ref = %s OR trc_canonical_lot_ref(source_lot_ref) = %s) ORDER BY lot_no LIMIT 1",
                        (tid, ref, ref))
            row = cur.fetchone()
        elif origin != "manual":   # a parsed lot without a number: same title on the same tender
            cur.execute(f"SELECT {_LOT_COLS} FROM tender_lot WHERE tender_id = %s AND source_lot_ref IS NULL", (tid,))
            row = next((r for r in cur.fetchall() if dedupe.clean_title(r[4]) == dedupe.clean_title(title)), None)
        if row:
            if origin == "structured" and row[3] == "ai_extracted":
                cur.execute("""UPDATE tender_lot SET lot_origin = 'structured',
                                 description = coalesce(description, %s), cpv_codes = coalesce(cpv_codes, %s),
                                 estimated_value = coalesce(estimated_value, %s), currency = coalesce(currency, %s),
                                 source_lot_label = coalesce(source_lot_label, %s),
                                 confirmed_by = coalesce(confirmed_by, %s), confirmed_at = coalesce(confirmed_at, now())
                               WHERE id = %s""",
                            (description, cpv_codes, estimated_value, currency, label, actor, row[0]))
                _event(cur, tid, "lot_confirmed", actor, {
                    "lot_id": str(row[0]), "accession_no": row[2], "via": "structured_source",
                    "source_lot_ref": ref, "was_confirmed": row[9] is not None}, source)
                return ChildResult(row[0], tid, row[1], row[2], "confirmed")
            return ChildResult(row[0], tid, row[1], row[2], "existing")
        no, acc = _next_child(cur, tid, "L")
        cur.execute(
            """INSERT INTO tender_lot (tender_id, lot_no, accession_no, source_lot_ref, source_lot_label, title,
                                       description, cpv_codes, estimated_value, currency, lot_origin)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (tid, no, acc, ref, label, title, description, cpv_codes, estimated_value, currency, origin))
        lot_id = cur.fetchone()[0]
        payload = {"lot_id": str(lot_id), "accession_no": acc, "lot_no": no, "source_lot_ref": ref,
                   "source_lot_label": label, "title": title, "lot_origin": origin}
        if extractor:
            payload["extractor"] = extractor
        _event(cur, tid, "lot_added", actor, payload, source)
    return ChildResult(lot_id, tid, no, acc, "created")


@_retrying
def ingest_lots(conn, tender_id, lots: list[dict], actor: str, *, source: str | None = None,
                extractor: str | None = None) -> list[ChildResult]:
    """Add the lots produced by lots.py (or any list of dicts with title / source_lot_ref or
    source_lot_label / description / cpv_codes / estimated_value / currency / origin) in one
    transaction. Idempotent: lots already present (same canonical ref, or same title for lots
    without a ref) are returned as 'existing' and use no new number."""
    if lots is None:
        return []
    if not isinstance(lots, (list, tuple)):
        raise IngestError("lots must be a list of dicts")
    out, seen = [], set()
    with conn.transaction():
        for i, lot in enumerate(lots):
            if not isinstance(lot, dict):
                raise IngestError(f"lot {i}: not a dict")
            label = lot.get("source_lot_label") or lot.get("source_lot_ref")
            ref = lotlib.canonical_lot_ref(lot.get("source_lot_ref") or label)
            if ref:
                if ref in seen:
                    continue              # the same lot twice in one notice
                seen.add(ref)
            out.append(add_lot(conn, tender_id, title=lot.get("title") or (f"Lot {label}" if label else ""),
                               actor=actor, source_lot_ref=ref, source_lot_label=label,
                               description=lot.get("description"), cpv_codes=lot.get("cpv_codes"),
                               estimated_value=lot.get("estimated_value"), currency=lot.get("currency"),
                               source=source, origin=lot.get("origin") or "structured", extractor=extractor))
    return out


@_retrying
def confirm_lot(conn, lot_accession: str, actor: str) -> bool:
    """A person confirms an AI-extracted lot (any old or new lot ID). Returns False if it was
    already confirmed; raises for a lot that is not AI-extracted."""
    actor = _require_actor(actor)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT kind, child_id, tender_id FROM resolve_child_accession(%s)", (lot_accession,))
        hit = cur.fetchone()
        if not hit or hit[0] != "L":
            raise IngestError(f"lot {lot_accession!r} not found")
        tid, _ = _lock_live_tender(cur, hit[2])
        cur.execute("SELECT accession_no, lot_origin, confirmed_at FROM tender_lot WHERE id = %s FOR UPDATE", (hit[1],))
        acc, origin, confirmed = cur.fetchone()
        if origin != "ai_extracted":
            raise IngestError(f"lot {acc} is {origin}, only ai_extracted lots need confirming")
        if confirmed:
            return False
        cur.execute("UPDATE tender_lot SET confirmed_by = %s, confirmed_at = now() WHERE id = %s", (actor, hit[1]))
        _event(cur, tid, "lot_confirmed", actor, {"lot_id": str(hit[1]), "accession_no": acc, "via": "manual"})
    return True


@_retrying
def add_amendment(conn, tender_id, *, kind: str, actor: str, lot_id=None, source_ref: str | None = None,
                  summary: str | None = None, published_at=None, new_deadline_at=None,
                  document_uri: str | None = None, document_sha256: str | None = None,
                  source: str | None = None) -> ChildResult:
    """Record an amendment. A deadline_change also moves tender.deadline_at (even when the
    amendment is lot-scoped: lots have no deadline of their own), recomputes the dedupe hash
    so later scrapes showing the new deadline still match, and logs deadline_changed."""
    actor = _require_actor(actor)
    if kind not in AMENDMENT_KINDS:
        raise IngestError(f"amendment kind must be one of {AMENDMENT_KINDS}")
    published_at = _timestamp(published_at, "published_at")
    new_deadline_at = _timestamp(new_deadline_at, "new_deadline_at")
    if kind == "deadline_change" and new_deadline_at is None:
        raise IngestError("a deadline_change amendment needs new_deadline_at")
    with conn.transaction(), conn.cursor() as cur:
        new_hash = None
        if kind == "deadline_change":
            # Take the new hash's advisory lock BEFORE the row lock (same order as
            # find_or_create_tender), so a concurrent scrape cannot create a tender with it.
            cur.execute("SELECT t.title, t.buyer FROM tender t "
                        "JOIN tender s ON s.id = %s AND t.id = coalesce(s.merged_into, s.id)", (tender_id,))
            row = cur.fetchone()
            if not row:
                raise IngestError(f"tender {tender_id} not found")
            new_hash = dedupe.dedupe_hash(row[0], row[1], new_deadline_at)
            cur.execute("SELECT pg_advisory_xact_lock(trc_lock_key('hash', %s))", (new_hash,))
        tid, _ = _lock_live_tender(cur, tender_id)
        if document_sha256:  # same document scraped again
            cur.execute("SELECT id, amendment_no, accession_no FROM tender_amendment "
                        "WHERE tender_id = %s AND document_sha256 = %s", (tid, document_sha256))
            if (row := cur.fetchone()):
                return ChildResult(row[0], tid, row[1], row[2], "existing")
        no, acc = _next_child(cur, tid, "A")
        cur.execute(
            """INSERT INTO tender_amendment (tender_id, lot_id, amendment_no, accession_no, kind, source_ref,
                                             summary, published_at, new_deadline_at, document_uri, document_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (tid, lot_id, no, acc, kind, source_ref, summary, published_at, new_deadline_at,
             document_uri, document_sha256))
        am_id = cur.fetchone()[0]
        _event(cur, tid, "amended", actor, {
            "amendment_id": str(am_id), "accession_no": acc, "amendment_no": no, "kind": kind,
            "lot_id": str(lot_id) if lot_id else None,
            "new_deadline_at": new_deadline_at.isoformat() if new_deadline_at else None}, source)
        if kind == "deadline_change":
            _apply_deadline_change(cur, tid, new_deadline_at, new_hash, acc, lot_id, actor, source)
    return ChildResult(am_id, tid, no, acc, "created")


def _apply_deadline_change(cur, tid, new_deadline: datetime, new_hash: str, amendment_acc: str,
                           lot_id, actor: str, source: str | None) -> None:
    """Same rules as a re-scrape (changes.apply_changes): an official amendment may always move
    the deadline, except when a person locked deadline_at. The replaced hash goes to history."""
    row = changes.lock_row(cur, tid)
    old_deadline, old_hash = row["deadline_at"], row["dedupe_hash"]
    res = changes.apply_changes(cur, row, {"deadline_at": new_deadline}, held_hashes={new_hash})
    if res.changes:
        h = res.hash or {"old": old_hash, "new": old_hash, "updated": False, "conflict_with": None}
        _event(cur, tid, "deadline_changed", actor, {
            "from": old_deadline.isoformat() if old_deadline else None, "to": new_deadline.isoformat(),
            "amendment": amendment_acc, "lot_id": str(lot_id) if lot_id else None,
            "old_hash": old_hash, "new_hash": h["new"] if h["updated"] else old_hash,
            "hash_updated": h["updated"], "hash_conflict_with": h.get("conflict_with")}, source)
    elif res.not_applied:
        _event(cur, tid, "fields_changed", actor, {
            "match": "amendment", "amendment": amendment_acc, "changes": {},
            "not_applied": res.not_applied}, source)


# ---------- manual edits and locks ----------

def _live_id(cur, tender_id):
    cur.execute("SELECT coalesce(merged_into, id) FROM tender WHERE id = %s", (tender_id,))
    row = cur.fetchone()
    if not row:
        raise IngestError(f"tender {tender_id} not found")
    return row[0]


def _check_field(field: str) -> str:
    if field not in changes.TRACKED_FIELDS:
        raise IngestError(f"unknown field {field!r}; expected one of {changes.TRACKED_FIELDS}")
    return field


def _clean_manual(field: str, value: Any):
    try:
        if field in ("title", "buyer"):
            v = _tidy(value)
            if field == "title" and (not v or not dedupe.clean_title(v)):
                raise IngestError("title cannot be empty")
            return v
        if field == "deadline_at":
            return _timestamp(value, "deadline_at")
        if field == "status":
            v = _tidy(value)
            if not v or v == "merged":
                raise IngestError("status cannot be empty or 'merged' (use merge_tender)")
            return v.lower()
        return {"description": changes.clean_description, "estimated_value": changes.clean_value,
                "currency": changes.clean_currency, "cpv_codes": changes.clean_cpv}[field](value)
    except changes.FieldError as e:
        raise IngestError(str(e)) from e


def _set_lock(cur, tid, field: str, on: bool, actor: str, reason: str | None) -> bool:
    op = "array_append(locked_fields, %s)" if on else "array_remove(locked_fields, %s)"
    cond = "NOT (%s = ANY(locked_fields))" if on else "%s = ANY(locked_fields)"
    cur.execute(f"UPDATE tender SET locked_fields = {op} WHERE id = %s AND {cond}", (field, tid, field))
    done = bool(cur.rowcount)
    if done:
        _event(cur, tid, "field_locked" if on else "field_unlocked", actor, {"field": field, "reason": reason})
    if done and not on:
        # Forget what the platforms last showed for this field, so the primary source's next
        # scrape counts as a first observation and brings its value back
        cur.execute("UPDATE tender_source_ref SET observed = observed - %s WHERE tender_id = %s AND observed ? %s",
                    (field, tid, field))
    return done


@_retrying
def lock_field(conn, tender_id, field: str, actor: str, reason: str | None = None) -> bool:
    """Stop scrapers overwriting one field. Returns False if it was already locked."""
    actor, field = _require_actor(actor), _check_field(field)
    with conn.transaction(), conn.cursor() as cur:
        tid = _live_id(cur, tender_id)
        changes.lock_row(cur, tid)
        return _set_lock(cur, tid, field, True, actor, reason)


@_retrying
def unlock_field(conn, tender_id, field: str, actor: str, reason: str | None = None) -> bool:
    actor, field = _require_actor(actor), _check_field(field)
    with conn.transaction(), conn.cursor() as cur:
        tid = _live_id(cur, tender_id)
        changes.lock_row(cur, tid)
        return _set_lock(cur, tid, field, False, actor, reason)


@_retrying
def edit_field(conn, tender_id, field: str, value: Any, actor: str, *, lock: bool = True) -> changes.ChangeResult:
    """A person sets a field. Overrides any lock, keeps norms + hash in sync, logs fields_changed
    (manual), and by default locks the field so later scrapes only record differences."""
    actor, field = _require_actor(actor), _check_field(field)
    value = _clean_manual(field, value)
    if value is None and field in ("title", "deadline_at"):
        raise IngestError(f"{field} cannot be cleared")
    with conn.transaction(), conn.cursor() as cur:
        tid = _live_id(cur, tender_id)
        held = set()
        if field in changes.HASH_FIELDS:   # advisory lock on the new hash before the row lock
            cur.execute("SELECT title, buyer, deadline_at FROM tender WHERE id = %s", (tid,))
            cur_vals = dict(zip(("title", "buyer", "deadline_at"), cur.fetchone()))
            cur_vals[field] = value
            h = dedupe.dedupe_hash(cur_vals["title"], cur_vals["buyer"], cur_vals["deadline_at"])
            cur.execute("SELECT pg_advisory_xact_lock(trc_lock_key('hash', %s))", (h,))
            held.add(h)
        row = changes.lock_row(cur, tid)
        if value is None:               # clearing an optional field
            res = changes.ChangeResult()
            if row[field] is not None:
                cur.execute(f"UPDATE tender SET {field} = NULL, updated_at = now() WHERE id = %s", (tid,))
                res.changes[field] = {"old": changes.jsonable(row[field]), "new": None}
        else:
            res = changes.apply_changes(cur, row, {field: value}, respect_locks=False, held_hashes=held)
        if res.changes or res.hash_updated:
            payload = {"match": "manual", "changes": res.changes, "not_applied": {}}
            if res.hash:
                payload["hash"] = res.hash
            _event(cur, tid, "fields_changed", actor, payload)
        if lock:
            _set_lock(cur, tid, field, True, actor, "manual edit")
    return res


@_retrying
def add_submission(conn, tender_id, *, actor: str, lot_id=None, opco: str | None = None,
                   owner: str | None = None, status: str = "draft", supersedes_id=None) -> ChildResult:
    actor = _require_actor(actor)
    with conn.transaction(), conn.cursor() as cur:
        tid, _ = _lock_live_tender(cur, tender_id)
        no, acc = _next_child(cur, tid, "S")
        cur.execute(
            """INSERT INTO tender_submission (tender_id, lot_id, submission_no, accession_no, opco, owner,
                                              status, supersedes_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (tid, lot_id, no, acc, opco, owner, status, supersedes_id))
        sub_id = cur.fetchone()[0]
        _event(cur, tid, "submission_added", actor, {
            "submission_id": str(sub_id), "accession_no": acc, "submission_no": no,
            "lot_id": str(lot_id) if lot_id else None, "opco": opco, "status": status,
            "supersedes_id": str(supersedes_id) if supersedes_id else None})
    return ChildResult(sub_id, tid, no, acc, "created")


@_retrying
def set_status(conn, tender_id, status: str, actor: str, reason: str | None = None) -> bool:
    """Change tender.status and log status_changed. Returns False if it was already that status."""
    actor = _require_actor(actor)
    if status == "merged":
        raise IngestError("use merge_tender() to merge tenders")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT status FROM tender WHERE id = %s FOR UPDATE", (tender_id,))
        row = cur.fetchone()
        if not row:
            raise IngestError(f"tender {tender_id} not found")
        if row[0] == status:
            return False
        cur.execute("UPDATE tender SET status = %s, updated_at = now() WHERE id = %s", (status, tender_id))
        _event(cur, tender_id, "status_changed", actor, {"from": row[0], "to": status, "reason": reason})
    return True


# ---------- CLI ----------

def _backfill_norms(conn) -> int:
    """Old rows (from before 002) have no title_norm/buyer_norm; the candidates job needs them."""
    n = 0
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, buyer FROM tender WHERE title_norm IS NULL OR "
                    "(buyer IS NOT NULL AND buyer_norm IS NULL)")
        rows = cur.fetchall()
    for tid, title, buyer in rows:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("UPDATE tender SET title_norm = %s, buyer_norm = %s WHERE id = %s",
                        (dedupe.clean_title(title), dedupe.clean_buyer(buyer) or None, tid))
        n += 1
    return n


def _backfill_observed(conn) -> int:
    """After upgrading to 004: give each PRIMARY reference an observed snapshot, so its first
    scrape after the upgrade is compared with what that platform showed, not treated as a first
    observation. Values = what the tender holds now (it came from that platform), except a
    deadline later moved by a deadline_change amendment: the platform last showed the original.
    Secondary references are left empty on purpose (their first scrape is only recorded)."""
    n = 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT r.source, r.source_ref, r.first_seen_at, t.id, t.title, t.buyer, t.deadline_at,
                              t.status, t.description, t.estimated_value, t.currency, t.cpv_codes,
                              (SELECT e.payload->>'from' FROM tender_event e
                                WHERE e.tender_id = t.id AND e.event_type = 'deadline_changed'
                                  AND e.occurred_at > r.first_seen_at ORDER BY e.id LIMIT 1) AS original_deadline
                       FROM tender_source_ref r JOIN tender t ON t.id = r.tender_id
                       WHERE r.is_primary AND r.observed IS NULL""")
        rows = cur.fetchall()
    for r in rows:
        snap = {f: r[f] for f in changes.TRACKED_FIELDS if r[f] is not None}
        if snap.get("status") not in changes.PLATFORM_STATUSES:
            snap.pop("status", None)
        if r["original_deadline"]:
            snap["deadline_at"] = datetime.fromisoformat(r["original_deadline"])
        snap = {f: changes.jsonable(v) for f, v in snap.items()}
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("UPDATE tender_source_ref SET observed = %s WHERE source = %s AND source_ref = %s "
                        "AND observed IS NULL", (Jsonb(snap), r["source"], r["source_ref"]))
            n += cur.rowcount
    return n


def main(argv: list[str]) -> None:
    import psycopg
    ap = argparse.ArgumentParser(description="Tender & RFP Cockpit ingestion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ld = sub.add_parser("load", help="ingest a JSON-lines file")
    ld.add_argument("file")
    ld.add_argument("--actor", required=True)
    sub.add_parser("backfill-norms")
    sub.add_parser("backfill-observed", help="after upgrading to 004: snapshot primary refs")
    lp = sub.add_parser("lots", help="extract lots from a notice/document file and add them to a tender")
    lp.add_argument("accession")
    lp.add_argument("file")
    lp.add_argument("--format", choices=("codice", "eforms", "ocds", "text"),
                    help="default: the tender platform's data_format (text for html/pdf/other)")
    lp.add_argument("--actor", required=True)
    args = ap.parse_args(argv)
    with psycopg.connect(os.environ.get("DATABASE_URL", ""), autocommit=True) as conn:
        if args.cmd == "backfill-norms":
            print(f"updated {_backfill_norms(conn)} tender(s)")
            return
        if args.cmd == "backfill-observed":
            print(f"snapshotted {_backfill_observed(conn)} primary reference(s)")
            return
        if args.cmd == "lots":
            row = conn.execute("""SELECT t.id, t.source, p.data_format FROM tender t
                                  JOIN source_platform p ON p.code = t.source
                                  WHERE t.id = resolve_accession(%s)""", (args.accession,)).fetchone()
            if not row:
                raise SystemExit(f"tender {args.accession} not found")
            fmt = args.format or (row[2] if row[2] in ("codice", "eforms", "ocds") else "text")
            data = open(args.file, "rb").read()
            if fmt == "text":
                ex = lotlib.HeuristicTextLotExtractor()
                found, name = ex.extract(data.decode("utf-8", "replace")), ex.name
            else:
                found, name = lotlib.extract_lots(data, fmt), None
            res = ingest_lots(conn, row[0], found, args.actor, source=row[1], extractor=name)
            for lot, r in zip(found, res):
                print(f"{r.outcome:<9} {r.accession_no}  [{lot['origin']}] {lot.get('source_lot_label')}: {lot['title'][:60]}")
            print(f"{len(found)} lot(s) found ({fmt})")
            return
        counts: dict[str, int] = {}
        with open(args.file, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    r = ingest_tender(conn, json.loads(line), args.actor)
                    counts[r.outcome] = counts.get(r.outcome, 0) + 1
                    extra = f"  changed {','.join(r.changed)}" if r.changed else ""
                    extra += f"  not applied {','.join(r.not_applied)}" if r.not_applied else ""
                    print(f"{lineno}: {r.outcome:<19} {r.accession_no}{extra}")
                except (IngestError, json.JSONDecodeError) as e:
                    counts["rejected"] = counts.get("rejected", 0) + 1
                    print(f"{lineno}: rejected            {e}", file=sys.stderr)
        print(counts)


if __name__ == "__main__":
    main(sys.argv[1:])
