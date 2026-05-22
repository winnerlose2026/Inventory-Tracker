# Chefs Warehouse PO ingestion

**Status:** implemented 2026-05-22

## What changed

Chefs Warehouse (CW) emails (sender domain `chefswarehouse.com`) carry their
POs as PDF attachments. Subjects look like `PO 1087421 FLA`, with a 2-3 letter
DC tag suffix (NY / MD / FLA / CHI).

A new parser (`integrations/chefs_warehouse_po_parser.py`) pulls every PO
line out of the PDF -- PO #, ship-to DC, order/delivery date, variety,
sliced/non-sliced, qty in cases, unit + ext cost, total USD. The scanner
routes CW emails to a parallel `cw_pos` channel on `ScanResult` so they
**never** touch `data/inventory.json`. CW POs live in
`data/chefs_warehouse_pos.json` and are surfaced on the Pending POs tab
via the new `/api/chefs-warehouse/pos` endpoint.

The Inventory tab stays CW-free by design -- this matches how the
operator thinks about CW (we sell TO them; they don't hold our stock).

## Pieces

- `integrations/chefs_warehouse_po_parser.py`: PDF parser. Maps
  CW ship-to id 200001 -> Bronx NY, 400001 -> Hanover MD (covers both
  MD and CHI -- CHI POs ship to Hanover and transfer), 600001 ->
  Opa Locka FL. `CW_DESCRIPTION_TO_VARIETY` maps the BAGELS HH <variety>
  [SLICED] strings to canonical varieties.

- `integrations/email_scanner.py`: classifies `chefswarehouse.com`
  senders, parses the PDF, emits a dict record into a new
  `ScanResult.cw_pos` list. `parse_message_with_errors` now returns
  `(events, errors, cw_pos)`.

- `inventory_tracker.py`: `load_chefs_warehouse_pos` /
  `save_chefs_warehouse_pos` helpers + `CHEFS_WAREHOUSE_POS_FILE`
  pointing at `data/chefs_warehouse_pos.json`.

- `sync_inventory.py`: `_apply_cw_pos` writes records to the CW data
  file, idempotent on `po_number`. Preserves operator-set `ship_date`
  and `arrival_date` on re-ingest. Skips POs in the shared
  canceled-POs ignore list.

- `app.py`: four new endpoints --
  `GET /api/chefs-warehouse/pos`,
  `POST /api/chefs-warehouse/ingest-pos`,
  `POST /api/chefs-warehouse/ship-date`,
  `POST /api/chefs-warehouse/cancel`.
  `WAREHOUSES` gains a `Chefs Warehouse` entry with the 3 DCs.
  `/api/traceability/search` extends its `po_pending` lookup to check
  the CW file too, so Daily Production sheets tagged with a CW PO#
  resolve to "pending" status with the correct ship / arrival dates.

- `scripts/cowork_graph_scan.py`: classifies CW senders, parses CW
  PDFs, POSTs the records to `/api/chefs-warehouse/ingest-pos`
  alongside the existing USF/Cheney events POST.

- `templates/index.html`: `loadPendingPOs` fetches CW POs from the new
  endpoint and merges them into the render groups. CW rows annotate
  the warehouse with the DC tag in parentheses (e.g. "Hanover, MD
  (CHI)"). Ship-date and cancel buttons route to the CW API endpoints
  when the row's source is `chefs_warehouse`.

## Verified

All 6 sample POs uploaded by JD parse cleanly:

| PO #     | DC  | Warehouse      | Lines | Cases | Total USD |
|----------|-----|----------------|-------|-------|-----------|
| 1068886  | MD  | Hanover, MD    | 4     | 112   | $3,469.44 |
| 1087421  | FLA | Opa Locka, FL  | 1     | 112   | $3,304.00 |
| 1087448  | NY  | Bronx, NY      | 2     | 168   | $4,956.00 |
| 1095389  | MD  | Hanover, MD    | 3     | 112   | $3,304.00 |
| 1100224  | NY  | Bronx, NY      | 2     | 168   | $4,956.00 |
| 1113603  | CHI | Hanover, MD    | 4     | 112   | $3,391.08 |

Every variety (Plain, Plain Sliced, Sesame, Cinnamon Raisin, Everything,
Everything Sliced) resolves correctly. The CHI PO correctly identifies
Hanover MD as the actual ship-to (CW transfers from Mid-Atlantic to
Chicago).
