"""Count checks: theoretical vs actual (integrations/stock_insights.py,
GET /api/insights)."""
from __future__ import annotations

from datetime import datetime, timedelta

from integrations.stock_insights import count_checks
from test_po_operator_paths import H, KEY, _book, _day, env  # noqa: F401

NOW = datetime(2026, 10, 23, 12, 0)


def _ts(days_ago, hour=16):
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=0).isoformat()


def _item(**kw):
    base = {"name": "Plain Bagel 4oz [USF - La Mirada]", "warehouse": "La Mirada, CA",
            "distributor": "US Foods", "category": "bagels", "quantity": 255.0,
            "weekly_usage": 52.0, "last_count_at": _ts(4, 14) + "Z",
            "last_count_qty": 285.0, "on_order": []}
    base.update(kw)
    return base


def _audit_counts(name, counts):
    """counts: [(days_ago, projected_before, counted)] oldest first."""
    return [{"ts": _ts(d), "event_type": "on_hand", "name": name,
             "old_quantity": old, "new_quantity": new, "delta": new - old}
            for d, old, new in counts]


# --- count check ----------------------------------------------------------------

def test_count_check_from_the_audit_and_count_based_usage():
    """La Mirada Plain, the real 10/19 shape: projected 248, counted 285; the
    counts drop ~25-30 cs a week while the forecast burns 52."""
    name = "Plain Bagel 4oz [USF - La Mirada]"
    key = name.lower()
    inv = {key: _item()}
    audit = _audit_counts(name, [(25, 300, 327), (18, 280, 302), (11, 280, 303),
                                 (4, 248, 285)])
    r = count_checks(inv, [], audit)
    c = r["items"][key]
    assert c["projected"] == 248 and c["counted"] == 285 and c["variance"] == 37
    assert c["flag"] and c["direction"] == "higher"
    assert abs(c["week"] - 18.0) <= 1              # 303 -> 285 in ~7 days
    assert 13 <= c["usage_4w"] <= 15.5             # (327 - 285) over ~3 weeks
    w = r["warehouses"][0]
    assert w["flag"] and "Usage looks off on Plain" in " ".join(w["hints"])


def test_recorded_check_wins_over_the_audit():
    key = "plain bagel 4oz [usf - la mirada]"
    inv = {key: _item(count_check={"count_at": _item()["last_count_at"],
                                   "projected": 280.0, "prev_count_at": _ts(11, 14),
                                   "prev_counted": 303.0})}
    r = count_checks(inv, [], [])
    assert r["items"][key]["variance"] == 5 and not r["items"][key]["flag"]
    assert r["items"][key]["source"] == "recorded"


def test_rise_without_a_delivery_is_reported_not_averaged():
    name = "Cinnamon Raisin Bagel 4oz [USF - Chicago]"
    key = name.lower()
    inv = {key: _item(name=name, warehouse="Chicago, IL", weekly_usage=8.0,
                      last_count_qty=95.0, quantity=90.0)}
    audit = _audit_counts(name, [(18, 70, 70), (11, 60, 57), (4, 49, 95)])
    c = count_checks(inv, [], audit)["items"][key]
    assert c["unexplained_rise"] == 38 and "week" not in c
    hints = count_checks(inv, [], audit)["warehouses"][0]["hints"]
    assert any("ROSE" in h for h in hints) or any("Isolated" in h for h in hints)


def test_a_delivery_between_counts_is_not_usage():
    name = "Plain Bagel 4oz [USF - Zebulon]"
    key = name.lower()
    inv = {key: _item(name=name, warehouse="Zebulon, NC", weekly_usage=14.0,
                      last_count_qty=61.0, quantity=58.0)}
    audit = _audit_counts(name, [(11, 60, 65), (4, 72, 61)])
    usage = [{"item_key": key, "source": "on_order_rollover", "amount": -24.0,
              "arrival_date": (NOW - timedelta(days=8)).date().isoformat() + "T00:00:00",
              "timestamp": _ts(8)}]
    c = count_checks(inv, usage, audit)["items"][key]
    assert abs(c["week"] - 28.0) <= 1.5           # 65 + 24 - 61 in ~7 days


def test_stamped_at_the_count(env):
    """sync_inventory records the projection when a NEW count lands."""
    it, sync, c = env
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent

    def _count(qty, days_ago):
        d = (datetime.now() - timedelta(days=days_ago)).replace(microsecond=0)
        sync._apply_events([EmailEvent(
            event_type="on_hand",
            item=SyncItem(quantity=qty, distributor="US Foods", variety="Plain",
                          warehouse="Manassas, VA", unit="cases"),
            source_message_id=f"m{days_ago}", source_subject="MANASSAS INVENTORY",
            count_date=d.isoformat(), source_received_at=d.isoformat() + "Z")])
    _count(40, 8)
    inv = it.load_inventory()
    inv[KEY]["weekly_usage"] = 7.0
    inv[KEY]["quantity"] = 33.0                     # a week of burn later
    it.save_inventory(inv)
    _count(30, 1)
    cc = it.load_inventory()[KEY]["count_check"]
    assert cc["projected"] == 33.0 and cc["prev_counted"] == 40
    r = c.get("/api/insights", headers=H).get_json()
    chk = r["count_checks"]["items"][KEY]
    assert chk["variance"] == -3 and chk["source"] == "recorded"


def test_insights_endpoint(env):
    it, sync, c = env
    _book(sync, "7800001O", order_day=_day(-3))
    r = c.get("/api/insights", headers=H).get_json()
    assert r["ok"] and "warehouses" in r["orders"] and "items" in r["count_checks"]


def test_po_booked_after_the_count_explains_an_overage():
    """Manassas 10/19: PO 7199415O landed 10/16, rolled in 10/20 after the
    count (later absorbed), so the count was ~112 cs over the projection."""
    count_day = 4
    inv, audit, lines = {}, [], []
    for v in ("Plain", "Everything", "Sesame", "Asiago"):
        name = f"{v} Bagel 4oz [USF - Manassas]"
        inv[name.lower()] = _item(name=name, warehouse="Manassas, VA", weekly_usage=6.0,
                                  last_count_qty=40.0, quantity=36.0)
        audit += _audit_counts(name, [(count_day, 12.0, 40.0)])
        lines.append({"variety": v, "qty": 28.0, "absorbed": True})
    ledger = [{"po_number": "7199415O", "warehouse": "Manassas, VA", "status": "arrived",
               "distributor": "US Foods", "lines": lines,
               "arrival_date": (NOW - timedelta(days=7)).isoformat(),
               "rolled_at": (NOW - timedelta(days=3)).isoformat()},
              {"po_number": "8228405O", "warehouse": "Manassas, VA", "status": "pending",
               "distributor": "US Foods",
               "lines": [{"variety": v, "qty": 28.0} for v in ("Plain", "Everything",
                                                                 "Sesame", "Asiago")]}]
    hints = count_checks(inv, [], audit, ledger=ledger)["warehouses"][0]["hints"]
    assert hints[0].startswith("PO 7199415O") and "booked after the count" in hints[0]
    assert len(hints) == 1
