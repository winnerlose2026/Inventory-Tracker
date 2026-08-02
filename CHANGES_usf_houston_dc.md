# US Foods NW Houston (DC B2) — PO ingestion + warehouse

**Date:** 2026-08-02
**Trigger:** PO `305202B2` (rev 0000003), 1120 cs / 20 pallets, arrive 08/31/26.
Sent by Matthew Greene 2026-07-30, revised the same day by Tom Foley (area buyer).

## The bug

The Houston PO PDF **parsed perfectly** — 12 lines, every item code mapped —
and then every line was thrown away.

`_usfoods_po_to_events` drops any line whose DC can't be resolved:

```python
for line in po.lines:
    if not line.variety or not po.warehouse:
        continue
```

`USF_DC_CITY_TO_WAREHOUSE` had five DCs and no `HOUSTON`, so `po.warehouse`
was `None` and all 1120 cases vanished. The cron logged the error and
**still exited 0**:

```
2026-07-30 18:01  ingest-events OK: 2 parsed, 0 events, ... 4 errors; updated=0
2026-07-31 00:01  ingest-events OK: 3 parsed, 0 events, ... 5 errors; updated=0
2026-07-31 06:01  ingest-events OK: 3 parsed, 0 events, ... 5 errors; updated=0
```

Five consecutive runs read "OK". Houston `on_order` sat at zero for two days
and nothing surfaced outside the Render log tail.

## Changes

### `integrations/usfoods_po_parser.py`
- `USF_DC_CITY_TO_WAREHOUSE`: added `"HOUSTON": "Houston, TX"`.
- `_BUYER_RE`: the Houston layout prints the REMARKS column on the same line
  as BUYER (`BUYER: 152 T X FOLEY        INFO@HHBAGELS.COM`). The old greedy
  `[A-Z \.]+` captured `"T X FOLEY ... INFO"` as the buyer name. Now stops at
  the 2+ space column gutter. Manassas/La Mirada layouts unaffected.

### `seed_bagels.py`
- Added `("Houston, TX", "Houston", 1.0)` to the US Foods warehouse list.
  12 varieties × 9 warehouses = **108 SKUs** (was 96). SKU names follow the
  existing convention — `Plain Bagel 4oz [USF - Houston]` — which is exactly
  what `sync_inventory._candidate_names()` builds from
  `warehouse="Houston, TX"`, so restock events match with no mapping shim.

### `test_usfoods_houston_po.py` (new)
Covers the DC mapping, warehouse resolution and the buyer-column fix, plus an
invariant that catches this whole class of bug on any future DC:

```python
def test_every_seeded_usf_warehouse_has_a_dc_city():
    seeded == set(USF_DC_CITY_TO_WAREHOUSE.values())
```

Seed a DC without teaching the parser its city (or vice versa) and the suite
fails, instead of the POs disappearing.

## Verification

The real `.msg` run through `parse_message_with_errors`:

```
events: 12 | errors: []

variety                 wh               qty  unit   sku    case$  csize
Egg                     Houston, TX       56  cases  1184   27.00     60
Blueberry               Houston, TX       56  cases  1171   27.00     60
Asiago                  Houston, TX       88  cases  1159   27.00     60
Jalapeno Cheddar        Houston, TX      120  cases  1189   27.00     60
Whole Wheat Everything  Houston, TX       56  cases  1157   27.00     60
Whole Wheat             Houston, TX       56  cases  1156   27.00     60
Poppy Seed              Houston, TX       56  cases  1152   27.00     60
Cinnamon Raisin         Houston, TX       88  cases  1155   27.00     60
Sesame                  Houston, TX      120  cases  1153   27.00     60
Plain                   Houston, TX      168  cases  1150   27.00     60
Everything              Houston, TX      192  cases  1158   27.00     60
Onion                   Houston, TX       64  cases  1151   27.00     60

PO 305202B2 rev 0000003 order 2026-07-29
TOTAL: 1120 cases = 67,200 bagels
```

Matches Tom's revised total exactly (he cut item 1150 from 176 → 168 cs to
land on 20 pallets @ 56 cs). Full suite: **92 passed**.

## Deploy steps

1. Push to `main` — the **cron** (`bagel-inventory-6h-scan`) auto-deploys on
   commit, so PO parsing is fixed on the next 6-hourly run.
2. The **web service** (`bagel-inventory`) has `autoDeploy: no` — trigger a
   manual deploy so `seed_bagels.py` ships.
3. `POST /api/seed` (header `X-Inventory-Token`) to create the 12 Houston
   SKUs. `seed()` skips existing items, so it's safe to re-run and won't
   touch the other 96.
4. Re-run the scan to pick up PO 305202B2 from the inbox. The apply path is
   idempotent (PO-revision guard in `sync_inventory`), so a replay can't
   double-count.

## US Foods PO routing (from the 2026-08 audit)

Which confirmations alias serves which DC. Subject format is
`USF PO <po#> <DC>/<code> <mm/dd/yy> B<buyer> C<cost> V150345 H&H BAGELS`.
All of them are `@usfoods.com`, so `_distributor_from_sender` already routes
every one of these — the DC comes off the PDF ship-to block, not the sender.

| DC code | Warehouse       | Confirmations alias                    |
|---------|-----------------|----------------------------------------|
| `5O/2125` | Manassas, VA    | `NORTHEASTCONFIRMATIONS.SHARED`        |
| `4C/4120` | La Mirada, CA   | `WESTCONFIRMATIONS.SHARED`             |
| `3T/2088` | Chicago, IL     | `CENTRALCONFIRMATIONS.SHARED`          |
| `5G/2240` | Zebulon, NC     | `SOUTHEASTCONFIRMATIONS.SHARED`        |
| `6H/2270` | Alcoa, TN       | `SOUTHEASTCONFIRMATIONS.SHARED`        |
| `B2`      | Houston, TX     | `CENTRALCONFIRMATIONS.SHARED`          |

`NoReply@usfoods.com` also sends `Confirm USF PO ...` nag mail with **no
attachment** — those carry no line items and are correctly ignored.

## Deliberately NOT done

**No Houston sender was added to `REPORT_SENDER_TO_WAREHOUSE`.** Houston has
no weekly on-hand report cadence yet, and a sender in that map bypasses
`_is_report_shaped`'s subject filtering entirely — so pre-registering Foley /
Youngblood / Greene would push all their ordinary correspondence into the
unparsed-report queue. That's the "rep chatter faking parser gaps" regression
fixed in `df3be07`; `test_scan_health.py` asserts Greene's `RE: Bagels` is not
report-shaped, and it caught the attempt when it was tried here.

When Houston starts sending a weekly report, register whoever actually sends
it — preferably via `data/rep_warehouse_map.json` (see `integrations/rep_map.py`)
so it needs no deploy. `report_status.WAREHOUSE_REPS` was left alone for the
same reason: adding Houston now would show it permanently overdue for a report
it has never agreed to send.

## Open follow-up: POs that parse to zero events are still silent

The Houston fix closes this instance, and the new invariant test stops it
recurring for a *seeded* DC. But the general failure mode is still quiet: any
PO PDF that parses and emits no events (new DC, new item code) logs an error
and the cron exits 0. Proposed fix is to collect those in
`scripts/cowork_graph_scan.py` and exit non-zero so Render's cron-failure
notification fires. Held pending a decision, because it makes the cron go red
and send mail. Everything parsed successfully is applied before the exit and
the apply path is idempotent, so failing the run would lose nothing.
