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


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\nall edi_810 tests passed")
