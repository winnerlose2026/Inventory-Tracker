#!/usr/bin/env python3
"""Parse a Cheney Brothers EDI 810 (invoice) file and summarize it.

Cheney (Walt Wilcox, 2026-07-06) will send daily EDI 810 invoices as our
shipment-history feed. This reads an 810 file, parses it with
integrations.edi_810, and prints a per-invoice summary (invoice #/date, PO #,
ship-from DC, line items with Cheney item #, cases, unit price, extended
cost). With --json it writes the parsed structure for inspection.

Ingest wiring (where this lands in the tracker -- usage/case-movement vs.
restock vs. a spend ledger) is intentionally deferred until we have Cheney's
first REAL 810 to confirm field semantics and the money-decimal convention
(see the note in integrations/edi_810.py). Until then this is parse-and-review.

    python scripts/ingest_cheney_810.py --edi /path/to/cheney_810.edi
    python scripts/ingest_cheney_810.py --edi file.edi --json parsed_810.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from integrations.edi_810 import parse_810  # noqa: E402


def _fmt_money(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--edi", required=True, help="Path to the EDI 810 file")
    p.add_argument("--json", dest="json_out", default="",
                   help="Optional: write parsed invoices to this JSON path")
    args = p.parse_args()

    path = Path(args.edi)
    if not path.exists():
        print(f"ERROR: EDI file not found: {path}", file=sys.stderr)
        return 2
    invoices = parse_810(path.read_text(encoding="utf-8", errors="replace"))
    print(f"Parsed {len(invoices)} invoice(s) from {path.name}\n")
    for v in invoices:
        print(f"Invoice {v['invoice_number'] or '—'}  date {v['invoice_date'] or '—'}  "
              f"PO {v['po_number'] or '—'}  ship-from {v['ship_from'] or '—'}")
        for l in v["lines"]:
            print(f"    item {l['item_no'] or '—':>10}  {str(l['cases']) if l['cases'] is not None else '—':>6} "
                  f"{l['uom'] or 'CA':<3} @ {_fmt_money(l['unit_price'])}  = {_fmt_money(l['extended'])}")
        print(f"    total (TDS): {_fmt_money(v['total'])}\n")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(invoices, indent=2))
        print(f"Wrote parsed JSON -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
