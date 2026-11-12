"""Order plan, production load and bake schedule (integrations/order_plan.py),
plus the reps-vs-counts backtest (integrations/usage_compare.py).

JD's rules (2026-10-23): lead time 30 days; every PO at least 2 pallets, and 6
at Riviera Beach, La Mirada, Ocala, US Foods Chicago and Zebulon; varieties
mix on a pallet (only the PO total lands on whole pallets); production caps at
6 pallets a day, Mon-Fri; plan on the reps' usage unless shown otherwise.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from integrations.order_plan import (plan_orders, production_load, daily_schedule,
                                     usage_rate, ORDER_DEFAULTS)
from integrations.planning_config import load_planning_config, min_po_pallets_for
from integrations.usage_compare import backtest

NOW = datetime(2026, 10, 23, 9, 0)        # a Friday morning


def _item(variety, wh, qty, wu, dist="US Foods", on_order=None, **kw):
    tag = "USF" if dist == "US Foods" else "CB"
    d = {"name": f"{variety} Bagel 4oz [{tag} - {wh.split(',')[0]}]", "warehouse": wh,
         "distributor": dist, "category": "bagels", "quantity": qty,
         "weekly_usage": wu, "on_order": on_order or []}
    d.update(kw)
    return d


def _inv(*items):
    return {i["name"].lower(): i for i in items}


def test_rules_in_config():
    cfg = load_planning_config()
    assert cfg["ordering"]["lead_days"] == 30 and cfg["ordering"]["usage_basis"] == "reps"
    assert cfg["capacity"]["max_cs_per_day"] == 336
    for wh in ("Riviera Beach, FL", "La Mirada, CA", "Ocala, FL", "Chicago, IL", "Zebulon, NC"):
        assert min_po_pallets_for(wh) == 6
    for wh in ("Punta Gorda, FL", "Alcoa, TN", "Manassas, VA", "Houston, TX"):
        assert min_po_pallets_for(wh) == 2


def test_one_pallet_need_is_padded_to_the_two_pallet_minimum():
    cfg = load_planning_config()
    inv = _inv(_item("Egg", "Alcoa, TN", 5.0, 1.0), _item("Plain", "Alcoa, TN", 30.0, 4.0))
    w = plan_orders(inv, cfg, "reps", now=NOW)["warehouses"][0]
    o = w["order_now"]
    assert o and o["total_cs"] == 112 and o["pallets"] == 2 and o["padded_cs"] > 0
    assert o["arrival_date"] == (NOW + timedelta(days=30)).date().isoformat()
    assert o["ship_date"] == (NOW + timedelta(days=23)).date().isoformat()


def test_six_pallet_dc_orders_at_least_six_pallets_mixed():
    cfg = load_planning_config()
    inv = _inv(_item("Plain", "Ocala, FL", 40.0, 21.0, dist="Cheney Brothers"),
               _item("Onion", "Ocala, FL", 30.0, 4.0, dist="Cheney Brothers"),
               _item("Everything", "Ocala, FL", 200.0, 23.0, dist="Cheney Brothers"))
    o = plan_orders(inv, cfg, "reps", now=NOW)["warehouses"][0]["order_now"]
    assert o["total_cs"] >= 336 and o["total_cs"] % 56 == 0
    assert len(o["lines"]) >= 2                       # varieties mix on pallets
    assert all(l["qty"] % 8 == 0 for l in o["lines"])


def test_no_order_when_position_is_above_the_reorder_point():
    cfg = load_planning_config()
    inv = _inv(_item("Plain", "Zebulon, NC", 120.0, 14.0))
    w = plan_orders(inv, cfg, "reps", now=NOW)["warehouses"][0]
    assert w["order_now"] is None and w["next_order_date"] > NOW.date().isoformat()


def test_short_means_runs_out_before_a_new_order_could_land():
    cfg = load_planning_config()
    po = [{"qty": 32.0, "po_number": "7236706H",
           "arrival_date": (NOW + timedelta(days=7)).isoformat()}]
    inv = _inv(_item("Plain", "Alcoa, TN", 1.2, 4.2, on_order=po))
    w = plan_orders(inv, cfg, "reps", now=NOW)["warehouses"][0]
    line = w["lines"][0]
    assert line["short"] and line["stockout_date"] < (NOW + timedelta(days=7)).date().isoformat()


def test_counts_basis_uses_count_usage_only_with_enough_history():
    oc = dict(ORDER_DEFAULTS)
    it = _item("Plain", "La Mirada, CA", 255.0, 52.0)
    good = {"k": {"usage_4w": 34.0, "usage_4w_windows": 4, "usage_4w_days": 28}}
    thin = {"k": {"usage_4w": 34.0, "usage_4w_windows": 1, "usage_4w_days": 7}}
    assert usage_rate(it, "k", "counts", good, oc) == (34.0, "counts")
    assert usage_rate(it, "k", "counts", thin, oc)[1].startswith("reps")
    assert usage_rate(it, "k", "reps", good, oc) == (52.0, "reps")


def test_load_and_schedule_respect_six_pallets_a_day():
    cfg = load_planning_config()
    queue = [{"po_number": "A", "warehouse": "La Mirada, CA", "distributor": "US Foods",
              "total_cs": 560.0, "ship_date": (NOW + timedelta(days=4)).isoformat(),
              "transit_days": 7},
             {"po_number": "B", "warehouse": "Zebulon, NC", "distributor": "US Foods",
              "total_cs": 224.0, "ship_date": (NOW + timedelta(days=11)).isoformat(),
              "transit_days": 7}]
    sch = daily_schedule(queue, {"warehouses": []}, cfg, [], now=NOW)
    assert all(d["cs"] <= 336 for d in sch["days"])
    assert sch["days"][0]["date"] == NOW.date().isoformat()        # before 2 pm
    assert [j["label"] for j in sch["days"][0]["jobs"]] == ["A"]
    assert sch["late"] == []
    load = production_load(queue, {"warehouses": []}, cfg, [], now=NOW)
    wk = load["weeks"]
    assert wk[0]["capacity_cs"] == 336                  # only Friday left
    assert sum(w["committed_cs"] for w in wk) == 784
    late = daily_schedule(queue, {"warehouses": []}, cfg, [],
                          now=NOW.replace(hour=15))
    assert late["days"][0]["date"] != NOW.date().isoformat()        # after 2 pm


def test_overloaded_week_reports_pull_forward():
    cfg = load_planning_config()
    big = [{"po_number": f"P{i}", "warehouse": "La Mirada, CA", "distributor": "US Foods",
            "total_cs": 560.0, "ship_date": (NOW + timedelta(days=17)).isoformat(),
            "transit_days": 7} for i in range(4)]
    load = production_load(big, {"warehouses": []}, cfg, [], now=NOW)
    over = [w for w in load["weeks"] if w["over_cs"] > 0]
    assert over and over[0]["pull_forward_possible_cs"] > 0


def test_backtest_scores_both_sources_without_peeking():
    """Six weekly counts of one SKU: true usage 30/wk, reps say 52/wk."""
    name = "Plain Bagel 4oz [USF - La Mirada]"
    key = name.lower()
    t0 = NOW - timedelta(days=42)
    audit, usage = [], []
    q = 400.0
    for k in range(6):
        ts = (t0 + timedelta(days=7 * k)).isoformat()
        audit.append({"ts": ts, "event_type": "on_hand", "name": name,
                      "old_quantity": q, "new_quantity": q, "delta": 0})
        q -= 30
    for dday in range(1, 42):
        usage.append({"item_key": key, "source": "forecast-daily", "amount": 52 / 7,
                      "timestamp": (t0 + timedelta(days=dday, hours=1)).isoformat()})
    inv = {key: {"name": name, "warehouse": "La Mirada, CA", "distributor": "US Foods",
                 "category": "bagels", "weekly_usage": 52.0,
                 "last_count_at": (t0 + timedelta(days=42)).isoformat(),
                 "last_count_qty": q}}
    r = backtest(inv, usage, audit)
    w = r["warehouses"][0]
    assert w["windows"] == 6 and abs(w["reps_bias_pct"] - 73) <= 2     # 52 vs 30
    assert w["counts_windows"] >= 3 and abs(w["counts_bias_pct"]) <= 1
    assert w["counts_closer_pct"] == 100


# --- data age + seasonal (review 2026-11-12) --------------------------------

def _cfg(seasonal=None, **age):
    cfg = load_planning_config()
    cfg["ordering"]["seasonal"] = seasonal or []
    if age:
        cfg["data_age"] = dict(cfg.get("data_age") or {}, **age)
    return cfg


def _dc(plan, wh="Alcoa, TN"):
    return next(w for w in plan["warehouses"] if w["warehouse"] == wh)


def test_usage_age_comes_from_the_usage_report_not_the_count():
    inv = _inv(_item("Plain", "Alcoa, TN", 10, 70,
                     last_count_at="2026-10-21T10:00:00",
                     last_usage_report_at="2026-09-01T10:00:00"))
    dc = _dc(plan_orders(inv, _cfg(), now=NOW))
    assert dc["count_age_days"] == 2 and dc["usage_age_days"] == 52
    assert dc["usage_age_basis"] == "usage_report"
    assert dc["data_stale"] and dc["check_data_first"] and dc["order_now"]
    assert dc["data_stale_reasons"] == ["usage is 52 days old"]


def test_fresh_data_is_not_flagged_and_old_count_is():
    fresh = _inv(_item("Plain", "Alcoa, TN", 10, 70, last_count_at="2026-10-20",
                       last_usage_report_at="2026-10-20"))
    dc = _dc(plan_orders(fresh, _cfg(), now=NOW))
    assert not dc["data_stale"] and not dc["check_data_first"]
    old = _inv(_item("Plain", "Alcoa, TN", 10, 70, last_count_at="2026-10-01",
                     last_usage_report_at="2026-10-20"))
    dc = _dc(plan_orders(old, _cfg(), now=NOW))
    assert dc["count_age_days"] == 22 and dc["data_stale_reasons"] == ["count is 22 days old"]
    never = _inv(_item("Plain", "Alcoa, TN", 10, 70))
    dc = _dc(plan_orders(never, _cfg(), now=NOW))
    assert dc["count_age_days"] is None and "no count on file" in dc["data_stale_reasons"]


def test_records_without_a_usage_stamp_fall_back_to_the_count_date():
    inv = _inv(_item("Plain", "Alcoa, TN", 10, 70, last_count_at="2026-10-20"))
    dc = _dc(plan_orders(inv, _cfg(), now=NOW))
    assert dc["usage_age_days"] == 3 and dc["usage_age_basis"] == "count"
    assert not dc["data_stale"]


def _strip(plan):
    """The plan minus the fields that only exist to describe seasonality."""
    import copy
    p = copy.deepcopy(plan)
    p.pop("seasonal", None)
    p["params"].pop("seasonal", None)
    for w in p["warehouses"]:
        for o in w["projected_orders"]:
            o.pop("seasonal", None); o.pop("seasonal_note", None)
        if w.get("order_now"):
            w["order_now"].pop("seasonal", None); w["order_now"].pop("seasonal_note", None)
    return p


def test_no_seasonal_configured_is_identical_to_a_neutral_window():
    inv = _inv(_item("Plain", "Alcoa, TN", 40, 63), _item("Onion", "Alcoa, TN", 300, 17))
    base = plan_orders(inv, _cfg(), now=NOW)
    # a window with factor 1.0 is dropped; a window that never touches the horizon changes nothing
    same = plan_orders(inv, _cfg([{"from": "12-15", "to": "01-02", "factor": 1.0}]), now=NOW)
    far = plan_orders(inv, _cfg([{"from": "05-01", "to": "05-10", "factor": 2.0}]), now=NOW)
    assert _strip(base) == _strip(same) == _strip(far)
    assert base["seasonal"] == {"configured": [], "applied": []}
    assert all(o["seasonal"] == [] and o["seasonal_note"] == ""
               for w in base["warehouses"] for o in w["projected_orders"])


def test_seasonal_factor_applies_only_inside_the_window():
    from integrations.order_plan import seasonal_windows, seasonal_factor
    w = seasonal_windows({"seasonal": [{"from": "12-15", "to": "01-02", "factor": 1.25,
                                        "label": "Holiday"}]})
    f = lambda m, d, y=2026: seasonal_factor(w, datetime(y, m, d))
    assert f(12, 14) == 1.0 and f(12, 15) == 1.25 and f(12, 31) == 1.25
    assert f(1, 1, 2027) == 1.25 and f(1, 2, 2027) == 1.25 and f(1, 3, 2027) == 1.0   # wraps
    assert f(7, 4) == 1.0
    # malformed entries are skipped, not raised on
    assert seasonal_windows({"seasonal": [{"from": "13-40", "to": "x", "factor": "a"}, 7]}) == []


def test_seasonal_uplift_raises_the_order_and_says_so():
    inv = _inv(_item("Plain", "Alcoa, TN", 0, 70))      # 10 cs/day, nothing on hand
    now = datetime(2026, 11, 12, 9, 0)
    hol = [{"from": "12-15", "to": "01-02", "factor": 1.25, "label": "Holiday"}]
    base = _dc(plan_orders(inv, _cfg(), now=now))["order_now"]
    up = _dc(plan_orders(inv, _cfg(hol), now=now))["order_now"]
    # cover window = 30 lead + 7 review + 14 safety = 51 days (11/13..1/2);
    # 12/15..1/2 = 19 days at +25% -> 10*(51 + 19*0.25) = 557.5 vs 510
    assert base["needed_cs"] == 512 and up["needed_cs"] == 560        # 8-cs layers
    assert up["seasonal"] == [{"label": "Holiday", "factor": 1.25, "from": "2026-12-15",
                               "to": "2027-01-02", "days": 19, "window": "12-15..01-02"}]
    assert up["seasonal_note"] == "includes +25% holiday uplift for 12/15–1/2"
    plan = plan_orders(inv, _cfg(hol), now=now)
    assert plan["seasonal"]["configured"][0]["factor"] == 1.25
    assert plan["seasonal"]["applied"][0]["from"] == "2026-12-15"


def test_default_config_ships_the_holiday_window():
    s = load_planning_config()["ordering"]["seasonal"]
    assert s == [{"from": "12-15", "to": "01-02", "factor": 1.25, "label": "Holiday"}]
    assert load_planning_config()["data_age"] == {"count_max_days": 10, "usage_max_days": 21}
