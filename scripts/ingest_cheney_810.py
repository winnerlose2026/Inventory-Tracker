#!/usr/bin/env python3
"""Parse Cheney Brothers EDI 810 (invoice) files and summarize them.

Cheney (Walt Wilcox, 2026-07-06) sends daily EDI 810 invoices as our
shipment-history feed. This reads one or more 810 files, parses them with
integrations.edi_810, and prints a per-invoice summary (invoice #/date, PO #,
ship-from DC, ship-to store, line items, money reconciliation) plus a batch
roll-up with credits netted out.

Money + quantity semantics were CONFIRMED 2026-08-04 against Cheney's first
real drop -- implied-decimal TDS, catch-weight LB lines, CR credit memos; see
integrations/edi_810.py. Each invoice is checked against its own TDS total and
any mismatch is called out, so a convention change on Cheney's side surfaces
here instead of quietly corrupting numbers.

What still isn't decided is WHERE the 810 lands in the tracker
(usage/case-movement vs. restock vs. a spend ledger) -- that's a modelling
call, so this stays parse-and-review.

    python scripts/ingest_cheney_810.py --edi /path/to/cheney_810.edi
    python scripts/ingest_cheney_810.py --edi dir_or_glob --json parsed_810.json
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from integrations.edi_810 import parse_810, summarize  # noqa: E402

_EDI_SUFFIXES = (".edi", ".810", ".x12", ".txt")


def _fmt_money(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"


def _resolve(spec: str) -> list[Path]:
    """A file, a directory of 810s, or a glob -> sorted list of files."""
    p = Path(spec)
    if p.is_dir():
        return sorted(f for f in p.iterdir()
                      if f.is_file() and f.suffix.lower() in _EDI_SUFFIXES)
    if p.exists():
        return [p]
    return sorted(Path(m) for m in globmod.glob(spec) if Path(m).is_file())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--edi", required=True,
                   help="EDI 810 file, a directory of them, or a glob")
    p.add_argument("--json", dest="json_out", default="",
                   help="Optional: write parsed invoices to this JSON path")
    args = p.parse_args()

    paths = _resolve(args.edi)
    if not paths:
        print(f"ERROR: no EDI file(s) matched: {args.edi}", file=sys.stderr)
        return 2

    invoices: list[dict] = []
    for path in paths:
        invoices.extend(parse_810(path.read_text(encoding="utf-8", errors="replace")))
    print(f"Parsed {len(invoices)} invoice(s) from {len(paths)} file(s)\n")

    for v in invoices:
        kind = "CREDIT MEMO" if v["is_credit"] else "invoice"
        print(f"{kind} {v['invoice_number'] or '—'}  date {v['invoice_date'] or '—'}  "
              f"PO {v['po_number'] or '—'}")
        print(f"    {v['ship_from'] or '—'} (DC {v['ship_from_code'] or '?'})"
              f"  ->  {v['ship_to'] or '—'} (acct {v['ship_to_account'] or '?'})")
        for l in v["lines"]:
            cases = f"{l['cases']:g}" if l["cases"] is not None else "?"
            note = ""
            if l["uom"] not in ("CA", ""):
                # Every field here can legitimately be None (blank IT102, no
                # VU, no PO4), so format defensively -- this print must never
                # be what kills the daily run.
                qty = f"{l['qty']:g}" if l["qty"] is not None else "?"
                cw = f"{l['case_weight']:g}" if l["case_weight"] is not None else "?"
                note = f"  [{qty} {l['uom']} @ {cw}/cs]"
                if l["case_weight_estimated"]:
                    note += " (pack ASSUMED 1/case -- cases may be high)"
            print(f"    item {l['item_no'] or '—':>10}  {cases:>7} cs "
                  f"@ {_fmt_money(l['unit_price'])} = {_fmt_money(l['extended'])}"
                  f"  {(l['description'] or '')[:34]}{note}")
        verdict = ("reconciles" if v["reconciles"]
                   else f"MISMATCH off by {_fmt_money(v['variance'])}")
        print(f"    subtotal {_fmt_money(v['subtotal'])}"
              f"  tax {_fmt_money(v['tax'])}"
              f"  charges {_fmt_money(v['charges'])}"
              f"  ->  TDS {_fmt_money(v['total'])}  [{verdict}]")
        print()

    s = summarize(invoices)
    print("Batch summary")
    print(f"    invoices {s['invoices']} ({s['credits']} credit memo(s)), "
          f"{s['lines']} line(s)")
    print(f"    net total {_fmt_money(s['net_total'])}   net cases {s['net_cases']:g}")
    problems = ("unreconciled", "lines_without_cases",
                "lines_with_estimated_pack", "line_count_mismatch",
                "unit_count_mismatch")
    for label, key in (("did NOT reconcile", "unreconciled"),
                       ("no case count", "lines_without_cases"),
                       ("pack size assumed", "lines_with_estimated_pack"),
                       ("CTT line-count mismatch", "line_count_mismatch"),
                       ("ISS unit-count mismatch", "unit_count_mismatch")):
        if s[key]:
            print(f"    ! {label}: {', '.join(str(x) for x in s[key])}")
    if not any(s[k] for k in problems):
        print("    all invoices reconcile against their own TDS totals, "
              "and every line has a confirmed case count")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(invoices, indent=2))
        print(f"\nWrote parsed JSON -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
