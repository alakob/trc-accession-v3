# Test fixtures: SYNTHETIC SAMPLE DATA

Every file here was written by hand for the tests. None of it was downloaded from a platform.
Buyers, references, amounts and texts are invented.

The XML/JSON structure follows the public specifications, so the parsers see realistic shapes:

| File | Modelled on |
|---|---|
| `placsp_codice_sample.atom` | PLACSP syndication (ATOM + CODICE `cac-place-ext:ContractFolderStatus`, namespaces `urn:dgpe:names:draft:codice...`; lots in `cac:ProcurementProjectLot`, `cbc:ID schemeName="ID_LOTE"`, `cac:BudgetAmount`, `cac:RequiredCommodityClassification`) |
| `ted_eforms_sample.xml` | TED eForms UBL ContractNotice (eForms SDK examples, e.g. `cn_24_maximal.xml`): `cac:ProcurementProjectLot` with `cbc:ID schemeName="Lot"` (LOT-0001) and a `LotsGroup` (GLO-0001), multilingual `cbc:Name`, `RequestedTenderTotal/EstimatedOverallContractAmount`, Main/Additional `CommodityClassification` |
| `fts_ocds_sample.json` | Find a Tender OCDS release package (ocid prefix `ocds-h6vhtk`, lots extension: `tender.lots[]`, `tender.items[].relatedLot`) with two releases, where the newer one adds a lot |
| `tender_document_es.txt`, `tender_document_en.txt` | Plain text of tender documents (pliego / ITT) for the heuristic text extractor |
| `platforms_input_messy.csv` | A platform list with odd headers, `;` delimiter, a duplicate row and bad values |
