"""Background duplicate-candidate job for Tender & RFP Cockpit.

Finds near-duplicates the exact dedupe hash cannot catch and puts them in
tender_possible_duplicate for a person to review (review.py). It never merges.

A pair is flagged when both tenders are live, have the same normalised buyer
(dedupe.clean_buyer, stored in tender.buyer_norm), deadlines within --days of
each other, and pg_trgm similarity of the normalised titles >= --threshold.

It scans tenders created or changed (tender.scan_at) since the watermark in job_state (minus a small
overlap, so rows from transactions that committed late are not missed), in
batches, committing each batch and advancing the watermark. Re-running is safe:
existing pairs (in either orientation, including ones a reviewer already
resolved) are never inserted again.

    python candidates.py                      # one pass from the stored watermark
    python candidates.py --since 2026-01-01   # rescan from a date (watermark only moves forward)
    python candidates.py --threshold 0.55 --days 10
    python candidates.py --loop --interval 300

Cron alternative to --loop (every 5 minutes):
    */5 * * * * cd /path/to/trc/v2 && DATABASE_URL=... .venv/bin/python candidates.py >> candidates.log 2>&1

Only one copy runs at a time (session advisory lock); a second copy exits at once.
Set DATABASE_URL, e.g. postgresql://user:pass@host/db
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

JOB_NAME = "duplicate_candidates"
DEFAULT_THRESHOLD = 0.6
DEFAULT_DAYS = 7
DEFAULT_BATCH = 500
DEFAULT_OVERLAP = timedelta(minutes=10)

# Next batch of new or changed tenders, keyset-paginated on (scan_at, id). scan_at is the
# insert time, bumped by ingest.py whenever title, buyer or deadline change (004).
BATCH_SQL = """
SELECT id, scan_at FROM tender
WHERE scan_at >= %(scan_from)s AND scan_at <= %(scan_to)s
  AND (scan_at, id) > (%(after_ts)s, %(after_id)s)
ORDER BY scan_at, id
LIMIT %(batch)s
"""

# Pairs are stored newer -> older (tender_id is the newer one), so the same pair
# found from either side lands on the same primary key.
FLAG_SQL = """
INSERT INTO tender_possible_duplicate (tender_id, candidate_id, reason, similarity)
SELECT DISTINCT
  CASE WHEN (n.created_at, n.id) > (c.created_at, c.id) THEN n.id ELSE c.id END,
  CASE WHEN (n.created_at, n.id) > (c.created_at, c.id) THEN c.id ELSE n.id END,
  %(reason)s,
  similarity(n.title_norm, c.title_norm)
FROM tender n
JOIN tender c
  ON  c.buyer_norm = n.buyer_norm
  AND c.id <> n.id
  AND c.merged_into IS NULL AND c.status <> 'withdrawn'
  AND c.deadline_at BETWEEN n.deadline_at - make_interval(days => %(days)s)
                        AND n.deadline_at + make_interval(days => %(days)s)
WHERE n.id = ANY(%(ids)s)
  AND n.merged_into IS NULL AND n.status <> 'withdrawn'
  AND similarity(n.title_norm, c.title_norm) >= %(threshold)s
  AND NOT EXISTS (SELECT 1 FROM tender_possible_duplicate d
                  WHERE (d.tender_id, d.candidate_id) IN ((n.id, c.id), (c.id, n.id)))
ON CONFLICT (tender_id, candidate_id) DO NOTHING
"""

SAVE_STATE_SQL = """
INSERT INTO job_state (job_name, watermark, last_run, updated_at)
VALUES (%(job)s, %(wm)s, %(stats)s, now())
ON CONFLICT (job_name) DO UPDATE
  SET watermark = greatest(job_state.watermark, EXCLUDED.watermark),
      last_run = EXCLUDED.last_run, updated_at = now()
"""

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def get_watermark(conn) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT watermark FROM job_state WHERE job_name = %s", (JOB_NAME,))
        row = cur.fetchone()
    return row[0] if row else None     # None = never run


def run_once(conn, *, since: datetime | None = None, threshold: float = DEFAULT_THRESHOLD,
             days: int = DEFAULT_DAYS, batch: int = DEFAULT_BATCH,
             overlap: timedelta = DEFAULT_OVERLAP) -> dict:
    """One pass. `conn` must be autocommit (each batch commits on its own)."""
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    if days < 0:
        raise ValueError("days must be >= 0")
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        scan_to = cur.fetchone()[0]
    if since is None:
        wm = get_watermark(conn)
        scan_from = (wm - overlap) if wm else EPOCH
    else:
        scan_from = since
    reason = f"candidates job: same buyer, title similarity >= {threshold}, deadline within {days} days"
    stats = {"scan_from": scan_from.isoformat(), "scan_to": scan_to.isoformat(), "scanned": 0,
             "flagged": 0, "batches": 0, "threshold": threshold, "days": days}
    after_ts, after_id = EPOCH - timedelta(days=1), ZERO_UUID
    while True:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(BATCH_SQL, {"scan_from": scan_from, "scan_to": scan_to, "after_ts": after_ts,
                                    "after_id": after_id, "batch": batch})
            rows = cur.fetchall()
            if not rows:
                break
            cur.execute(FLAG_SQL, {"ids": [r[0] for r in rows], "reason": reason,
                                   "days": days, "threshold": threshold})
            stats["flagged"] += cur.rowcount
            stats["scanned"] += len(rows)
            stats["batches"] += 1
            after_id, after_ts = rows[-1]
            cur.execute(SAVE_STATE_SQL, {"job": JOB_NAME, "wm": after_ts, "stats": Jsonb(stats)})
    if stats["batches"] == 0:   # nothing new: still record the run
        with conn.cursor() as cur:
            cur.execute(SAVE_STATE_SQL, {"job": JOB_NAME, "wm": get_watermark(conn) or EPOCH,
                                         "stats": Jsonb(stats)})
    stats["watermark"] = (get_watermark(conn) or EPOCH).isoformat()
    return stats


def _parse_since(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Flag possible duplicate tenders for review")
    ap.add_argument("--since", type=_parse_since, help="ISO date/time to rescan from (default: stored watermark)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="title similarity, default 0.6")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="max deadline gap in days, default 7")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="tenders per batch, default 500")
    ap.add_argument("--overlap-minutes", type=float, default=DEFAULT_OVERLAP.total_seconds() / 60,
                    help="re-scan this far behind the watermark to catch late commits, default 10")
    ap.add_argument("--loop", action="store_true", help="keep running every --interval seconds")
    ap.add_argument("--interval", type=int, default=300, help="seconds between passes with --loop")
    a = ap.parse_args(argv)

    with psycopg.connect(os.environ.get("DATABASE_URL", ""), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", ("trc:job:" + JOB_NAME,))
            if not cur.fetchone()[0]:
                print("another candidates job is running; exiting", file=sys.stderr)
                return 1
        since = a.since
        while True:
            s = run_once(conn, since=since, threshold=a.threshold, days=a.days, batch=a.batch,
                         overlap=timedelta(minutes=a.overlap_minutes))
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] scanned {s['scanned']} tender(s) in {s['batches']} "
                  f"batch(es), flagged {s['flagged']} new pair(s); watermark {s['watermark']}", flush=True)
            if not a.loop:
                return 0
            since = None   # --since applies to the first pass only
            time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
