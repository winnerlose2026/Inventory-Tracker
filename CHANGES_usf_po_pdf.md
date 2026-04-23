# US Foods PO PDF ingestion — changes

Closes the ingestion gap where real US Foods POs (which arrive as PDF
attachments from `NORTHEASTCONFIRMATIONS.SHARED@USFOODS.COM`) were
silently bypassing the scanner — `email_scanner.py` only understood CSV
attachments and structured body text.

## Files

### New: `integrations/usfoods_po_parser.py`
Self-contained parser for the US Foods PO PDF layout. Depends on `pypdf`.
Exports:

- `parse_po_pdf(pdf_bytes: bytes) -> UsFoodsPO` — main entry point.
- `parse_po_text(text: str) -> UsFoodsPO` — same parser over already-extracted
  text; handy for unit tests without a PDF dependency.
- `UsFoodsPO` — dataclass with PO #, revision, order/cancel/arrive dates,
  vendor #, buyer, ship-to city/state/zip, canonical `warehouse`
  (`"Manassas, VA"` etc.), plus `lines: list[UsFoodsPOLine]` and
  `unmapped_items` for diagnostics.
- `UsFoodsPOLine` — per-line: USF item #, qty, unit, description, list/net
  cost, SCC/GTIN, pack string (`"6/10/4.06"`), mfr prod #, mapped variety,
  derived `case_size` (units per case).
- `USF_ITEM_TO_VARIETY` — the 7 item codes observed on PO 3885495O:

      1150 Plain             1152 Poppy Seed       1153 Sesame
      1158 Everything        1159 Asiago           1184 Egg
      1189 Jalapeno Cheddar

  Per JD: the "cheese wheat" SKUs (1159, 1189) map onto the existing
  Asiago and Jalapeño Cheddar varieties rather than creating new
  wheat-variant SKUs. Poppy Seed (1152) is mapped here even though it
  isn't in the current 11-variety seed list — inventory-match will fail
  until a Poppy Seed SKU is added, which is the correct failure mode
  (visible, not silent).

- `USF_DC_CITY_TO_WAREHOUSE` — ship-to city (UPPERCASE) → canonical
  `"<City>, <ST>"`. Seeded with the 5 USF DCs in `seed_bagels.py`.

### Modified: `integrations/email_scanner.py`
Two changes:

1. **New attachment branch — USF PO PDF.** When the sender resolves to
   `"US Foods"` via `_distributor_from_sender`, `.pdf` attachments are
   routed through `parse_po_pdf` and each `UsFoodsPOLine` becomes a
   `SyncItem` wrapped in an `EmailEvent(event_type="restock")`. Fields
   populated: `quantity`, `distributor`, `variety`, `warehouse`, `unit`
   (lowercased, `"cases"`), `case_cost` (net), `case_size`,
   `distributor_sku` (USF item #). PDFs from non-USF senders are left
   untouched.

2. **New public function — `parse_message_with_errors(msg)`** returns
   `(events, errors)` so the scan loops can surface unmapped USF item #s
   and unknown ship-to DCs on `ScanResult.errors` instead of silently
   dropping the line. `parse_message(msg)` is preserved as a thin wrapper
   for backwards compatibility. All three scan loops (`_scan_ms365`,
   `_scan_imap`, `_scan_dumps`) now use the new function.

Precedence inside `parse_message_with_errors`:

    1. USF PO PDF attachments     -> restock events
    2. CSV attachments            -> event type inferred from filename
    3. Structured body (tag lines) -> fallback if no attachments produced events

### Modified: `requirements.txt`
Adds `pypdf>=4.0`.

## Verification

End-to-end with the real `.msg` JD uploaded (PO 3885495O, Manassas DC,
April 2026):

    Events: 7
    Errors: []
    Event types: Counter({'restock': 7})

    idx type    qty variety           warehouse    unit  sku  case$ csize
    0   restock   8 Egg               Manassas, VA cases 1184 27.00    60
    1   restock   8 Asiago            Manassas, VA cases 1159 27.00    60
    2   restock  16 Jalapeno Cheddar  Manassas, VA cases 1189 27.00    60
    3   restock   8 Poppy Seed        Manassas, VA cases 1152 27.00    60
    4   restock  16 Sesame            Manassas, VA cases 1153 27.00    60
    5   restock  24 Plain             Manassas, VA cases 1150 27.00    60
    6   restock  32 Everything        Manassas, VA cases 1158 27.00    60

    Total: 112 cases  (6,720 bagels)

## Follow-ups (not in this change)

- ~~**Add a Poppy Seed SKU** to `seed_bagels.py`.~~ **Done** — see
  `CHANGES_poppy_seed_sku.md`. Total seeded SKUs went from 88 → 96.
- **Fill in the remaining DC addresses** in the USF account reference
  memory (Zebulon NC, La Mirada CA, Chicago IL, Alcoa TN) as POs for
  those DCs come in.
- **Subject-line vendor cross-check** — subject encodes
  `B225 C064 V150345`, which can validate that the parsed vendor #
  matches before writing the events. Nice-to-have, not required.
- **Revision handling** — PO `3885495O` rev `0000002` suggests USF
  revises POs in place. Decide whether a later revision should replace
  earlier restock events for the same PO or add to them. Current behavior
  is additive.
