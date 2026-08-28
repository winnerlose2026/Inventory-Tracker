#!/usr/bin/env python3
"""renormalize-varieties must be idempotent, and must not eat its own splits.

The assorted split writes four lines whose raw_variety is still "ASSORTED".
Re-resolving those lines INDIVIDUALLY maps every one of them back to
"Assorted", so a second renormalize pass collapsed the split it had just made
-- 96 lines on 2026-08-28, silently, with the case counts left intact so the
totals still looked right.

Derived lines are therefore re-derived as a GROUP from their shared original,
which is both idempotent and self-healing.
"""
import sys
sys.path.insert(0, ".")

import blueprints.production as prod
import inventory_tracker as it
from app import app
from integrations.production_pdf_parser import ASSORTED_SPLIT


def _rec(lines, **kw):
    r = {"po_number": "P1", "production_date": "2026-08-01", "lines": lines,
         "total_cases": sum(int(L.get("cs_count") or 0) for L in lines),
         "unmapped_varieties": []}
    r.update(kw)
    return r


def _run(monkeypatch, records, dry_run=False):
    saved = {}
    monkeypatch.setattr(it, "load_production", lambda: records)
    monkeypatch.setattr(it, "save_production", lambda d: saved.update(d=d))
    with app.test_request_context(json={"dry_run": dry_run}):
        resp = prod.api_admin_production_renormalize_varieties()
    return resp.get_json(), records


def _by_variety(rec):
    out = {}
    for L in rec["lines"]:
        out[L["variety"]] = out.get(L["variety"], 0) + L["cs_count"]
    return out


def test_assorted_expands_on_first_pass(monkeypatch):
    recs = [_rec([{"variety": "In-House Inventory", "raw_variety": "ASSORTED",
                   "cs_count": 24}])]
    out, recs = _run(monkeypatch, recs)
    assert out["assorted_lines_expanded"] == 1
    assert _by_variety(recs[0]) == {v: 6 for v in ASSORTED_SPLIT}


def test_second_pass_does_not_collapse_the_split(monkeypatch):
    """The regression: pass two used to rewrite all four back to Assorted."""
    recs = [_rec([{"variety": "In-House Inventory", "raw_variety": "ASSORTED",
                   "cs_count": 24}])]
    _run(monkeypatch, recs)
    first = _by_variety(recs[0])
    out2, recs = _run(monkeypatch, recs)
    assert _by_variety(recs[0]) == first
    assert "Assorted" not in _by_variety(recs[0])


def test_many_passes_are_stable(monkeypatch):
    recs = [_rec([{"variety": "In-House Inventory", "raw_variety": "ASSORTED",
                   "cs_count": 110}])]
    _run(monkeypatch, recs)
    snapshot = _by_variety(recs[0])
    for _ in range(4):
        _run(monkeypatch, recs)
        assert _by_variety(recs[0]) == snapshot
    assert sum(snapshot.values()) == 110


def test_a_collapsed_split_is_repaired(monkeypatch):
    """The live 2026-08-28 damage: variety clobbered to Assorted, counts
    intact. The group re-derive has to heal it, not just stop making it."""
    collapsed = [{"variety": "Assorted", "raw_variety": "ASSORTED",
                  "derived_from": "ASSORTED", "cs_count": c}
                 for c in (14, 14, 14, 14)]
    recs = [_rec(collapsed)]
    out, recs = _run(monkeypatch, recs)
    assert _by_variety(recs[0]) == {v: 14 for v in ASSORTED_SPLIT}
    assert sum(L["cs_count"] for L in recs[0]["lines"]) == 56


def test_repair_preserves_an_uneven_split(monkeypatch):
    collapsed = [{"variety": "Assorted", "raw_variety": "ASSORTED",
                  "derived_from": "ASSORTED", "cs_count": c}
                 for c in (28, 28, 27, 27)]
    recs = [_rec(collapsed)]
    _run(monkeypatch, recs)
    assert sum(L["cs_count"] for L in recs[0]["lines"]) == 110


def test_footer_row_is_dropped_and_total_recomputed(monkeypatch):
    recs = [_rec([{"variety": "Plain", "raw_variety": "PLAIN", "cs_count": 24},
                  {"variety": "In-House Inventory", "raw_variety": "TOTAL",
                   "cs_count": 24}])]
    out, recs = _run(monkeypatch, recs)
    assert out["footer_lines_dropped"] == 1
    assert [L["variety"] for L in recs[0]["lines"]] == ["Plain"]
    assert recs[0]["total_cases"] == 24


def test_a_sheets_own_total_survives_another_records_footer(monkeypatch):
    """dropped_footer_lines is a run-wide counter; it must not make an
    unrelated record's printed total get overwritten."""
    footer = _rec([{"variety": "In-House Inventory", "raw_variety": "TOTAL",
                    "cs_count": 10}])
    other = _rec([{"variety": "In-House Inventory", "raw_variety": "ASIAGO CHEESE",
                   "cs_count": 8}], total_cases=99)
    _run(monkeypatch, [footer, other])
    assert other["total_cases"] == 99


def test_stale_unmapped_flags_are_cleared(monkeypatch):
    """75 of 85 flags on 2026-08-28 were stale -- labels a later alias fixed,
    still advertised because the record itself never otherwise changed."""
    recs = [_rec([{"variety": "Whole Wheat", "raw_variety": "PARB-WHOLEWHEAT",
                   "cs_count": 8}], unmapped_varieties=["PARB-WHOLEWHEAT"])]
    out, recs = _run(monkeypatch, recs)
    assert recs[0]["unmapped_varieties"] == []


def test_a_genuinely_unmapped_label_keeps_its_flag(monkeypatch):
    recs = [_rec([{"variety": "In-House Inventory", "raw_variety": "ZQXWV BAGEL",
                   "cs_count": 8}])]
    out, recs = _run(monkeypatch, recs)
    assert recs[0]["unmapped_varieties"] == ["ZQXWV BAGEL"]
