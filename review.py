"""Review the Tender & RFP Cockpit duplicate queue.

  python review.py list               show pending pairs
  python review.py stats              how often each similarity band was a real duplicate
  python review.py same NEW EXISTING  merge NEW into EXISTING (accession numbers)
  python review.py different NEW EXISTING
  python review.py interactive        step through the queue with s / d / k(skip) / q
  python review.py history ACCESSION  event log for one tender (tender_event)
  python review.py show ACCESSION     current tender (and child) for any ID, old or new, plus platform refs
  python review.py lock ACCESSION FIELD     stop scrapers overwriting FIELD (they only record differences)
  python review.py unlock ACCESSION FIELD
  python review.py edit ACCESSION FIELD VALUE   set FIELD by hand (also locks it)
  python review.py lots ACCESSION     lots of a tender with origin and confirmation state
  python review.py confirm-lot LOT_ACCESSION   confirm an AI-extracted lot (e.g. TRC-26-PLC-000001-L/L02)
FIELD is one of title, buyer, deadline_at, status, description, estimated_value, currency, cpv_codes.

Set DATABASE_URL, e.g. postgresql://user:pass@host/db
Decisions are logged in tender_event with actor TRC_ACTOR, default "review:<login name>".
"""
from __future__ import annotations

import getpass
import os
import sys

import psycopg

LIST_SQL = """SELECT new_accession, existing_accession, title_similarity, deadline_gap_days,
                     new_source, existing_source, new_title, existing_title,
                     new_buyer, existing_buyer, new_deadline, existing_deadline,
                     tender_id, candidate_id
              FROM duplicate_review_queue"""


ACTOR = os.environ.get("TRC_ACTOR") or f"review:{getpass.getuser()}"


def _ids(cur, new_acc: str, existing_acc: str):
    cur.execute("SELECT resolve_accession(%s), resolve_accession(%s)", (new_acc, existing_acc))
    a, b = cur.fetchone()
    if not a or not b:
        raise SystemExit("One of those accession numbers was not found.")
    return a, b


def _short(v, n=60) -> str:
    s = "∅" if v is None else (", ".join(v) if isinstance(v, list) else str(v))
    return s if len(s) <= n else s[: n - 1] + "…"


REASONS = {"locked": "locked by a person", "lower_priority": "lower-priority platform",
           "stale_copy": "old copy (matched an old or merged-duplicate hash)", "merged_duplicate_ref": "ref of a merged duplicate",
           "first_seen_on_secondary": "first seen on a secondary platform",
           "unchanged_on_platform": "platform still shows the value it always showed"}


def describe(typ: str, p: dict) -> str:
    """One-line summary per event; fields_changed details come from diff_lines()."""
    if typ == "fields_changed":
        bits = [f"{len(p.get('changes', {}))} changed"] if p.get("changes") else []
        if p.get("not_applied"):
            bits.append(f"{len(p['not_applied'])} not applied")
        if p.get("hash"):
            h = p["hash"]
            bits.append("hash updated" if h["updated"] else f"hash kept ({h.get('reason')}"
                        + (f" with {h['conflict_with']})" if h.get("conflict_with") else ")"))
        via = p.get("match")
        return f"[{via}{' ' + p['source_ref'] if p.get('source_ref') else ''}] " + ", ".join(bits or ["hash only"])
    if typ == "deadline_changed":
        return f"{_short(p.get('from'))} → {_short(p.get('to'))} ({p.get('amendment')})"
    if typ in ("field_locked", "field_unlocked"):
        return f"{p.get('field')}" + (f" ({p['reason']})" if p.get("reason") else "")
    if typ == "status_changed":
        return f"{p.get('from')} → {p.get('to')}"
    if typ in ("created", "matched_hash", "matched_source_ref"):
        return " ".join(f"{k}={v}" for k, v in (("ref", p.get("source_ref")), ("via", p.get("via"))) if v)
    if typ == "lot_added":
        return (f"{p.get('accession_no')} lot {p.get('source_lot_label') or p.get('source_lot_ref') or '-'}"
                f" [{p.get('lot_origin', 'structured')}] {_short(p.get('title'), 50)}"
                + (f" (by {p['extractor']})" if p.get("extractor") else ""))
    if typ == "lot_confirmed":
        return f"{p.get('accession_no')} confirmed ({p.get('via')})"
    if typ == "merged":
        return (f"into {p.get('into_accession')}" if p.get("role") == "duplicate"
                else f"absorbed {p.get('absorbed_accession')}")
    keys = ("accession_no", "kind", "title", "opco", "other")
    return " ".join(f"{k}={p[k]}" for k in keys if p.get(k))


def diff_lines(typ: str, p: dict) -> list[str]:
    if typ != "fields_changed":
        return []
    out = [f"{f}: {_short(c['old'])}  →  {_short(c['new'])}" for f, c in p.get("changes", {}).items()]
    out += [f"{f}: kept {_short(c['old'])}; platform shows {_short(c['new'])}  (not applied: "
            f"{REASONS.get(c.get('reason'), c.get('reason'))})" for f, c in p.get("not_applied", {}).items()]
    return out


def show(row) -> None:
    (na, ea, sim, gap, ns, es, nt, et, nb, eb, nd, ed, *_ ) = row
    print(f"\n{na}  vs  {ea}    title similarity {sim}, deadlines {gap} days apart")
    print(f"  new      [{ns}] {nt}\n           {nb} | deadline {nd:%Y-%m-%d}" if nd else f"  new      [{ns}] {nt}\n           {nb}")
    print(f"  existing [{es}] {et}\n           {eb} | deadline {ed:%Y-%m-%d}" if ed else f"  existing [{es}] {et}\n           {eb}")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__); return
    cmd = argv[0]
    with psycopg.connect(os.environ.get("DATABASE_URL", ""), autocommit=True) as conn, conn.cursor() as cur:
        if cmd == "list":
            cur.execute(LIST_SQL)
            rows = cur.fetchall()
            for r in rows:
                show(r)
            print(f"\n{len(rows)} pair(s) waiting for review.")
        elif cmd == "stats":
            cur.execute("SELECT sim_from, sim_to, confirmed_same, confirmed_different, pending, pct_same FROM duplicate_review_stats")
            print(f"{'similarity':<14}{'same':>6}{'diff':>6}{'pending':>9}{'% same':>8}")
            for f, t, s, d, p, pct in cur.fetchall():
                print(f"{f}-{t:<9}{s:>6}{d:>6}{p:>9}{'' if pct is None else pct:>8}")
        elif cmd in ("same", "different") and len(argv) == 3:
            new_id, ex_id = _ids(cur, argv[1], argv[2])
            if cmd == "same":
                cur.execute("SELECT merge_tender(%s, %s, %s)", (new_id, ex_id, ACTOR))
                print(f"Merged {argv[1]} into {argv[2]}. The old number still resolves.")
            else:
                cur.execute("SELECT mark_not_duplicate(%s, %s, %s)", (new_id, ex_id, ACTOR))
                print(f"Marked {argv[1]} and {argv[2]} as different tenders.")
        elif cmd == "history" and len(argv) == 2:
            cur.execute("SELECT resolve_accession(%s)", (argv[1],))
            tid = cur.fetchone()[0]
            if not tid:
                raise SystemExit("Accession number not found.")
            cur.execute("""SELECT e.occurred_at, e.event_type, e.actor, e.source, e.payload
                           FROM tender_event e
                           WHERE e.tender_id = %s OR e.tender_id IN (SELECT id FROM tender WHERE merged_into = %s)
                           ORDER BY e.id""", (tid, tid))
            for at, typ, actor, src, payload in cur.fetchall():
                print(f"{at:%Y-%m-%d %H:%M:%S}  {typ:<19} {actor:<22} {src or '':<4} {describe(typ, payload)}")
                for line in diff_lines(typ, payload):
                    print(f"{'':>22}{line}")
        elif cmd == "show" and len(argv) == 2:
            cur.execute("SELECT kind, accession_no FROM resolve_child_accession(%s)", (argv[1],))
            child = cur.fetchone()
            cur.execute("SELECT resolve_accession(%s)", (argv[1],))
            tid = cur.fetchone()[0]
            if not tid:
                raise SystemExit("Accession number not found.")
            cur.execute("SELECT accession_no, title, buyer, deadline_at, status FROM tender WHERE id = %s", (tid,))
            acc, title, buyer, deadline, status = cur.fetchone()
            print(f"{acc}  [{status}]  {title}\n  {buyer} | deadline {deadline:%Y-%m-%d %H:%M}" if deadline
                  else f"{acc}  [{status}]  {title}\n  {buyer}")
            if child:
                print(f"  child {argv[1].upper()} is now {child[1]}")
            cur.execute("SELECT source, source_ref, is_primary, last_seen_at FROM tender_source_ref "
                        "WHERE tender_id = %s ORDER BY is_primary DESC, first_seen_at", (tid,))
            for src, ref, prim, seen in cur.fetchall():
                print(f"  ref {src} {ref:<24} {'primary' if prim else '':<8} last seen {seen:%Y-%m-%d %H:%M}")
        elif cmd in ("lock", "unlock") and len(argv) == 3:
            from ingest import lock_field, unlock_field
            cur.execute("SELECT resolve_accession(%s)", (argv[1],))
            tid = cur.fetchone()[0]
            if not tid:
                raise SystemExit("Accession number not found.")
            fn = lock_field if cmd == "lock" else unlock_field
            done = fn(conn, tid, argv[2], ACTOR, reason="review.py")
            print(f"{argv[2]} {'locked' if cmd == 'lock' else 'unlocked'} on {argv[1]}" if done
                  else f"{argv[2]} was already {'locked' if cmd == 'lock' else 'unlocked'}")
        elif cmd == "edit" and len(argv) == 4:
            from ingest import edit_field
            cur.execute("SELECT resolve_accession(%s)", (argv[1],))
            tid = cur.fetchone()[0]
            if not tid:
                raise SystemExit("Accession number not found.")
            res = edit_field(conn, tid, argv[2], argv[3], ACTOR)
            print(f"{argv[2]} set and locked" if res.changes else f"{argv[2]} unchanged (now locked)")
        elif cmd == "lots" and len(argv) == 2:
            cur.execute("SELECT resolve_accession(%s)", (argv[1],))
            tid = cur.fetchone()[0]
            if not tid:
                raise SystemExit("Accession number not found.")
            cur.execute("""SELECT accession_no, source_lot_ref, source_lot_label, lot_origin, confirmed_by,
                                  confirmed_at, title, estimated_value, currency, cpv_codes
                           FROM tender_lot WHERE tender_id = %s ORDER BY lot_no""", (tid,))
            rows = cur.fetchall()
            pending = 0
            for acc, ref, label, origin, by, at, title, val, curr, cpv in rows:
                if origin == "ai_extracted" and not at:
                    state, pending = "NEEDS CONFIRMATION", pending + 1
                elif at:
                    state = f"confirmed by {by} {at:%Y-%m-%d}"
                else:
                    state = ""
                money = f" | {val:,.2f} {curr or ''}".rstrip() if val is not None else ""
                print(f"{acc}  ref {ref or '-':<6} label {_short(label, 18):<18} {origin:<12} {state}")
                print(f"{'':>4}{_short(title, 70)}{money}" + (f" | CPV {', '.join(cpv)}" if cpv else ""))
            print(f"{len(rows)} lot(s)" + (f", {pending} AI-extracted lot(s) waiting for confirmation"
                                           f" (python review.py confirm-lot LOT_ACCESSION)" if pending else ""))
        elif cmd == "confirm-lot" and len(argv) == 2:
            from ingest import confirm_lot
            done = confirm_lot(conn, argv[1], ACTOR)
            print(f"lot {argv[1]} confirmed" if done else f"lot {argv[1]} was already confirmed")
        elif cmd == "interactive":
            cur.execute(LIST_SQL)
            for r in cur.fetchall():
                show(r)
                ans = input("  [s]ame / [d]ifferent / [k] skip / [q]uit: ").strip().lower()
                if ans == "q":
                    break
                if ans == "s":
                    cur.execute("SELECT merge_tender(%s, %s, %s)", (r[12], r[13], ACTOR)); print("  merged")
                elif ans == "d":
                    cur.execute("SELECT mark_not_duplicate(%s, %s, %s)", (r[12], r[13], ACTOR)); print("  marked different")
        else:
            print(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ValueError as e:        # ingest.IngestError: bad field name / value
        raise SystemExit(f"error: {e}")
