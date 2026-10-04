"""Tender & RFP Cockpit accession numbers: TRC-YY-SRC-NNNNNN-C[/L01|/A01|/S01]."""
from __future__ import annotations

import re
from dataclasses import dataclass

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*"
_ID = re.compile(
    r"^TRC-(?P<yy>\d{2})-(?P<source>[A-Z]{3})-(?P<number>\d{6})-(?P<check>[0-9A-Z*])"
    r"(?:/(?P<kind>[LAS])(?P<child_no>\d{2,3}))?$"
)


def mod37_2_check(body: str) -> str:
    """ISO 7064 MOD 37-2 check character. Matches the Postgres mod37_2_check()."""
    clean = re.sub(r"[^0-9A-Za-z]", "", body).upper()
    p = 0
    for ch in clean:
        p = ((p + ALPHABET.index(ch)) * 2) % 37
    return ALPHABET[(38 - p) % 37]


def make(yy: int, source: str, number: int) -> str:
    if not 1 <= number <= 999_999:
        raise ValueError("number must be between 1 and 999999")
    if not re.fullmatch(r"[A-Za-z]{3}", source):
        raise ValueError("source must be three letters")
    body = f"TRC-{yy % 100:02d}-{source.upper()}-{number:06d}"
    return f"{body}-{mod37_2_check(body)}"


def child(parent: str, kind: str, child_no: int) -> str:
    """Build a lot/amendment/submission ID, e.g. child(p, 'L', 2) -> '.../L02'."""
    if kind not in ("L", "A", "S"):
        raise ValueError("kind must be L, A or S")
    if not is_valid(parent) or "/" in parent:
        raise ValueError(f"not a valid parent accession number: {parent}")
    return f"{parent}/{kind}{child_no:02d}"


@dataclass(frozen=True)
class Accession:
    yy: int
    source: str
    number: int
    check: str
    kind: str | None = None        # 'L', 'A', 'S' or None for the tender itself
    child_no: int | None = None

    @property
    def parent(self) -> str:
        return f"TRC-{self.yy:02d}-{self.source}-{self.number:06d}-{self.check}"


def normalise(raw: str) -> str:
    """Tidy pasted input: trim, uppercase, and accept spaces or en dashes instead of hyphens."""
    return re.sub(r"[\s\u2013\u2014_]+", "-", raw.strip().upper()).replace("-/", "/")


def parse(raw: str) -> Accession:
    """Parse and validate an ID. Raises ValueError with a readable reason."""
    s = normalise(raw)
    m = _ID.match(s)
    if not m:
        raise ValueError(f"'{raw}' is not in the format TRC-YY-SRC-NNNNNN-C")
    body = s.split("/", 1)[0][:-2]
    if mod37_2_check(body) != m["check"]:
        raise ValueError(f"'{raw}' has a wrong check character; it may contain a typo")
    return Accession(
        yy=int(m["yy"]), source=m["source"], number=int(m["number"]), check=m["check"],
        kind=m["kind"], child_no=int(m["child_no"]) if m["child_no"] else None,
    )


def is_valid(raw: str) -> bool:
    try:
        parse(raw)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    # Values produced by the Postgres functions, so both sides must agree.
    assert make(26, "PLC", 1) == "TRC-26-PLC-000001-L"
    assert make(26, "PLC", 2) == "TRC-26-PLC-000002-J"
    assert make(26, "PLC", 4817) == "TRC-26-PLC-004817-P"
    assert child("TRC-26-PLC-000001-L", "L", 2) == "TRC-26-PLC-000001-L/L02"
    a = parse(" trc 26 plc 000001 l/s02 ")
    assert (a.kind, a.child_no, a.parent) == ("S", 2, "TRC-26-PLC-000001-L")
    assert not is_valid("TRC-26-PLC-000001-J")     # wrong check character
    assert not is_valid("TRC-26-PLC-000010-L")     # typo in the number
    assert not is_valid("TRC-26-PLC-000001-L/X01") # unknown child kind
    print("all checks passed")
