"""Tests for the X12 EDI 810 invoice parser. Standalone or pytest."""
import sys
sys.path.insert(0, ".")
from integrations.edi_810 import parse_810, _iso_date, _num


def _isa(elem, comp, seg):
    """Build a spec-width 106-char ISA so delimiter auto-detection is exercised."""
    f = ["00", " " * 10, "00", " " * 10, "ZZ", "SENDER".ljust(15), "ZZ",
         "RECEIVER".ljust(15), "260706", "1200", "U", "00401", "000000001", "0", "P"]
    isa = "ISA" + elem + elem.join(f) + elem + comp + seg
    assert len(isa) == 106, len(isa)
    return isa


def _doc(elem="*", comp=">", seg="~", with_isa=False):
    segs = [
        "ST" + elem + "810" + elem + "0001",
        elem.join(["BIG", "20260706", "INV12345", "20260701", "4511715932"]),
        elem.join(["N1", "SF", "CHENEY BROTHERS RIVIERA BEACH", "92", "RVB"]),
        elem.join(["IT1", "1", "40", "CA", "26.50", "", "VN", "100001"]),
        elem.join(["IT1", "2", "24", "CA", "26.50", "", "VN", "100002"]),
        elem.join(["TDS", "1696.00"]),
        elem.join(["SE", "7", "0001"]),
    ]
    body = seg.join(segs) + seg
    return (_isa(elem, comp, seg) + seg + body) if with_isa else body


def test_parse_default_delimiters():
    inv = parse_810(_doc())
    assert len(inv) == 1
    v = inv[0]
    assert v["invoice_number"] == "INV12345"
    assert v["invoice_date"] == "2026-07-06"
    assert v["po_number"] == "4511715932"
    assert v["po_date"] == "2026-07-01"
    assert v["ship_from"] == "CHENEY BROTHERS RIVIERA BEACH"
    assert len(v["lines"]) == 2
    l0 = v["lines"][0]
    assert l0["item_no"] == "100001"
    assert l0["cases"] == 40
    assert l0["unit_price"] == 26.50
    assert l0["extended"] == 1060.00
    assert v["total"] == 1696.00
    print("ok: default-delimiter 810 parses invoice + lines + extended")


def test_parse_custom_delimiters_via_isa():
    # pipe elements, caret components, apostrophe segment terminator
    inv = parse_810(_doc(elem="|", comp="^", seg="'", with_isa=True))
    assert len(inv) == 1, inv
    v = inv[0]
    assert v["invoice_number"] == "INV12345"
    assert v["po_number"] == "4511715932"
    assert [l["item_no"] for l in v["lines"]] == ["100001", "100002"]
    assert v["lines"][1]["extended"] == round(24 * 26.50, 2)
    print("ok: ISA-declared custom delimiters detected + parsed")


def test_multiple_invoices_and_helpers():
    two = _doc() + _doc()  # two ST..SE transactions back to back
    assert len(parse_810(two)) == 2
    assert _iso_date("20260706") == "2026-07-06"
    assert _iso_date("") == ""
    assert _num("26.50") == 26.50 and _num("") is None
    assert parse_810("") == []
    print("ok: multiple invoices + date/num helpers")


# --------------------------------------------------------------------------
# Regressions against Cheney's REAL 2026-08-04 sample drop. These pin the
# money and quantity conventions the synthetic doc above can't express.
# --------------------------------------------------------------------------
from pathlib import Path  # noqa: E402
from integrations.edi_810 import summarize, _money  # noqa: E402

FIX = Path(__file__).parent / "tests" / "fixtures" / "cheney"
INVOICE = FIX / "CheneyInvoices_9100257242_20260803012916_301220260803-012919-470.EDI"
CREDIT = FIX / "CheneyInvoices_9800024285_20260803012916_300620260803-012919-238.EDI"


def test_money_implied_vs_explicit_decimals():
    # No decimal point -> X12 implied 2 decimals (Cheney's TDS and SAC05).
    assert _money("167619") == 1676.19
    assert _money("700") == 7.00
    assert _money("8934") == 89.34
    # An explicit decimal point is authoritative (Cheney's TXI and CTP07).
    assert _money("6.24") == 6.24
    assert _money("1696.00") == 1696.00
    assert _money("-450") == -4.50
    assert _money("") is None and _money("x") is None
    print("ok: implied-decimal money rule (no '.' -> /100, '.' -> face value)")


def test_real_invoice_reconciles_to_the_penny():
    inv = parse_810(INVOICE.read_text())
    assert len(inv) == 1
    v = inv[0]
    assert v["invoice_number"] == "9100257242"
    assert v["invoice_date"] == "2026-07-31"
    assert v["po_number"] == "WP0002652226"
    assert v["doc_type"] == "DI" and v["is_credit"] is False and v["sign"] == 1
    assert v["ship_from"] == "STATESVILLE" and v["ship_from_code"] == "3012"
    assert v["ship_to"] == "H&H BAGELS CHAPEL HILL"
    assert v["ship_to_account"] == "60415887"
    # TDS*74040 is $740.40, NOT $74,040 -- the 100x bug this pins shut.
    assert v["total"] == 740.40, v["total"]
    assert v["subtotal"] == 733.40
    assert v["charges"] == 7.00                       # SAC*C*D270***700
    assert v["charge_details"][0]["code"] == "D270"
    assert v["reconciles"] is True and v["variance"] == 0.0
    # ISS/CTT self-checks present in the file.
    assert v["line_count"] == 5 == len(v["lines"])
    assert v["unit_count"] == 39.7
    print("ok: real invoice totals reconcile (TDS implied decimals + SAC)")


def test_catch_weight_line_converts_pounds_to_cases():
    v = parse_810(INVOICE.read_text())[0]
    smoked = next(l for l in v["lines"] if l["item_no"] == "10053633")
    # IT1*000050*32.700*LB*16.02 ... TP*Y ... VU*3   +   PO4*002*3*LB
    # Case weight is pack count x unit weight = 2 x 3 = 6 lb, NOT VU alone.
    assert smoked["uom"] == "LB" and smoked["qty"] == 32.7
    assert smoked["catch_weight"] is True and smoked["unit_weight"] == 3.0
    assert smoked["pack_count"] == 2 and smoked["case_weight"] == 6.0
    assert smoked["case_weight_estimated"] is False
    assert smoked["cases"] == 5.45, smoked["cases"]   # 32.7 lb / 6 lb per case
    assert smoked["extended"] == round(32.7 * 16.02, 2)
    assert smoked["description"] == "SALMON COLD SMOKED PRE-SLICED"
    assert smoked["pack"] == "002 3LB"
    # A plain case line is untouched by the conversion.
    coke = next(l for l in v["lines"] if l["item_no"] == "102309")
    assert coke["uom"] == "CA" and coke["qty"] == 1.0 and coke["cases"] == 1.0
    assert coke["case_weight"] is None
    print("ok: catch-weight LB -> cases via pack count x VU; CA lines untouched")


def test_catch_weight_pack_count_beats_rounded_po4_size():
    """PO4's size element is rounded to whole units; VU carries the precise
    per-unit weight. 10024349 is PO4*008*2*LB with VU*1.5 -- a 12 lb case, not
    16 -- so 24 lb must come out as exactly 2 cases."""
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV1*20260730*PO1***DI*00",
        "IT1*000010*24.000*LB*3.52**VN*10024349*TP*Y*VU*1.5",
        "PO4*008*2*LB",
        "TDS*8448",
        "SE*6*1",
    ]) + "~"
    line = parse_810(doc)[0]["lines"][0]
    assert line["unit_weight"] == 1.5 and line["pack_count"] == 8
    assert line["case_weight"] == 12.0
    assert line["cases"] == 2.0, line["cases"]
    print("ok: pack count x VU (12 lb) used over PO4's rounded 2LB size")


def test_weight_line_without_po4_is_flagged_as_estimated():
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV2*20260730*PO2***DI*00",
        "IT1*000010*20.000*LB*2.80**VN*164011*TP*Y*VU*5",
        "TDS*5600",
        "SE*5*1",
    ]) + "~"
    line = parse_810(doc)[0]["lines"][0]
    # No PO4 -> pack count unknown, falls back to 1 and says so.
    assert line["pack_count"] is None
    assert line["case_weight"] == 5.0 and line["case_weight_estimated"] is True
    assert line["cases"] == 4.0
    print("ok: weight line with no PO4 falls back to VU and flags the estimate")


def test_weight_line_without_vu_yields_no_case_count():
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV3*20260730*PO3***DI*00",
        "IT1*000010*20.000*LB*2.80**VN*999999*TP*Y",
        "TDS*5600",
        "SE*5*1",
    ]) + "~"
    inv = parse_810(doc)[0]
    assert inv["lines"][0]["cases"] is None
    # ...and the batch summary names the line rather than silently dropping it.
    assert summarize([inv])["lines_without_cases"] == ["INV3:000010 (999999 20.0 LB)"]
    print("ok: weight line with no VU reports no cases and is surfaced")


def test_credit_memo_is_signed_negative():
    v = parse_810(CREDIT.read_text())[0]
    assert v["invoice_number"] == "9800024285"
    assert v["doc_type"] == "CR" and v["is_credit"] is True and v["sign"] == -1
    assert v["ship_from"] == "PUNTA GORDA" and v["ship_from_code"] == "3006"
    assert v["total"] == 89.34                       # TDS*8934
    assert v["subtotal"] == 83.10
    assert v["tax"] == 6.24                          # TXI*LS*6.24
    assert v["charges"] == 0.0
    assert v["reconciles"] is True
    # A credit must reduce, not increase, a spend/case ledger.
    s = summarize([v])
    assert s["credits"] == 1
    assert s["net_total"] == -89.34, s["net_total"]
    assert s["net_cases"] == -1.0, s["net_cases"]
    print("ok: credit memo (BIG07=CR) carries sign=-1 and nets negative")


def test_all_seven_real_invoices_reconcile():
    """The whole drop, not just the two invoices the other tests poke at.
    This is the claim the money fix rests on, so CI pins it."""
    files = sorted(FIX.glob("CheneyInvoices_*.EDI"))
    assert len(files) == 7, [f.name for f in files]
    invs = []
    for f in files:
        invs.extend(parse_810(f.read_text()))
    assert len(invs) == 7
    for v in invs:
        assert v["reconciles"], (v["invoice_number"], v["variance"])
        assert v["total"] is not None and v["total"] < 100_000, v["total"]
        # Every line resolves to a case count, and none of them guessed a pack.
        for l in v["lines"]:
            assert l["cases"] is not None, (v["invoice_number"], l["line_no"])
            assert l["case_weight_estimated"] is False
    s = summarize(invs)
    assert s == {**s, "invoices": 7, "credits": 2, "lines": 117,
                 "net_total": 10869.66, "unreconciled": [],
                 "lines_without_cases": [], "lines_with_estimated_pack": [],
                 "line_count_mismatch": [], "unit_count_mismatch": []}
    # DC coverage across the drop: 3 tracked FL DCs + Statesville NC.
    assert sorted({v["ship_from"] for v in invs}) == [
        "OCALA", "PUNTA GORDA", "RIVIERA", "STATESVILLE"]
    print("ok: all 7 real invoices reconcile; 117 lines all have case counts")


def test_money_tolerates_commas_and_trailing_sign():
    assert _money("1,676.19") == 1676.19
    assert _money("9449-") == -94.49          # X12 trailing sign
    assert _money("$1,676.19") == 1676.19
    assert _money("1E3") is None              # not a number we should guess at
    assert _money("12x34") is None
    print("ok: money parsing handles commas + trailing sign, rejects junk")


def test_negative_line_on_an_invoice_keeps_its_sign():
    """A returned line netted into a delivery invoice is routine X12. Clamping
    the whole document to a magnitude would count the return as a delivery."""
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV8*20260730*PO8***DI*00",
        "IT1*000010*5.000*CA*30.00**VN*10153018",
        "IT1*000020*-1.000*CA*30.00**VN*10153018",
        "TDS*12000",
        "SE*6*1",
    ]) + "~"
    v = parse_810(doc)[0]
    assert v["is_credit"] is False
    s = summarize([v])
    assert s["net_cases"] == 4.0, s["net_cases"]      # 5 shipped, 1 returned
    assert s["net_total"] == 120.00
    print("ok: a negative line on a non-credit invoice keeps its sign")


def test_credit_direction_survives_a_negative_total():
    """If Cheney ever states credit amounts already-negative, a credit must
    still net negative rather than flipping to a charge."""
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*CR9*20260730*PO9***CR*00",
        "IT1*000010*-1.000*CA*83.10**VN*10063604",
        "TDS*-8310",
        "SE*5*1",
    ]) + "~"
    v = parse_810(doc)[0]
    assert v["is_credit"] is True and v["total"] == -83.10
    s = summarize([v])
    assert s["net_total"] == -83.10, s["net_total"]
    assert s["net_cases"] == -1.0, s["net_cases"]
    print("ok: credit stays negative even when the sender pre-signs amounts")


def test_second_po4_does_not_overwrite_the_first():
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV4*20260730*PO4X***DI*00",
        "IT1*000010*24.000*LB*3.52**VN*10024349*TP*Y*VU*1.5",
        "PO4*008*2*LB",
        "PO4*004*5*LB",
        "TDS*8448",
        "SE*7*1",
    ]) + "~"
    line = parse_810(doc)[0]["lines"][0]
    assert line["pack_count"] == 8 and line["case_weight"] == 12.0
    assert line["cases"] == 2.0, line["cases"]
    print("ok: a stray second PO4 cannot overwrite a line's pack count")


def test_pack_in_a_different_uom_is_not_divided():
    """A 320 OZ line against a '004 5LB' pack can't be divided safely -- report
    no case count rather than a 16x-wrong one."""
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV5*20260730*PO5***DI*00",
        "IT1*000010*320.000*OZ*0.18**VN*99999*TP*Y*VU*5",
        "PO4*004*5*LB",
        "TDS*5760",
        "SE*6*1",
    ]) + "~"
    inv = parse_810(doc)[0]
    assert inv["lines"][0]["cases"] is None
    assert summarize([inv])["lines_without_cases"] == ["INV5:000010 (99999 320.0 OZ)"]
    print("ok: mismatched pack UOM yields no case count instead of a wrong one")


def test_estimated_pack_is_reported_not_hidden():
    """No PO4 -> the pack count is assumed 1, which for a real 4-unit pack is
    4x high. It must never pass the roll-up as clean."""
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV6*20260730*PO6***DI*00",
        "IT1*000010*20.000*LB*2.80**VN*164011*TP*Y*VU*5",
        "TDS*5600",
        "SE*5*1",
    ]) + "~"
    inv = parse_810(doc)[0]
    line = inv["lines"][0]
    assert line["case_weight_estimated"] is True and line["cases"] == 4.0
    s = summarize([inv])
    assert s["lines_without_cases"] == []      # it DID produce a number...
    assert s["lines_with_estimated_pack"] == [   # ...but the number is flagged
        "INV6:000010 (164011 20.0 LB -> 4.0 cs assuming 1/pack)"]
    print("ok: assumed-pack lines are surfaced separately from clean ones")


def test_blank_quantity_on_a_weight_line_does_not_crash_reporting():
    """Regression: the daily 810 report formatted qty with :g unconditionally,
    so one blank IT102 on a non-CA line killed the whole run."""
    doc = "~".join([
        "ST*810*1",
        "BIG*20260731*INV7*20260730*PO7***DI*00",
        "IT1*000010**LB*2.73**VN*164011*TP*Y*VU*5",
        "PO4*004*5*LB",
        "TDS*0",
        "SE*6*1",
    ]) + "~"
    line = parse_810(doc)[0]["lines"][0]
    assert line["qty"] is None and line["cases"] is None
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "blank.edi"
        p.write_text(doc)
        r = subprocess.run(
            [sys.executable, "scripts/ingest_cheney_810.py", "--edi", str(p)],
            capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    assert "Batch summary" in r.stdout, r.stdout[-500:]
    print("ok: blank quantity on a weight line no longer crashes the report")


def test_summarize_flags_integrity_problems():
    invs = parse_810(INVOICE.read_text()) + parse_810(CREDIT.read_text())
    s = summarize(invs)
    assert s["invoices"] == 2 and s["credits"] == 1
    assert s["net_total"] == round(740.40 - 89.34, 2)
    assert s["unreconciled"] == []
    assert s["lines_without_cases"] == []
    assert s["line_count_mismatch"] == []
    assert s["unit_count_mismatch"] == []
    print("ok: batch summary nets credits and reports no integrity faults")


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\nall edi_810 tests passed")
