"""Cheney 810 invoices as an independent check on the reported usage.

The 810s are Cheney's invoices to each H&H store: every H&H bagel line is
cases that left a Cheney DC. Stored idempotently (H&H bagel lines only), then
compared, per warehouse + variety, with the rep-reported weekly_usage.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from integrations import cheney_810_store as store
from integrations import po_health as PH
from integrations.edi_810 import parse_810

FIX = Path(__file__).resolve().parent / "fixtures" / "cheney"
H = {"X-Inventory-Token": "tok"}
TODAY = date(2026, 8, 12)                       # a Wednesday


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    import inventory_tracker as it
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for attr, fn in (("INVENTORY_FILE", "inventory.json"), ("USAGE_FILE", "usage.json"),
                     ("PO_DELIVERY_FILE", "po_delivery.json")):
        monkeypatch.setattr(it, attr, tmp_path / fn)
    it.save_inventory({})
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, app_module.app.test_client()


def _all_invoices():
    out = []
    for f in sorted(FIX.glob("*.EDI")):
        out.extend(parse_810(f.read_text(encoding="utf-8", errors="replace")))
    return out


def _item(variety, wh, wu):
    tag = wh.split(",")[0]
    return {f"{variety.lower()} bagel 4oz [cb - {tag.lower()}]": {
        "name": f"{variety} Bagel 4oz [CB - {tag}]", "distributor": "Cheney Brothers",
        "warehouse": wh, "weekly_usage": wu, "quantity": 50}}


def _line(day, wh, variety, cases, inv="X1", n=1):
    return {"invoice_number": inv, "invoice_date": day, "warehouse": wh,
            "variety": variety, "cases": cases, "item_no": "10153018"}


# ----------------------------------------------------------------- the store

def test_only_hh_bagel_lines_are_kept_with_dc_and_variety():
    lines, notes = store.lines_from_invoices(_all_invoices())
    assert lines and all(l["variety"] for l in lines)
    ocala_plain = [l for l in lines if l["warehouse"] == "Ocala, FL" and l["variety"] == "Plain"]
    assert sorted(l["cases"] for l in ocala_plain) == [3.0, 6.0, 6.0]
    riv = [l for l in lines if l["warehouse"] == "Riviera Beach, FL"]
    assert {l["variety"]: l["cases"] for l in riv}["Everything"] == 12.0
    assert all(l["store"] for l in lines) and all(l["invoice_date"] == "2026-07-31" for l in lines)
    # sausage patties ("BAGEL SIZE") and detergent are not bagels
    assert not any(l["item_no"] == "224012" for l in lines)


def test_ingest_is_idempotent_and_prunes_old_lines(env):
    it, _ = env
    inv = _all_invoices()
    a = store.ingest_invoices(inv, filename="a.EDI", today=TODAY)
    assert a["added"] == a["bagel_lines"] > 0 and a["replaced"] == 0
    b = store.ingest_invoices(inv, filename="a.EDI", today=TODAY)
    assert b["added"] == 0 and b["replaced"] == a["bagel_lines"]
    assert b["stored_lines"] == a["stored_lines"]
    # dry run stores nothing new
    it._write_json(it.DATA_DIR / "cheney_810.json", {"lines": {}})
    d = store.ingest_invoices(inv, dry_run=True, today=TODAY)
    assert d["added"] > 0 and store.load_lines() == []
    # > 400 days old: pruned
    store.ingest_invoices(inv, today=TODAY)
    late = store.ingest_invoices([], filename="later.EDI", today=TODAY + timedelta(days=420))
    assert late["pruned"] == a["bagel_lines"] and store.load_lines() == []


def test_credit_memo_bagel_lines_are_negative():
    inv = [{"invoice_number": "CR1", "invoice_date": "2026-07-31", "is_credit": True,
            "ship_from_code": "3005", "ship_to_account": "60446046",
            "lines": [{"line_no": "1", "item_no": "10153018", "cases": 2.0}]}]
    [l] = store.lines_from_invoices(inv)[0]
    assert l["cases"] == -2.0 and l["warehouse"] == "Ocala, FL"


# ------------------------------------------------------------- the crosscheck

def test_full_weeks_are_monday_to_sunday_before_this_week():
    weeks = store._full_weeks(TODAY)
    assert [(a.isoformat(), b.isoformat()) for a, b in weeks] == [
        ("2026-07-13", "2026-07-19"), ("2026-07-20", "2026-07-26"),
        ("2026-07-27", "2026-08-02"), ("2026-08-03", "2026-08-09")]


def test_diverges_needs_35_percent_and_5_cases_and_enough_weeks():
    wh = "Ocala, FL"
    inv = {**_item("Plain", wh, 10.0), **_item("Egg", wh, 3.0), **_item("Onion", wh, 20.0),
           **_item("Sesame", "Riviera Beach, FL", 1.0)}
    days = ["2026-07-14", "2026-07-21", "2026-07-28", "2026-08-04"]
    lines = []
    for i, d in enumerate(days):
        lines += [_line(d, wh, "Plain", 20.0, f"P{i}"),          # 20 vs 10: +100%, +10 cs
                  _line(d, wh, "Egg", 6.0, f"E{i}"),             # 6 vs 3: +100% but only 3 cs
                  _line(d, wh, "Onion", 18.0, f"O{i}")]          # 18 vs 20: 10%
    lines.append(_line("2026-08-04", "Riviera Beach, FL", "Sesame", 40.0, "S1"))   # 1 week only
    lines.append(_line("2026-08-11", wh, "Plain", 500.0, "NOW"))                   # this week: ignored
    res = store.crosscheck(inv, lines, TODAY)
    rows = {(r["warehouse"], r["variety"]): r for r in res["rows"]}
    p = rows[(wh, "Plain")]
    assert p["invoiced_cs_per_week"] == 20.0 and p["reported_weekly_usage"] == 10.0
    assert p["diverges"] is True and p["direction"] == "invoiced_above_reported"
    assert p["invoiced_by_week"] == [20.0, 20.0, 20.0, 20.0]
    assert rows[(wh, "Egg")]["diverges"] is False          # under 5 cs/wk apart
    assert rows[(wh, "Onion")]["diverges"] is False        # under 35%
    s = rows[("Riviera Beach, FL", "Sesame")]
    assert s["insufficient_data"] is True and s["diverges"] is False
    assert [w["diverging"] for w in res["warehouses"] if w["warehouse"] == wh] == [["Plain"]]
    assert "store accounts only" in res["limits"]


def test_reported_usage_with_no_invoices_only_diverges_when_the_feed_is_live():
    wh = "Ocala, FL"
    inv = {**_item("Plain", wh, 30.0), **_item("Egg", wh, 30.0)}
    # no invoices at all for the warehouse: nothing to compare against
    res = store.crosscheck(inv, [], TODAY)
    assert all(r["insufficient_data"] and not r["diverges"] for r in res["rows"])
    # the feed is live (Plain invoiced every week) and Egg never appears
    lines = [_line(d, wh, "Plain", 30.0, f"P{i}") for i, d in enumerate(
        ["2026-07-14", "2026-07-21", "2026-07-28", "2026-08-04"])]
    rows = {r["variety"]: r for r in store.crosscheck(inv, lines, TODAY)["rows"]}
    assert rows["Plain"]["diverges"] is False
    assert rows["Egg"]["diverges"] is True
    assert rows["Egg"]["direction"] == "invoiced_below_reported"


# --------------------------------------------------------------- the routes

def test_ingest_route_and_crosscheck_route(env):
    it, c = env
    edi = sorted(FIX.glob("*.EDI"))[0]
    text = edi.read_text(encoding="utf-8", errors="replace")
    r = c.post(f"/api/ingest/cheney-810?dry_run=1&filename={edi.name}", data=text,
               headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 200 and r.get_json()["dry_run"] is True
    assert store.load_lines() == []
    r = c.post(f"/api/ingest/cheney-810?filename={edi.name}", data=text,
               headers={**H, "Content-Type": "text/plain"})
    body = r.get_json()
    assert body["ok"] and body["added"] == body["bagel_lines"] == 6
    assert body["integrity"]["unreconciled"] == []
    r = c.post("/api/ingest/cheney-810", json={"edi": text}, headers=H)
    assert r.get_json()["replaced"] == 6 and r.get_json()["added"] == 0
    assert c.post("/api/ingest/cheney-810", data="", headers=H).status_code == 400
    assert c.post("/api/ingest/cheney-810", data="not edi",
                  headers={**H, "Content-Type": "text/plain"}).status_code == 422
    assert c.post("/api/ingest/cheney-810", data=text).status_code == 401     # token-gated
    out = c.get("/api/usage/cheney-crosscheck", headers=H).get_json()
    assert out["ok"] and len(out["weeks"]) == 4 and out["stored_lines"] == 6
    assert "thresholds" in out and isinstance(out["rows"], list)
    # inventory was never touched
    assert it.load_inventory() == {}


def test_run_checks_raises_usage_diverge_at_three_varieties(env, monkeypatch):
    it, _ = env
    from core import feed_health
    from datetime import datetime, timezone
    wh = "Ocala, FL"
    inv = {}
    for v in ("Plain", "Everything", "Sesame", "Onion"):
        inv.update(_item(v, wh, 10.0))
    it.save_inventory(inv)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    lines = {}
    for i, d in enumerate(["2026-07-14", "2026-07-21", "2026-07-28", "2026-08-04"]):
        for v, cs in (("Plain", 25.0), ("Everything", 30.0), ("Sesame", 10.0), ("Onion", 11.0)):
            lines[f"I{i}|{v}"] = _line(d, wh, v, cs, f"I{i}")
    it._write_json(it.DATA_DIR / "cheney_810.json", {"lines": lines})
    out = PH._check_usage_divergence(now)
    assert out["diverging_warehouses"] == []                 # only two diverge
    assert "usage-diverge:Ocala, FL" not in feed_health.snapshot()["issues"]
    for i, d in enumerate(["2026-07-14", "2026-07-21", "2026-07-28", "2026-08-04"]):
        lines[f"I{i}|Sesame"] = _line(d, wh, "Sesame", 22.0, f"I{i}")
    it._write_json(it.DATA_DIR / "cheney_810.json", {"lines": lines})
    out = PH._check_usage_divergence(now)
    assert out["diverging_warehouses"] == [wh]
    msg = feed_health.snapshot()["issues"]["usage-diverge:Ocala, FL"]["message"]
    assert "3 varieties" in msg and "Plain invoiced 25 vs reported 10" in msg
