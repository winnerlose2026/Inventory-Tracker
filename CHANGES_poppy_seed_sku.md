# Add Poppy Seed variety — changes

Closes the last remaining gap from `CHANGES_usf_po_pdf.md`: the USF parser
already maps item #1152 to "Poppy Seed", but there was no Poppy Seed SKU in
the seed list, so restock events for 1152 would land as unmatched. Adding
the variety populates 8 new SKUs (one per DC) and puts the total at 96.

## File

### Modified: `seed_bagels.py`

1. **New variety row in `VARIETIES`** (placed between Sesame and
   Cinnamon Raisin since it's another seed-topped bagel):

       ("Poppy Seed",               40,  72, 36),

   Parameters copied from Sesame as a reasonable first cut:
     - 40 bagels/week weekly_usage
     - 72 base qty per DC (scaled by each DC's stock multiplier)
     - 36 bagels low-stock threshold

   **Flag for JD:** if Poppy Seed actually moves faster/slower than
   Sesame, adjust these three numbers — the rest of the pipeline
   doesn't care what they are, but reorder planning and days-of-supply
   both read from them.

2. **Docstring updates:**
   - Variety list now reads "...sesame, poppy seed, cinnamon raisin..."
   - "11 varieties × 8 warehouses = 88 SKUs" → "12 varieties x 8 warehouses = 96 SKUs"

No other code changes: `_build_bagels()` already iterates `VARIETIES`, and
the print block uses `len(VARIETIES)` so SKU counts update automatically.

## Verification

Dry-run of the seed builder (stubbing `inventory_tracker.add_item` so no
DB writes happen):

    Varieties: 12
    Total SKUs built: 96

    SKUs per variety:
      Plain                      8
      Everything                 8
      Sesame                     8
      Poppy Seed                 8
      Cinnamon Raisin            8
      Whole Wheat                8
      Whole Wheat Everything     8
      Blueberry                  8
      Egg                        8
      Onion                      8
      Asiago                     8
      Jalapeno Cheddar           8

    Poppy Seed SKUs (8):
      Poppy Seed Bagel 4oz [CB - Riviera Beach]     qty=72   Riviera Beach, FL
      Poppy Seed Bagel 4oz [CB - Ocala]             qty=86   Ocala, FL
      Poppy Seed Bagel 4oz [CB - Punta Gorda]       qty=50   Punta Gorda, FL
      Poppy Seed Bagel 4oz [USF - Manassas]         qty=72   Manassas, VA
      Poppy Seed Bagel 4oz [USF - Zebulon]          qty=65   Zebulon, NC
      Poppy Seed Bagel 4oz [USF - La Mirada]        qty=79   La Mirada, CA
      Poppy Seed Bagel 4oz [USF - Chicago]          qty=94   Chicago, IL
      Poppy Seed Bagel 4oz [USF - Alcoa]            qty=58   Alcoa, TN

## End-to-end check

Cross-referencing the USF parser's `USF_ITEM_TO_VARIETY` + `USF_DC_CITY_TO_WAREHOUSE`
against the 96 seeded SKUs: every (USF item #, USF DC) pair now resolves
to a matching SKU, including the previously-failing case:

    USF item #1152 + MANASSAS DC
      -> variety "Poppy Seed"
      -> warehouse "Manassas, VA"
      -> SKU "Poppy Seed Bagel 4oz [USF - Manassas]"  ✓ present

Result: all 7 restock events from PO 3885495O will now match seeded SKUs
cleanly, with zero unmapped items.

## Deployment

To apply to an existing inventory:

    python seed_bagels.py          # adds only the 8 new Poppy Seed SKUs
                                    # (existing 88 are skipped as already present)

`--reset` is **not** recommended — it wipes on-hand quantities for the
existing 88 SKUs. The default (additive) run is safe.
