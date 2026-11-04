"""Nothing still on order is a confirmed arrival.

On 2026-10-22 the Inventory tab showed a green "Confirmed arrival" for 19
SKUs: ship + 7 guesses (7402733Y, 7236706H, still only "requested" from
Lineage) and 014511763291's 10/22 appointment, while the Pending tab said
"Awaiting delivery confirmation". The On Order cell now says where its date
came from.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from blueprints.inventory import _enrich_on_order


def _item(*entries):
    return _enrich_on_order({"name": "Plain Bagel 4oz [USF - Chicago]",
                             "on_order": list(entries)})


def test_kind_of_the_next_arrival():
    appt = _item({"po_number": "014511763291", "qty": 56, "arrival_source": "lineage",
                  "arrival_date": "2026-10-22T09:00:00", "eta": "2026-10-22T09:00:00"})
    assert appt["on_order_next_kind"] == "appointment" and not appt["on_order_next_is_actual"]
    est = _item({"po_number": "7402733Y", "qty": 56, "ship_date": "2026-10-23T00:00:00",
                 "arrival_date": "2026-10-30T00:00:00", "eta": "2026-10-25T00:00:00"})
    assert est["on_order_next_kind"] == "estimate" and not est["on_order_next_is_actual"]
    for src in ("freight", "ship+transit"):
        assert _item({"po_number": "X", "qty": 8, "arrival_source": src,
                      "arrival_date": "2026-10-30T00:00:00"})["on_order_next_kind"] == "estimate"
    eta = _item({"po_number": "9314683Y", "qty": 56, "eta": "2026-11-20T00:00:00"})
    assert eta["on_order_next_kind"] == "estimate"
    op = _item({"po_number": "Y", "qty": 8, "arrival_source": "operator",
                "arrival_date": "2026-10-28T00:00:00"},
               {"po_number": "Z", "qty": 8, "eta": "2026-11-20T00:00:00"})
    assert op["on_order_next_kind"] == "operator" and op["on_order_next_is_actual"] is True
    assert op["on_order_next_arrival"] == "2026-10-28T00:00:00"
    assert _item()["on_order_next_kind"] == ""


def test_the_inventory_tab_never_says_confirmed_arrival():
    js = Path("static/app.js").read_text()
    body = re.search(r"function renderOnOrder\(item\) \{(.*?)\n\}", js, re.S).group(1)
    assert "Confirmed arrival" not in body
    assert "on_order_next_kind" in body and "set by you" in body and "appt" in body
