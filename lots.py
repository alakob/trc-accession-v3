"""Lot extraction for Tender & RFP Cockpit (v5).

Every extractor returns a list of plain dicts in one normalised shape:

    {"source_lot_ref": "1",            # canonical ref (canonical_lot_ref), used for idempotency/merges
     "source_lot_label": "LOT-0001",   # the platform's raw label
     "title": "...", "description": "..." | None, "cpv_codes": ["33696500"] | None,
     "estimated_value": Decimal | None, "currency": "EUR" | None,
     "origin": "structured" | "ai_extracted"}

Structured extractors (origin='structured'): the platform publishes lots as data.
    lots_from_codice(xml)   PLACSP CODICE (cac:ProcurementProjectLot, CODICE namespaces)
    lots_from_eforms(xml)   TED eForms UBL (cac:ProcurementProjectLot, cbc:ID schemeName="Lot")
    lots_from_ocds(json)    Find a Tender OCDS (releases[].tender.lots[], CPV from tender.items[].relatedLot)
    extract_lots(data, data_format)   dispatch on source_platform.data_format

Text extractor (origin='ai_extracted'): lots guessed from free text (tender documents, HTML/PDF
text). TextLotExtractor is the plug-in interface; HeuristicTextLotExtractor is a regex-based
implementation (no AI model is called). An LLM extractor can implement the same interface later:

    class MyLLMExtractor(TextLotExtractor):
        name = "llm:my-model"
        def extract(self, text: str) -> list[dict]: ...   # same dict shape, origin='ai_extracted'

Lots with origin 'ai_extracted' need a person to confirm them (review.py confirm-lot).
Empty, missing or unparseable input gives [] (never raises on content; raises TypeError on a
wrong argument type).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

# ---------- canonical lot refs ----------
# Must stay identical to trc_canonical_lot_ref() in 005_platforms_lots.sql (tests compare them).
_ACCENTS = str.maketrans(
    "áàâäãåāéèêëēíìîïīóòôöõøōúùûüūçñýÿÁÀÂÄÃÅĀÉÈÊËĒÍÌÎÏĪÓÒÔÖÕØŌÚÙÛÜŪÇÑÝºª°",
    "aaaaaaaeeeeeiiiiiooooooouuuuucnyyAAAAAAAEEEEEIIIIIOOOOOOOUUUUUCNYoao")
_PREFIX = re.compile(r"^(LOTES|LOTE|LOTS|LOT|LOTTI|LOTTO|LOSE|LOS|PARTIDAS|PARTIDA)(?= |[0-9]|$) ?")
_MARKER = re.compile(r"^(NO|NR|NUM|NUMERO|NUMBER|N)(?= |[0-9]) ?")
_NUMBER = re.compile(r"^([0-9]+)([A-Z]?)(?= |$)")


def canonical_lot_ref(label: Any) -> str | None:
    """'Lote 1', 'Lot 1', 'LOT-0001', 'Lot nº 1', '1' -> '1'; 'Lot A - Hardware' -> 'A';
    a label without a lot word that is not a number is kept whole (upper case, accents folded)."""
    if label is None:
        return None
    s = str(label).translate(_ACCENTS)
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).strip().upper()
    if not s:
        return None
    had_prefix = False
    m = _PREFIX.match(s)
    if m:
        had_prefix = True
        word = m.group(1)
        s = s[len(word):].strip()
        s = _MARKER.sub("", s, count=1).strip()
        if not s:
            return word
    m = _NUMBER.match(s)
    if m:
        return (m.group(1).lstrip("0") or "0") + m.group(2)
    if had_prefix:
        return s.split(" ")[0]
    return s


# ---------- small helpers ----------

def _text(el) -> str | None:
    if el is None or el.text is None:
        return None
    s = " ".join(el.text.split())
    return s or None


def _decimal(v) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        d = Decimal(str(v).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return d.quantize(Decimal("0.01")) if d >= 0 else None


def _cpv(codes: Iterable[str | None]) -> list[str] | None:
    out = []
    for c in codes:
        c = (c or "").strip()
        m = re.fullmatch(r"(\d{8})(-\d)?", c)
        if m and c not in out:
            out.append(c)                # kept as published (with check digit if given), like changes.clean_cpv
    return sorted(out) or None


def _lot(label, title, description=None, cpv=None, value=None, currency=None, origin="structured") -> dict:
    label = " ".join(str(label).split()) if label is not None else None
    title = " ".join(str(title).split()) if title else None
    return {"source_lot_ref": canonical_lot_ref(label), "source_lot_label": label,
            "title": title or (f"Lot {label}" if label else "Lot"),
            "description": description or None, "cpv_codes": cpv,
            "estimated_value": value, "currency": (currency or "").strip().upper() or None,
            "origin": origin}


def _parse_xml(xml) -> ET.Element | None:
    if xml is None:
        return None
    if isinstance(xml, ET.Element):
        return xml
    if isinstance(xml, (bytes, bytearray)):
        data = bytes(xml)
    elif isinstance(xml, str):
        data = xml                      # ElementTree reads str with or without an XML declaration
    else:
        raise TypeError("xml must be str, bytes or an ElementTree element")
    if not data.strip():
        return None
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError):
        return None


def _first_lang(parent, path: str, ns: dict, prefer=("SPA", "ENG")) -> str | None:
    """First non-empty text among multilingual elements (eForms repeats Name per languageID)."""
    els = parent.findall(path, ns)
    for lang in prefer:
        for e in els:
            if e.get("languageID") == lang and _text(e):
                return _text(e)
    for e in els:
        if _text(e):
            return _text(e)
    return None


# ---------- (a) PLACSP CODICE ----------
CODICE_NS = {
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cac-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
    "cbc-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
}


def codice_folders(xml) -> list[tuple[str | None, ET.Element]]:
    """(ContractFolderID, ContractFolderStatus element) for each tender in a PLACSP ATOM feed."""
    root = _parse_xml(xml)
    if root is None:
        return []
    ns = CODICE_NS
    tag = f"{{{ns['cac-place-ext']}}}ContractFolderStatus"
    folders = [root] if root.tag == tag else list(root.iter(tag))
    return [(_text(f.find("cbc:ContractFolderID", ns)), f) for f in folders]


def lots_from_codice(xml, folder_id: str | None = None) -> list[dict]:
    """PLACSP CODICE: an ATOM feed/entry or a ContractFolderStatus document. A feed with several
    tenders needs folder_id (cbc:ContractFolderID), otherwise ValueError. Each
    cac:ProcurementProjectLot gives cbc:ID (e.g. schemeName="ID_LOTE" "1"),
    cac:ProcurementProject/cbc:Name (+ cbc:Description if present), BudgetAmount
    (TaxExclusiveAmount, else EstimatedOverallContractAmount, else TotalAmount; currencyID) and
    RequiredCommodityClassification/cbc:ItemClassificationCode (CPV)."""
    folders = codice_folders(xml)
    if not folders:
        root = _parse_xml(xml)
        if root is None:
            return []
        folders = [(None, root)]
    if folder_id is not None:
        folders = [f for f in folders if f[0] == folder_id]
        if not folders:
            return []
    elif len(folders) > 1:
        raise ValueError(f"the feed has {len(folders)} contract folders "
                         f"({', '.join(str(f[0]) for f in folders)}); pass folder_id")
    root = folders[0][1]
    ns = CODICE_NS
    out = []
    for lot in root.iter(f"{{{ns['cac']}}}ProcurementProjectLot"):
        label = _text(lot.find("cbc:ID", ns))
        pp = lot.find("cac:ProcurementProject", ns)
        if pp is None:
            pp = ET.Element("empty")
        amount_el = None
        for tag in ("TaxExclusiveAmount", "EstimatedOverallContractAmount", "TotalAmount"):
            amount_el = pp.find(f"cac:BudgetAmount/cbc:{tag}", ns)
            if amount_el is not None and _text(amount_el):
                break
            amount_el = None
        value = _decimal(_text(amount_el)) if amount_el is not None else None
        cpv = _cpv(_text(e) for e in pp.findall("cac:RequiredCommodityClassification/cbc:ItemClassificationCode", ns))
        if label is None and _text(pp.find("cbc:Name", ns)) is None:
            continue
        out.append(_lot(label, _text(pp.find("cbc:Name", ns)), _text(pp.find("cbc:Description", ns)), cpv,
                        value, amount_el.get("currencyID") if amount_el is not None else None))
    return out


# ---------- (b) TED eForms UBL ----------
EFORMS_NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


def lots_from_eforms(xml, languages=("ENG",)) -> list[dict]:
    """TED eForms (ContractNotice / PriorInformationNotice / ContractAwardNotice). Only
    cac:ProcurementProjectLot whose cbc:ID has schemeName="Lot" (LOT-0001); groups of lots
    (LotsGroup, GLO-0001) and parts (Part) are skipped. The technical lot LOT-0000 of a procedure
    without lots is returned as one lot (ref "0"). Name/Description per languageID (first of
    `languages`, else the first one present); RequestedTenderTotal/EstimatedOverallContractAmount
    (currencyID); Main + Additional CommodityClassification CPV codes."""
    root = _parse_xml(xml)
    if root is None:
        return []
    ns = EFORMS_NS
    out = []
    for lot in root.findall("cac:ProcurementProjectLot", ns) or list(root.iter(f"{{{ns['cac']}}}ProcurementProjectLot")):
        id_el = lot.find("cbc:ID", ns)
        if id_el is None or (id_el.get("schemeName") or "Lot") != "Lot":
            continue
        pp = lot.find("cac:ProcurementProject", ns)
        if pp is None:
            pp = ET.Element("empty")
        amount_el = pp.find("cac:RequestedTenderTotal/cbc:EstimatedOverallContractAmount", ns)
        cpv = _cpv(_text(e) for path in ("cac:MainCommodityClassification/cbc:ItemClassificationCode",
                                         "cac:AdditionalCommodityClassification/cbc:ItemClassificationCode")
                   for e in pp.findall(path, ns) if (e.get("listName") or "cpv") == "cpv")
        out.append(_lot(_text(id_el), _first_lang(pp, "cbc:Name", ns, languages),
                        _first_lang(pp, "cbc:Description", ns, languages), cpv,
                        _decimal(_text(amount_el)) if amount_el is not None else None,
                        amount_el.get("currencyID") if amount_el is not None else None))
    return out


# ---------- (c) Find a Tender OCDS ----------

def lots_from_ocds(data) -> list[dict]:
    """OCDS release package ({"releases": [...]}), record package ({"records": [{"compiledRelease"}]})
    or a single release. Uses the LAST release that has tender.lots (packages list them in date
    order). Lot: id, title, description, value.amount/currency. CPV: tender.items[] with
    relatedLot == lot id (classification + additionalClassifications with scheme CPV)."""
    if data is None:
        return []
    if isinstance(data, (str, bytes, bytearray)):
        if not str(data if isinstance(data, str) else data.decode("utf-8", "replace")).strip():
            return []
        try:
            data = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return []
    if not isinstance(data, dict):
        raise TypeError("OCDS data must be a dict or a JSON string")
    releases = data.get("releases") or [r.get("compiledRelease") for r in data.get("records") or [] if isinstance(r, dict)]
    if not releases and isinstance(data.get("tender"), dict):
        releases = [data]
    tender = None
    for rel in releases:
        if isinstance(rel, dict) and isinstance(rel.get("tender"), dict) and rel["tender"].get("lots"):
            tender = rel["tender"]
    if tender is None:
        return []
    cpv_by_lot: dict[str, list[str]] = {}
    for item in tender.get("items") or []:
        if not isinstance(item, dict):
            continue
        rel_lot = item.get("relatedLot")
        for cl in [item.get("classification")] + list(item.get("additionalClassifications") or []):
            if isinstance(cl, dict) and str(cl.get("scheme", "")).upper() == "CPV" and rel_lot is not None:
                cpv_by_lot.setdefault(str(rel_lot), []).append(str(cl.get("id")))
    out = []
    for lot in tender["lots"]:
        if not isinstance(lot, dict) or lot.get("id") in (None, ""):
            continue
        value = lot.get("value") if isinstance(lot.get("value"), dict) else {}
        out.append(_lot(str(lot["id"]), lot.get("title"), lot.get("description"),
                        _cpv(cpv_by_lot.get(str(lot["id"]), [])), _decimal(value.get("amount")),
                        value.get("currency")))
    return out


def extract_lots(data, data_format: str) -> list[dict]:
    """Dispatch on source_platform.data_format. html/pdf/other need text: use a TextLotExtractor."""
    fn = {"codice": lots_from_codice, "eforms": lots_from_eforms, "ocds": lots_from_ocds}.get(data_format)
    if fn is None:
        raise ValueError(f"no structured lot parser for data_format {data_format!r}; "
                         "use HeuristicTextLotExtractor().extract(text) for document text")
    return fn(data)


# ---------- (d) free text: the extractor interface + a heuristic implementation ----------

class TextLotExtractor:
    """Interface for extracting lots from document text. Implementations return the normalised
    dict list with origin='ai_extracted' (a person confirms them). `name` is recorded in the
    lot_added event so you can tell which extractor guessed a lot."""
    name = "abstract"

    def extract(self, text: str) -> list[dict]:   # pragma: no cover - interface
        raise NotImplementedError


class HeuristicTextLotExtractor(TextLotExtractor):
    """Regex heuristics (NOT an AI model): finds lines starting with a lot heading such as
    'Lote 1: ...', 'Lot 2 - ...', 'Lot nº 3 ...', 'LOT 4 - ...', 'Lotto 5 ...', 'Los 6: ...',
    including the list that follows 'divided into the following lots' / 'dividido en los
    siguientes lotes'. The title is the rest of the heading line; following lines up to the next
    heading become the description while they are indented (a blank or unindented line ends it;
    first 1000 characters). A value like '120.000,00 EUR' or
    '€ 80,000' on the heading line or in its description is picked up. Mentions inside running text
    ('see lot 2 below') are ignored: the heading must start the line (after bullets/numbering)."""
    name = "heuristic-regex-v1"

    _HEAD = re.compile(
        r"^[\s\-\*\u2022\u25cf\u25aa>]*(?:\(?[ivx\d]+[\.\)]\s+)?"         # bullets / list numbers
        r"(?P<word>lotes?|lots?|lotto|los)\s*"
        r"(?:(?:n[º°o]?\.?|nr\.?|n[uú]m(?:ero|\.)?|number|#)\s*)?"
        r"(?P<num>\d{1,4}[a-z]?|[A-Z](?![a-z]))\b"
        r"\s*(?:[:\.\-\u2013\u2014\)]+\s*)?(?P<title>.*)$",
        re.IGNORECASE | re.MULTILINE)
    _INTRO = re.compile(r"(divided into (the following )?lots|dividid[oa] en (los siguientes )?lotes|"
                        r"divisé en (les )?lots|in lotti|in (folgende )?lose)", re.IGNORECASE)
    _MONEY = re.compile(
        r"(?:(?P<cur1>EUR|GBP|USD|€|£|\$)\s?(?P<amt1>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?))"
        r"|(?:(?P<amt2>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s?(?P<cur2>EUR|GBP|USD|€|£|euros?))",
        re.IGNORECASE)
    _CURRENCY = {"€": "EUR", "£": "GBP", "$": "USD", "EURO": "EUR", "EUROS": "EUR"}

    def extract(self, text: str) -> list[dict]:
        if text is None:
            return []
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text.strip():
            return []
        heads = list(self._HEAD.finditer(text))
        out, seen = [], set()
        for i, m in enumerate(heads):
            label = " ".join(text[m.start("word"):m.end("num")].split())   # raw heading label, e.g. "Lote nº 3"
            ref = canonical_lot_ref(label)
            if ref in seen:
                continue                    # the same lot mentioned again (e.g. a summary table)
            seen.add(ref)
            title = m.group("title").strip(" .:-\u2013\u2014") or None
            if title:   # the heading's first sentence ("Reactivos. Presupuesto: ..." -> "Reactivos")
                title = re.split(r"(?<=[a-z0-9\)])\.\s+(?=[A-ZÁÉÍÓÚÑ])", title, maxsplit=1)[0].rstrip(" .") or None
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            body_lines = []
            for line in text[m.end():end].splitlines()[1:]:     # [0] is the rest of the heading line
                if not line.strip() or not line[:1].isspace():
                    break                    # a blank or unindented line ends the lot's description
                body_lines.append(line.strip())
            body = " ".join(" ".join(body_lines).split())[:1000] or None
            value, currency = self._money(m.group("title") + " " + (body or ""))
            if title and len(title) > 300:
                title = title[:300].rsplit(" ", 1)[0]
            out.append(_lot(label, title, body, None, value, currency, origin="ai_extracted"))
        return out

    def _money(self, s: str):
        m = self._MONEY.search(s)
        if not m:
            return None, None
        amt = m.group("amt1") or m.group("amt2")
        cur = (m.group("cur1") or m.group("cur2")).upper()
        cur = self._CURRENCY.get(cur, cur)
        amt = amt.replace(" ", "")
        if re.search(r",\d{1,2}$", amt):           # 120.000,50 -> European decimal comma
            amt = amt.replace(".", "").replace(",", ".")
        else:
            amt = amt.replace(",", "")
            if re.search(r"\.\d{3}$", amt):         # 120.000 -> thousands separator
                amt = amt.replace(".", "")
        return _decimal(amt), cur if re.fullmatch(r"[A-Z]{3}", cur) else None

    def mentions_lots(self, text: str) -> bool:
        """True when the text says the tender is divided into lots (useful to flag a tender whose
        lots could not be extracted)."""
        return bool(text and self._INTRO.search(text))
