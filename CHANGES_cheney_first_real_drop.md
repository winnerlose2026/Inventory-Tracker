# Cheney Brothers first real feed drop — what the samples changed

*2026-08-04. Jairo Henao (Cheney software development director) sent the first
real sample files for the two feeds Walt Wilcox agreed to on 2026-07-06. Both
parsers were written against an agreed spec, not real data. The samples
contradicted the spec in three ways that would have corrupted tracker numbers,
and confirmed one open question. This is what changed and why.*

Samples live in the project folder, not the repo:
`vendor_feeds/cheney/2026-08-04_sample_drop/` — 9 order-guide CSVs (1,882 rows)
and 7 EDI 810 invoices. Trimmed copies are checked in under
`tests/fixtures/cheney/` as regression fixtures.

## 1. The "daily inventory CSV" is an order guide with no inventory in it

`HHBGELES_OrderGuide_sample.zip` is a **catalog + price file**, not the per-DC
on-hand snapshot we asked for. Across all 9 files and 1,882 rows:

| Expected (2026-07-06 agreement) | What arrived |
| --- | --- |
| on-hand cases | column present, `0` on **every row** |
| product description | absent — only an 8-char brand code (`SCHREIBE`) |
| one snapshot per DC per day | one file per **store account** |
| header row | none |

`integrations/cheney_csv_inventory.parse_inventory_csv` treated row 0 as a
header and required an on-hand column, so it already rejected these files — but
with an opaque "could not find the required columns", which invites someone to
"fix" it by forcing a column mapping. Forcing it would have written
`quantity=0` for every item at every warehouse and erased the tracker's real
counts.

Two guards now make that outcome unreachable:

1. **Shape detection.** `cheney_order_guide.looks_like_order_guide` recognises
   the headerless 7-column layout, and the on-hand parser refuses it by name
   with a pointer to the right parser. Verified against all 9 sample files, and
   verified NOT to fire on the existing headed example CSV.
2. **All-zero refusal.** Any on-hand CSV whose quantity column is zero or blank
   on *every* row is refused regardless of shape. A real count has stock
   somewhere. `allow_all_zero=True` exists for a deliberate zero-out.

New `integrations/cheney_order_guide.py` parses the file for what it actually
is — item #, brand, DC, pack, case cost, snapshot date, ship-to account — and
**emits no events at all**, by design. It also resolves all 12 H&H bagel
varieties from the existing item-number crosswalk and prices them at
$30.00/60 CT case, which the 810s independently confirm.

`scripts/sync_cheney_feeds.py` routes by shape, so if Cheney later adds a real
on-hand file to the same SFTP drop it flows through untouched. When a drop
contains *only* order guides, the bridge says so loudly rather than reporting a
clean run with zero updates.

## 2. EDI 810 money: TDS carries implied decimals (100x error)

The parser's module note flagged this as the thing to check against the first
real 810. It is real: `TDS*167619` is **$1,676.19**, not $167,619. `SAC05` is
the same (`SAC*C*D270***700` = a $7.00 delivery charge). `TXI` (tax) and
`CTP07` (extended cost) *do* carry explicit decimal points.

`_money()` now honours an explicit decimal point and applies implied
2-decimal scaling only when none is present — the correct X12 rule, and one
that keeps the existing synthetic-doc test (`TDS*1696.00`) passing unchanged.

With tax and charges captured, every one of the 7 real invoices now reconciles
to the penny:

    total (TDS) == sum(line extended) + sum(line tax) + sum(charges)

`parse_810` reports `reconciles` / `variance` per invoice, so a future
convention change on Cheney's side surfaces as a mismatch instead of quietly
corrupting spend.

## 3. EDI 810 quantities: catch-weight lines are pounds, not cases

Roughly a fifth of the sample lines are catch-weight (`TP*Y` — sliced cheese,
smoked salmon, deli meat, sausage). They are invoiced in **pounds**, and the
old parser put that pound figure straight into `cases`, overstating those lines
3–10x.

Converting needs both segments of the line:

    IT1*000030*40.000*LB*2.73*...*VU*5      VU = 5 lb per *unit*
    PO4*004*5*LB                            4 units to a case  ->  20 lb/case

so 40 lb is **2 cases**. Note `VU` is the per-unit weight, **not** the case
weight — dividing by `VU` alone overstates cases by the pack count. PO4's size
element is rounded to whole units (`008 2LB` for a 1.5 lb unit), so we take the
pack *count* from PO4 and the precise unit weight from `VU`. Cheney's own order
guide lists the matching pack (`008 1.5LB`), which is how the rounding was
caught.

Across all 11 catch-weight items in the drop this yields clean case counts
(164011: 40 lb → 2.0; 224012: 20 lb → 2.0; 10127838: 12 lb → 1.0). Remaining
fractional values are genuine average-weight cases — 238600 at 14.3 lb is one
case that came in under its 16 lb nominal.

Lines carry `qty`/`uom` (as invoiced), `cases` (true count), `case_weight`, and
`case_weight_estimated` when PO4 was missing. A weight line with no `VU` gets
`cases=None` and is named in the batch summary rather than being guessed at.

## 4. EDI 810: credit memos must subtract

`BIG07` carries the document type: `DI` = invoice, `CR` = credit memo. **2 of
the 7 samples are credit memos** (returns). The parser ignored `BIG07`, so a
return would have been added to a spend or case ledger as a delivery.

Invoices now expose `doc_type`, `is_credit` and `sign` (`-1` for credits), and
`summarize()` nets them out. Callers must apply `sign` before adding to any
ledger.

## Also picked up from the samples

- **Product descriptions.** The order guide has none, but the 810's `PID`
  segment does (`BAGEL ASIAGO PARBAKED`, `SALMON COLD SMOKED PRE-SLICED`).
  Captured per line — this is the only structured source of Cheney item
  descriptions we have.
- **DC and account crosswalks.** New `integrations/cheney_dcs.py`, derived from
  the 810s stating both sides where the CSVs carry only short codes. The CSV's
  DC column is the last two digits of Cheney's 3-digit DC number
  (`05` → `3005` Ocala).
- **A fourth DC.** Cheney serves H&H Chapel Hill out of **Statesville, NC**
  (`3012`), which the tracker doesn't model. It's marked known-but-untracked so
  those rows are flagged out-of-scope instead of having a warehouse invented
  for them. Chapel Hill's order guide carries no H&H bagel SKUs at all.
- **Crosswalk confirmed.** All 12 `CHENEY_ITEM_NO_TO_MFG` mappings match the
  `MG` (manufacturer product code) values on the real 810s. No changes needed.
- **Duplicate item rows are legitimate.** An item can appear twice in one order
  guide at two different case costs (140056 at $10.21 and $47.07) — Cheney
  prices some items at more than one pack/split. Rows are returned as-is;
  dedup is the caller's decision.

## Hardening from the review pass

An adversarial review of the first cut found several real problems, fixed here:

- **The "on-hand arrives later" claim wasn't true.** The most likely form of the
  fix Cheney owes us is the *same* headerless layout with the on-hand column
  filled in — and shape-based routing sent it to the order-guide parser, which
  emits nothing, while the log said "no on-hand data in this file". The decision
  is now made on *content*, not shape: an order-guide-layout file whose on-hand
  column is populated is converted to `on_hand` events
  (`cheney_order_guide.to_on_hand_events`), and only an all-zero one is refused.
  `case_size` comes from the pack string, so `001 60CT` yields 60 per case
  rather than 1.
- **Crash in the daily 810 report.** `ingest_cheney_810.py` formatted `qty` with
  `:g` unconditionally, so a single blank `IT102` on a non-`CA` line raised
  `TypeError` and killed the run before the batch summary printed. All
  nullable fields are now formatted defensively.
- **The no-PO4 fallback was silently wrong.** Assuming a 1-unit pack overstates
  cases by the real pack count — the exact error this change set out to fix.
  The estimate is kept (it beats nothing) but `summarize()` now reports
  `lines_with_estimated_pack` separately, so it can't pass as clean. Reworded
  the "all reconcile" line to say reconciliation is a *money* check that says
  nothing about case counts.
- **A good snapshot could be refused.** `_role_for_header` bound `qty` to the
  first header containing "on hand", so a feed with `Split Units On Hand`
  before `Cases On Hand` read splits as cases and then tripped the all-zero
  guard. Split/each/unit/weight headers no longer claim the `qty` role.
- **`allow_all_zero=True` was unreachable.** The refusal message told operators
  to do something no caller supported. `ingest_cheney_inventory_csv.py` now has
  `--allow-all-zero`, and the message points at it.
- **`normalize_dc_code` could return 5 characters.** `"012"` became `"30012"`
  instead of `"3012"`, silently turning Statesville into an unknown DC. It now
  strips leading zeros and returns 4 digits or `''` — and refuses genuinely
  ambiguous 3-digit input rather than guessing.
- **Mismatched pack UOM.** A 320 `OZ` line divided by a `004 5LB` pack gave a
  16x-wrong case count. Conversion now requires PO4's UOM to match the line's,
  otherwise `cases` is None and the line is surfaced.
- **A second PO4 could overwrite a correct pack count** (PO4 carries no line
  reference, so `lines[-1]` is the only anchor). Only the first PO4 per line is
  taken.
- **Credit direction is now magnitude-based**, so a credit stays negative even
  if Cheney starts stating credit amounts already-negative.
- **`_money`** tolerates thousands separators and X12 trailing signs, and
  rejects junk like `"1E3"` instead of reading it as `0.01`.
- **Fixtures**: all 7 real invoices are now checked in, and a test asserts every
  one reconciles with a confirmed case count on all 117 lines. Previously only
  2 were pinned, so the headline claim wasn't actually covered by CI.

Test count 172 → 184.

## Still open (not code)

1. **The on-hand feed does not exist yet.** This is the blocker: the tracker
   still has no daily inventory source from Cheney. Needs to go back to Jairo
   and Walt — either populate the on-hand column in the order-guide export, or
   send the separate per-DC snapshot originally agreed.
2. **Where the 810 lands in the tracker** — usage/case-movement vs. restock vs.
   a spend ledger. Field semantics are now settled, so this is purely a
   modelling call.
3. **SFTP landing** — unchanged and still the gating item for both feeds; see
   `RUNBOOK_cheney_data_feeds.md`.
