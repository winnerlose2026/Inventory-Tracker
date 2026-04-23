# Cheney Brothers PO PDF ingestion

**Date:** 2026-04-23
**Sample:** `PO # 4511694485.msg` from `NOREPLY@CHENEYBROTHERS.COM`

## What this adds

Cheney Brothers now submits purchase orders the same way US Foods does: an
email with a PDF attachment that lists the actual line items. This change
wires the Cheney branch into `email_scanner.py` so Cheney POs are parsed
into `restock` events exactly like USF POs.

## Files

### New
- `integrations/cheney_po_parser.py` — Parses Cheney PO PDFs into
  `CheneyPO` + `CheneyPOLine` dataclasses. Understands the Riviera Beach,
  Ocala, and Punta Gorda FL DC labels; maps Cheney's `Mfg#` column through
  `HH_MFG_CODE_TO_VARIETY` to our canonical variety name.
- `integrations/hh_mfg_codes.py` — Shared variety map. Cheney's `Mfg#`
  column and US Foods' `item #` column are both H&H's internal SKU codes
  (the same 1150/1152/1153/1155/1158/1159/1171/1184/1189 codes), so the
  lookup table lives here once and is imported by both parsers.

### Updated
- `integrations/usfoods_po_parser.py` — Now pulls variety map from
  `hh_mfg_codes`. `USF_ITEM_TO_VARIETY` kept as a backward-compat alias.
- `integrations/email_scanner.py` — PDF-attachment dispatch now routes by
  distributor:
    - `US Foods`        → `_usfoods_po_to_events` (unchanged)
    - `Cheney Brothers` → `_cheney_po_to_events` (new)
  Also updated the `parse_message_with_errors` docstring to note Cheney is
  a first-class branch alongside USF.
- `sync_inventory.py` — **Idempotency fix** in the PO revision-replace
  path (applies to Cheney). The duplicate-PO guard previously keyed on
  `existing_rev_int`, so a PO that doesn't expose a revision (Cheney)
  parsed as `rev_int == 0` and would bypass the "already seen" check on
  replay, double-booking the restock. The guard now keys on `active_idx`
  so any PO that's already been applied — revisioned or not — is skipped
  cleanly.

## PO number format

Cheney prints the PO as `01/4511694485` in the PDF. We preserve that as
`po_number="014511694485"` — leading zeros retained, slash stripped. No
revision field exists on this layout, so `po_revision=""`. Both are
intentional: the revision-replace logic treats two events with the same
PO number as duplicates regardless of whether one is a revision or a
replay.

## End-to-end verification

Against the real `PO # 4511694485.msg`:

```
Events:  4
  distributor='Cheney Brothers'  variety='Poppy Seed'          qty=24.0 cs  wh='Riviera Beach, FL'  po='014511694485'  rev=''  sku='1152'
  distributor='Cheney Brothers'  variety='Blueberry'           qty=32.0 cs  wh='Riviera Beach, FL'  po='014511694485'  rev=''  sku='1171'
  distributor='Cheney Brothers'  variety='Cinnamon Raisin'     qty=40.0 cs  wh='Riviera Beach, FL'  po='014511694485'  rev=''  sku='1155'
  distributor='Cheney Brothers'  variety='Everything'          qty=16.0 cs  wh='Riviera Beach, FL'  po='014511694485'  rev=''  sku='1158'

Errors:  0
ALL END-TO-END CHECKS PASSED
```

The regression harness at `test_po_revision.py` was extended with a
no-revision replay scenario (Cheney-style) that would have caught the
`active_idx` bug; all 6 scenarios pass.

## Operational notes

- **Adding a new Cheney DC:** extend `CHENEY_DC_CITY_TO_WAREHOUSE` in
  `cheney_po_parser.py`. The scanner surfaces unknown DCs as a non-fatal
  error instead of silently dropping.
- **Adding a new H&H bagel SKU:** add the mfg code + variety to
  `hh_mfg_codes.HH_MFG_CODE_TO_VARIETY`. Both the USF and Cheney parsers
  pick it up automatically.
- **Dependencies:** unchanged. `pypdf>=4.0` already in `requirements.txt`
  from the USF rollout.

## Case cost

H&H-set flat case costs by distributor (as of 2026-04-23):

- **Cheney Brothers: $26.50/case** — applied as a fallback for every line on
  every Cheney PO (Cheney's PDF has no cost column).
  Lives in `cheney_po_parser.CHENEY_CASE_COST`. Change it there if H&H
  renegotiates; all downstream events pick it up.
- **US Foods: $27.00/case** — read per-line from the PDF's NET COST column.
  Verified against sample PO 388549: every line prints $27.00. No flat
  override — if USF ever prices lines differently, the PDF is authoritative.

Both flow into `SyncItem.case_cost` on the restock event.
