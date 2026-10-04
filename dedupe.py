"""Text cleaning and the exact-duplicate hash for Tender & RFP Cockpit.

This module is the ONLY place titles, buyers, deadlines and source references are
normalised and the dedupe hash is computed. SQL never recomputes them; it stores
what Python sends (tender.dedupe_hash, title_norm, buyer_norm). That keeps the rules
identical when the database moves from Postgres to Snowflake.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone

_STOP = {"the", "a", "an", "of", "for", "and", "to", "de", "la", "el", "los", "las", "y", "del", "en", "para"}
_LEGAL = re.compile(r"\b(ltd|limited|plc|llp|inc|sl|sa|slu|gmbh|nhs trust|trust)\b")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace(".", "")                # "S.A." -> "sa", "N.H.S." -> "nhs"
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(w for w in t.split() if w not in _STOP)


def clean_title(title: str | None) -> str:
    return _clean(title)


def clean_buyer(buyer: str | None) -> str:
    return " ".join(_LEGAL.sub(" ", _clean(buyer)).split())


def clean_deadline(deadline: datetime | None) -> str:
    """Day only, in UTC, so a re-post that shifts the time by an hour still matches."""
    if deadline is None:
        return ""
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline.astimezone(timezone.utc).strftime("%Y-%m-%d")


def dedupe_hash(title: str | None, buyer: str | None, deadline: datetime | None) -> str:
    key = "|".join([clean_title(title), clean_buyer(buyer), clean_deadline(deadline)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def clean_source_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    r = re.sub(r"\s+", "", ref).upper()
    return r or None


def find_or_create(conn, source: str, source_ref: str | None, title: str,
                   buyer: str | None, deadline: datetime | None, actor: str = "legacy"):
    """Backwards-compatible wrapper. New code should call ingest.ingest_tender().
    Returns (tender_id, accession_no, outcome)."""
    from ingest import ingest_tender   # local import: ingest depends on this module
    r = ingest_tender(conn, {"source": source, "source_ref": source_ref, "title": title,
                             "buyer": buyer, "deadline": deadline}, actor=actor)
    return r.tender_id, r.accession_no, r.outcome
