"""Change tracking for re-scraped tenders (Tender & RFP Cockpit).

When a scrape matches an existing tender, ingest.py calls apply_changes() inside the
same transaction as find_or_create_tender, so the reference/hash advisory locks
taken there are still held, plus a row lock on the tender. This module decides:

  * what differs     : diff(current row, scraped values), field by field
  * who may write it : source precedence (source_platform.priority, v5), manual locks (tender.locked_fields),
                       stale copies (matched through an old hash) and refs that came over in a
                       merge never overwrite; they are recorded as "not_applied"
  * the hash         : recomputed here (never in SQL) from the new title/buyer/deadline. If
                       another live tender already has it, the old hash is kept, the clash is
                       recorded and the pair is queued for review. Replaced hashes go to
                       tender_hash_history so old copies still match.

Everything here is plain Python + simple SQL, so it moves to Snowflake unchanged
except the advisory-lock calls (single writer there).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.rows import dict_row

import dedupe

# ---------- configuration ----------

# Who may overwrite a tender's fields. The tender's primary source (tender.source) always may;
# another platform may if its priority is equal or higher. Since v5 the priorities live in
# source_platform.priority (seeded PLC 100, FTS 100, TED 50, others 10; change them with
# `platforms.py update CODE --priority N`). A code missing from the table gets DEFAULT_PRIORITY.
DEFAULT_PRIORITY = 10

TRACKED_FIELDS = ("title", "buyer", "deadline_at", "status", "description",
                  "estimated_value", "currency", "cpv_codes")
HASH_FIELDS = ("title", "buyer", "deadline_at")

# Statuses a platform can report. Anything else in tender.status (in_bid, withdrawn, merged, ...)
# is our own workflow state: a platform saying "open" never overwrites it, but a real platform
# event (cancelled, awarded, ...) does, unless status is locked.
PLATFORM_STATUSES = ("open", "closed", "suspended", "cancelled", "awarded")
STATUS_ALIASES = {"canceled": "cancelled", "active": "open", "published": "open", "unsuccessful": "cancelled"}
MAX_DESCRIPTION = 20000


class FieldError(ValueError):
    pass


# ---------- validation of the optional tracked fields ----------

def clean_status(v: Any) -> str | None:
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().lower()
    s = STATUS_ALIASES.get(s, s)
    if s not in PLATFORM_STATUSES:
        raise FieldError(f"status {v!r} is not a platform status {PLATFORM_STATUSES}")
    return s


def clean_description(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if len(s) > MAX_DESCRIPTION:
        raise FieldError(f"description is longer than {MAX_DESCRIPTION} characters")
    return s or None


def clean_value(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as e:
        raise FieldError(f"estimated_value {v!r} is not a number") from e
    if d < 0:
        raise FieldError("estimated_value cannot be negative")
    return d


def clean_currency(v: Any) -> str | None:
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", s):
        raise FieldError(f"currency {v!r} is not a three-letter code")
    return s


def clean_cpv(v: Any) -> list[str] | None:
    if v is None:
        return None
    items = re.split(r"[,;\s]+", v) if isinstance(v, str) else list(v)
    out = set()
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        if not re.fullmatch(r"\d{8}(-\d)?", s):
            raise FieldError(f"CPV code {it!r} is not in the form 12345678 or 12345678-9")
        out.add(s)
    return sorted(out) or None


# ---------- comparison / JSON ----------

def jsonable(v: Any) -> Any:
    """JSON form used in events and observed snapshots. Datetimes are always UTC so the same
    instant gives the same string whatever the session time zone."""
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (list, tuple)):
        return list(v)
    return v


def same(fieldname: str, a: Any, b: Any) -> bool:
    if fieldname == "cpv_codes":
        return sorted(a or []) == sorted(b or [])
    if fieldname == "estimated_value" and a is not None and b is not None:
        return Decimal(a) == Decimal(b)
    if fieldname == "currency" and a is not None and b is not None:
        return a.strip() == b.strip()
    return a == b


def cosmetic(fieldname: str, old: Any, new: Any) -> bool:
    """Differences dedupe ignores anyway (case, accents, punctuation, legal suffixes). Applied
    changes still log them; differences that are NOT applied skip them to keep the log useful."""
    if fieldname == "title":
        return dedupe.clean_title(old) == dedupe.clean_title(new)
    if fieldname == "buyer":
        return dedupe.clean_buyer(old) == dedupe.clean_buyer(new)
    return False


# ---------- precedence ----------

def load_priorities(cur, codes) -> dict:
    """{code: priority} from source_platform for the given codes."""
    cur.execute("SELECT code, priority FROM source_platform WHERE code = ANY(%s)",
                ([str(c).strip() for c in codes if c],))
    return {c.strip(): p for c, p in cur.fetchall()}


def priority(source: str | None, priorities: dict | None = None) -> int:
    return (priorities or {}).get((source or "").strip(), DEFAULT_PRIORITY)


def may_overwrite(primary_source: str, incoming_source: str, *, ref_origin: str | None,
                  stale: bool, priorities: dict | None = None) -> tuple[bool, str | None]:
    """Source-level rule: (allowed, reason_if_not). See field_rule() for the per-field refinement."""
    if stale:
        return False, "stale_copy"              # matched through a hash the tender no longer has
    if ref_origin == "merge":
        return False, "merged_duplicate_ref"    # a merged duplicate's platform cannot rewrite the survivor
    if incoming_source.strip() == primary_source.strip():
        return True, None
    if priority(incoming_source, priorities) >= priority(primary_source, priorities):
        return True, None
    return False, "lower_priority"


def field_rule(primary_source: str, incoming_source: str, *, ref_origin: str | None, stale: bool,
               previously_observed: dict, priorities: dict | None = None):
    """Per-field rule for re-scrapes, on top of may_overwrite():

      * a field may only be overwritten when the platform itself CHANGED it since we last saw
        that reference (previously_observed). Re-asserting the value it always showed never
        overwrites: that is what keeps a stale listing from undoing a deadline amendment or a
        manual edit, and two platforms that disagree from flipping a field back and forth.
      * no previous observation (first scrape of a ref after 004, or after unlock_field):
        the primary source may overwrite, another platform only records the difference.
    """
    ok, reason = may_overwrite(primary_source, incoming_source, ref_origin=ref_origin, stale=stale,
                               priorities=priorities)
    is_primary = incoming_source.strip() == primary_source.strip()

    def allow(f: str, new_json):
        if not ok:
            return False, reason
        if f not in previously_observed:
            return (True, None) if is_primary else (False, "first_seen_on_secondary")
        if previously_observed[f] == new_json:
            return False, "unchanged_on_platform"
        return True, None
    return allow, ok, reason


# ---------- applying ----------

TENDER_COLUMNS = ("id, accession_no, source, title, buyer, deadline_at, status, description, "
                  "estimated_value, currency, cpv_codes, locked_fields, dedupe_hash, merged_into, created_at")


@dataclass
class ChangeResult:
    changes: dict = field(default_factory=dict)       # field -> {old, new}
    not_applied: dict = field(default_factory=dict)   # field -> {old, new, reason}
    hash: dict | None = None                          # {old, new, updated, reason, conflict_with}

    @property
    def hash_updated(self) -> bool:
        return bool(self.hash and self.hash["updated"])


def lock_row(cur, tender_id) -> dict:
    """Row-lock the tender FOR UPDATE (following a merge to the survivor) and return it.
    Always lock before inserting anything that references the tender; see trc_lock_tender()."""
    with cur.connection.cursor(row_factory=dict_row) as dc:
        dc.execute("SELECT trc_lock_tender(%s) AS id", (tender_id,))
        tid = dc.fetchone()["id"]
        dc.execute(f"SELECT {TENDER_COLUMNS} FROM tender WHERE id = %s", (tid,))
        return dc.fetchone()


def diff(row: dict, desired: dict, *, allow=None, respect_locks: bool = True) -> tuple[dict, dict, dict]:
    """Returns (to_apply {field: new}, changes, not_applied).
    allow(field, new_json) -> (bool, reason) decides per field; None means always allowed."""
    apply, changes, not_applied = {}, {}, {}
    locked = set(row.get("locked_fields") or [])
    for f, new in desired.items():
        if f not in TRACKED_FIELDS or new is None:
            continue
        old = row[f]
        if same(f, old, new):
            continue
        if f == "status" and new == "open" and old not in PLATFORM_STATUSES:
            continue      # "still open" tells us nothing about our own workflow state
        entry = {"old": jsonable(old), "new": jsonable(new)}
        if respect_locks and f in locked:
            if not cosmetic(f, old, new):
                not_applied[f] = {**entry, "reason": "locked"}
        else:
            ok, reason = allow(f, entry["new"]) if allow else (True, None)
            if ok:
                changes[f] = entry
                apply[f] = new
            elif not cosmetic(f, old, new):
                not_applied[f] = {**entry, "reason": reason}
    return apply, changes, not_applied


def apply_changes(cur, row: dict, desired: dict, *, allow=None, respect_locks: bool = True,
                  held_hashes: frozenset | set = frozenset()) -> ChangeResult:
    """Diff, write what may be written, keep norms + hash in sync. Caller holds the row lock
    (lock_row) and the advisory locks for every hash in held_hashes."""
    to_apply, changes, not_applied = diff(row, desired, allow=allow, respect_locks=respect_locks)
    res = ChangeResult(changes, not_applied)
    after = {f: to_apply.get(f, row[f]) for f in HASH_FIELDS}
    expected = dedupe.dedupe_hash(after["title"], after["buyer"], after["deadline_at"])
    new_hash = row["dedupe_hash"]
    if expected != row["dedupe_hash"]:
        res.hash = {"old": row["dedupe_hash"], "new": expected, "updated": False, "reason": None,
                    "conflict_with": None}
        got = expected in held_hashes
        if not got:   # never block while holding the row lock: try, and retry on a later scrape
            cur.execute("SELECT pg_try_advisory_xact_lock(trc_lock_key('hash', %s))", (expected,))
            got = cur.fetchone()[0]
        if not got:
            res.hash["reason"] = "lock_busy"
        else:
            cur.execute("SELECT id, accession_no FROM tender WHERE dedupe_hash = %s AND id <> %s "
                        "AND merged_into IS NULL AND status <> 'withdrawn' LIMIT 1", (expected, row["id"]))
            clash = cur.fetchone()
            if clash:
                res.hash.update(reason="conflict", conflict_with=clash[1])
            else:
                res.hash["updated"] = True
                new_hash = expected
                cur.execute("INSERT INTO tender_hash_history (dedupe_hash, tender_id) VALUES (%s, %s) "
                            "ON CONFLICT (dedupe_hash, tender_id) DO UPDATE SET retired_at = now()",
                            (row["dedupe_hash"], row["id"]))
    if not to_apply and new_hash == row["dedupe_hash"]:
        return res      # nothing to write (a clash with no field change is not re-queued)
    sets, vals = [], []
    for f, v in to_apply.items():
        sets.append(f"{f} = %s")      # f is from TRACKED_FIELDS, never user input
        vals.append(v)
    if "title" in to_apply:
        sets.append("title_norm = %s"); vals.append(dedupe.clean_title(to_apply["title"]))
    if "buyer" in to_apply:
        sets.append("buyer_norm = %s"); vals.append(dedupe.clean_buyer(to_apply["buyer"]) or None)
    if new_hash != row["dedupe_hash"]:
        sets.append("dedupe_hash = %s"); vals.append(new_hash)
    if any(f in to_apply for f in HASH_FIELDS):
        sets.append("scan_at = now()")    # the candidates job re-checks it
    sets.append("updated_at = now()")
    cur.execute(f"UPDATE tender SET {', '.join(sets)} WHERE id = %s", (*vals, row["id"]))
    for f, v in to_apply.items():
        row[f] = v
    row["dedupe_hash"] = new_hash
    if res.hash and res.hash["reason"] == "conflict":
        cur.execute("SELECT id FROM tender WHERE accession_no = %s", (res.hash["conflict_with"],))
        _queue_clash(cur, row["id"], cur.fetchone()[0])
    return res


def _queue_clash(cur, a, b) -> None:
    """Two live tenders now describe the same tender: put the pair (newer -> older) in the review queue."""
    cur.execute(
        """INSERT INTO tender_possible_duplicate (tender_id, candidate_id, reason, similarity)
           SELECT CASE WHEN (x.created_at, x.id) > (y.created_at, y.id) THEN x.id ELSE y.id END,
                  CASE WHEN (x.created_at, x.id) > (y.created_at, y.id) THEN y.id ELSE x.id END,
                  'hash clash after re-scrape', similarity(x.title_norm, y.title_norm)
           FROM tender x, tender y
           WHERE x.id = %s AND y.id = %s
             AND NOT EXISTS (SELECT 1 FROM tender_possible_duplicate d
                             WHERE (d.tender_id, d.candidate_id) IN ((x.id, y.id), (y.id, x.id)))
           ON CONFLICT DO NOTHING""", (a, b))
