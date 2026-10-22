"""A count that lands just after a CONFIRMED delivery, but before the
distributor's own system shows it, must not swallow the delivery.

Houston, PO 393072B2 (1,120 cs). Lineage delivered it 2026-10-19 08:00 CT
(Belinda: "Yes this shows delivered 0800AM/19th"). Tom Foley's buyer screen,
sent 11:20 CT the same morning, still listed every case under "Order" and Hand
totalled 404 cs -- a week's burn below the 10/13 count of 510. The rollover's
calendar-day rule ("delivered on/before the count's day = already counted")
marked all 13 lines absorbed, on-hand never moved, and because the PO ledger
only listed on_order_rollover rows the PO vanished from the Pending POs tab.

Pinned here:
  * the rollover keeps a confirmed delivery the count physically cannot
    contain, and stamps the receipt so re-reads of that count keep it;
  * the reverse order (delivery confirmed first, low count after) does too;
  * a count that DOES contain the delivery still absorbs it, unconfirmed ETAs
    keep the plain day rule, and the next count absorbs the receipt;
  * an absorbed PO still shows in the ledger as arrived;
  * Houston is in the warehouse catalog the Pending POs filter uses;
  * /api/admin/restore-absorbed-po repairs an already-absorbed PO.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent))

import inventory_tracker as it
import sync_inventory as sync
from core.util import count_missed_delivery, receipt_inside_count
from integrations import po_delivery as D

PO = "393072B2"
WH = "Houston, TX"
COUNT_AT = "2026-10-19T16:20:13Z"           # Tom's 10/19 email, 11:20 CT
DELIVERED = "2026-10-19T08:00:00-05:00"     # Lineage-confirmed, 08:00 CT
BELINDA = "bmorris@onelineage.com"

# variety: (Hand on 10/19, Order = PO line, cs/wk) -- read off Tom's screen
SCREEN = {
    "Egg": (24, 56, 5), "Blueberry": (8, 56, 7), "Asiago": (32, 56, 8),
    "Jalapeno Cheddar": (89, 112, 8), "Whole Wheat Everything": (15, 56, 5),
    "Whole Wheat": (22, 56, 5), "Poppy Seed": (34, 56, 6),
    "Cinnamon Raisin": (26, 56, 9), "Sesame": (25, 112, 12),
    "Plain": (50, 224, 19), "Everything": (49, 224, 22), "Onion": (30, 56, 5),
}
PLAIN = "plain bagel 4oz [usf - houston]"


def _key(v):
    return f"{v} Bagel 4oz [USF - Houston]".lower()


def _tracking(delivered=DELIVERED):
    rec, _, _ = D.apply_event(None, {
        "po_number": PO, "event": "requested",
        "thread": {"conversation_id": "c1", "subject": "Quote and Schedule - 2 Orders"},
        "evidence": {"message_id": "<req>", "sent": "2026-10-12T19:16:29Z",
                     "quote": f"PO #: {PO}"}}, datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc))
    rec, _, _ = D.apply_event(rec, {
        "po_number": PO, "event": "delivered", "delivered_at": delivered,
        "evidence": {"sender": BELINDA, "sent": "2026-10-19T20:53:31Z",
                     "quote": "Yes this shows delivered 0800AM/19th"}},
        datetime(2026, 10, 20, 13, 22, tzinfo=timezone.utc))
    return {PO: rec}


def _inv(count_at=COUNT_AT, counted=None, eta="2026-10-28T00:00:00"):
    """The 12 Houston SKUs on the PO, counted at ``count_at``, PO pending."""
    inv = {}
    for v, (hand, order, wk) in SCREEN.items():
        c = hand if counted is None else counted(hand, order)
        inv[_key(v)] = {
            "name": f"{v} Bagel 4oz [USF - Houston]", "distributor": "US Foods",
            "warehouse": WH, "unit": "cs", "quantity": float(c),
            "weekly_usage": float(wk), "last_count_at": count_at,
            "last_count_qty": float(c),
            "on_order": [{"po_number": PO, "po_revision": "0000005", "qty": float(order),
                          "unit": "cs", "ordered_at": "2026-09-28T00:00:00", "eta": eta,
                          "source_received_at": "2026-09-28T19:04:28Z",
                          "source_sender": "tom.foley@usfoods.com"}]}
    return inv


def _roll(monkeypatch, inv, tracking):
    monkeypatch.setattr(it, "load_po_delivery", lambda: tracking)
    it._rollover_on_order(inv)
    usage: list = []
    it._append_rollover_usage(inv, usage)
    return usage


def _total(inv):
    return sum(i["quantity"] for i in inv.values())


# ---------------------------------------------------------------------------
# The predicate and the physical test
# ---------------------------------------------------------------------------

def test_the_10_19_screen_cannot_contain_the_delivery():
    lines = [{"receipt": o, "counted": h, "weekly_usage": w}
             for h, o, w in SCREEN.values()]
    missed, why = count_missed_delivery(lines, 3.3)
    assert missed and "404" in why and "1120" in why


def test_a_count_that_holds_the_delivery_is_not_called_missed():
    lines = [{"receipt": o, "counted": h + o - 1, "weekly_usage": w}
             for h, o, w in SCREEN.values()]
    assert count_missed_delivery(lines, 3.3)[0] is False


def test_not_in_count_at_only_covers_that_count():
    row = {"arrival_date": it._parse_sane_dt(DELIVERED).isoformat(),
           "not_in_count_at": COUNT_AT}
    assert receipt_inside_count(row, COUNT_AT, "2026-10-19") is False
    assert receipt_inside_count(row, "2026-10-26T15:00:00Z", "2026-10-19") is True


def test_confirmed_delivery_uses_the_exact_time():
    row = {"arrival_date": it._parse_sane_dt(DELIVERED).isoformat(),
           "delivery_confirmed": True}
    early = it._parse_sane_dt(DELIVERED).replace(hour=0)  # same day, before it
    assert receipt_inside_count(row, early.isoformat(), "2026-10-19") is False
    assert receipt_inside_count(row, COUNT_AT, "2026-10-19") is True
    # an unconfirmed (ETA) row keeps the day rule
    assert receipt_inside_count({"arrival_date": it._parse_sane_dt(DELIVERED).isoformat()},
                                early.isoformat(), "2026-10-19") is True


# ---------------------------------------------------------------------------
# Rollover path: count in first, delivery confirmed afterwards (10/19-10/20)
# ---------------------------------------------------------------------------

def test_the_houston_case_rolls_in_instead_of_being_absorbed(monkeypatch):
    inv = _inv()
    usage = _roll(monkeypatch, inv, _tracking())
    assert _total(inv) == 404 + 1120
    assert all(i["on_order"] == [] for i in inv.values())
    assert [r["source"] for r in usage] == ["on_order_rollover"] * 12
    plain = next(r for r in usage if r["item_key"] == PLAIN)
    assert plain["amount"] == -224
    assert plain["not_in_count_at"] == COUNT_AT
    assert plain["delivery_confirmed"] is True
    assert "not in the" in plain["note"]
    # The 6-hourly re-read of Tom's same email keeps the receipt on top ...
    assert sync._receipts_after_count(usage, PLAIN, COUNT_AT) == 224
    # ... and next Monday's count absorbs it as usual.
    assert sync._receipts_after_count(usage, PLAIN, "2026-10-26T15:00:00Z") == 0


def test_a_count_that_includes_the_delivery_still_absorbs_it(monkeypatch):
    inv = _inv(counted=lambda hand, order: hand + order)
    usage = _roll(monkeypatch, inv, _tracking())
    assert _total(inv) == 404 + 1120          # the count already has it
    assert {r["source"] for r in usage} == {"on_order_absorbed"}


def test_a_count_stamped_before_the_delivery_never_absorbs_it(monkeypatch):
    early = "2026-10-19T12:00:00Z"            # 07:00 CT, an hour before the truck
    inv = _inv(count_at=early, counted=lambda hand, order: hand + order)
    usage = _roll(monkeypatch, inv, _tracking())
    assert _total(inv) == 404 + 1120 + 1120   # figures don't matter: time does
    assert all(r.get("not_in_count_at") == early for r in usage)


def test_an_unconfirmed_eta_keeps_the_day_rule(monkeypatch):
    inv = _inv(eta="2026-10-19T00:00:00")
    usage = _roll(monkeypatch, inv, {})
    assert _total(inv) == 404
    assert {r["source"] for r in usage} == {"on_order_absorbed"}


def test_a_count_days_after_the_delivery_absorbs_it(monkeypatch):
    inv = _inv(count_at="2026-10-23T15:00:00Z")   # > 72 h after the truck
    usage = _roll(monkeypatch, inv, _tracking())
    assert {r["source"] for r in usage} == {"on_order_absorbed"}


# ---------------------------------------------------------------------------
# Sync path: delivery confirmed first, the lagging count arrives afterwards
# ---------------------------------------------------------------------------

def _setup_disk(tmp: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp))
    monkeypatch.setattr(it, "DATA_DIR", tmp)
    monkeypatch.setattr(it, "INVENTORY_FILE", tmp / "inventory.json")
    monkeypatch.setattr(it, "USAGE_FILE", tmp / "usage.json")
    monkeypatch.setattr(sync, "INVENTORY_FILE", tmp / "inventory.json", raising=False)
    monkeypatch.setattr(it, "load_po_delivery", lambda: {})


def _count_evt(variety, qty, wk, count_at, msg="tom-1019"):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=qty, distributor="US Foods", variety=variety,
                      warehouse=WH, unit="cases", weekly_usage=wk),
        source_message_id=msg, source_subject="RE: Weekly Bagel Inventory",
        count_date=count_at, source_received_at=count_at,
        source_sender="tom.foley@usfoods.com",
        parser_source="usf-screenshot-ocr:image001.png")


def test_a_lagging_count_after_the_rollover_keeps_the_receipt(monkeypatch):
    with TemporaryDirectory() as td:
        _setup_disk(Path(td), monkeypatch)
        arrival = it._parse_sane_dt(DELIVERED).isoformat()
        inv = _inv(count_at="2026-10-13T14:26:49Z")   # last week's count
        usage = []
        for v, (hand, order, wk) in SCREEN.items():
            inv[_key(v)]["on_order"] = []
            inv[_key(v)]["quantity"] = float(hand + order)    # rolled in 10/19
            usage.append({"item_key": _key(v), "item_name": inv[_key(v)]["name"],
                          "amount": -float(order), "unit": "cs", "po_number": PO,
                          "po_revision": "0000005", "source": "on_order_rollover",
                          "arrival_date": arrival, "delivery_confirmed": True,
                          "timestamp": "2026-10-19T14:00:00", "warehouse": WH,
                          "note": f"PO {PO} arrived"})
        it.save_inventory(inv)
        it.save_usage(usage)

        events = [_count_evt(v, h, w, COUNT_AT) for v, (h, o, w) in SCREEN.items()]
        rep = sync._apply_events(events, dry_run=False)
        after = it.load_inventory()
        assert after[PLAIN]["quantity"] == 50 + 224
        assert round(_total({k: after[k] for k in inv}), 2) == 404 + 1120
        assert rep.get("count_missed_delivery")
        assert all(r.get("not_in_count_at") == COUNT_AT
                   for r in it.load_usage() if r.get("source") == "on_order_rollover")

        # The same email read again on the next cron: nothing moves.
        sync._apply_events(events, dry_run=False)
        assert it.load_inventory()[PLAIN]["quantity"] == 50 + 224

        # Next week's count contains the delivery and governs.
        nxt = [_count_evt("Plain", 251, 19, "2026-10-26T15:00:00Z", msg="tom-1026")]
        sync._apply_events(nxt, dry_run=False)
        assert it.load_inventory()[PLAIN]["quantity"] == 251


def test_a_count_that_shows_the_delivery_is_left_alone(monkeypatch):
    with TemporaryDirectory() as td:
        _setup_disk(Path(td), monkeypatch)
        arrival = it._parse_sane_dt(DELIVERED).isoformat()
        inv = _inv(count_at="2026-10-13T14:26:49Z")
        usage = []
        for v, (hand, order, wk) in SCREEN.items():
            inv[_key(v)]["on_order"] = []
            inv[_key(v)]["quantity"] = float(hand + order)
            usage.append({"item_key": _key(v), "amount": -float(order), "unit": "cs",
                          "po_number": PO, "source": "on_order_rollover",
                          "arrival_date": arrival, "delivery_confirmed": True,
                          "timestamp": "2026-10-19T14:00:00", "warehouse": WH})
        it.save_inventory(inv)
        it.save_usage(usage)
        events = [_count_evt(v, h + o, w, COUNT_AT) for v, (h, o, w) in SCREEN.items()]
        rep = sync._apply_events(events, dry_run=False)
        assert it.load_inventory()[PLAIN]["quantity"] == 50 + 224   # not doubled
        assert not rep.get("count_missed_delivery")


# ---------------------------------------------------------------------------
# Ledger + warehouse catalog
# ---------------------------------------------------------------------------

def _absorbed_rows():
    return [{"item_key": _key(v), "amount": 0, "absorbed_qty": float(o),
             "source": "on_order_absorbed", "po_number": PO, "po_revision": "0000005",
             "arrival_date": it._parse_sane_dt(DELIVERED).isoformat(),
             "warehouse": WH, "unit": "cs",
             "timestamp": "2026-10-20T13:24:31",
             "note": (f"PO {PO} arrival (ETA 2026-10-19) absorbed by the 2026-10-19 "
                      f"count - {o:g} cs already on hand, not re-added")}
            for v, (h, o, w) in SCREEN.items()]


def _ledger_env(monkeypatch, inv, usage, tracking=None):
    import blueprints.pos as pos
    monkeypatch.setattr(it, "load_inventory", lambda: inv)
    monkeypatch.setattr(it, "load_usage", lambda: usage)
    monkeypatch.setattr(it, "load_chefs_warehouse_pos", lambda: [])
    monkeypatch.setattr(it, "load_canceled_pos", lambda: {})
    monkeypatch.setattr(it, "load_status_overrides", lambda: {})
    monkeypatch.setattr(it, "load_po_delivery", lambda: tracking or {})
    monkeypatch.setattr(pos, "_freight_ship_date_index", lambda: {})
    return pos


def test_an_absorbed_po_still_shows_in_the_ledger_as_arrived(monkeypatch):
    inv = _inv()
    for i in inv.values():
        i["on_order"] = []
    pos = _ledger_env(monkeypatch, inv, _absorbed_rows())
    rec = next(r for r in pos.build_po_ledger() if r["po_number"] == PO)
    assert rec["status"] == "arrived"
    assert rec["warehouse"] == WH and rec["distributor"] == "US Foods"
    assert rec["total_cs"] == 1120
    assert all(l.get("absorbed") for l in rec["lines"])
    assert PO in pos._present_po_numbers()


def test_houston_is_in_the_warehouse_catalog():
    from blueprints.inventory import WAREHOUSES
    from seed_bagels import WAREHOUSES as SEED
    assert WH in WAREHOUSES["US Foods"]
    assert set(WAREHOUSES["US Foods"]) == {w for w, _s, _m in SEED["US Foods"]}


def test_houston_production_sheets_resolve():
    from integrations.production_pdf_parser import _classify_warehouse
    assert _classify_warehouse("US FOODS HOUSTON") == (WH, "US Foods")


# ---------------------------------------------------------------------------
# Repair endpoint
# ---------------------------------------------------------------------------

def _admin_client(monkeypatch, inv, usage, tracking):
    import blueprints.admin as A
    from app import app
    store = {"inv": inv, "usage": usage, "audit": []}
    monkeypatch.setattr(A, "load_inventory", lambda: store["inv"])
    monkeypatch.setattr(A, "save_inventory", lambda d: store.update(inv=d))
    monkeypatch.setattr(it, "load_usage", lambda: store["usage"])
    monkeypatch.setattr(it, "save_usage", lambda d: store.update(usage=d))
    monkeypatch.setattr(it, "load_po_delivery", lambda: tracking)
    monkeypatch.setattr(it, "append_inventory_audit",
                        lambda rows, **k: store["audit"].extend(rows))
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    return app.test_client(), store


def _absorbed_state():
    inv = _inv()
    for i in inv.values():
        i["on_order"] = []
    return inv, _absorbed_rows()


def test_restore_dry_run_changes_nothing(monkeypatch):
    inv, usage = _absorbed_state()
    c, store = _admin_client(monkeypatch, inv, usage, _tracking())
    r = c.post("/api/admin/restore-absorbed-po", headers={"X-Inventory-Token": "tok"},
               json={"po_number": PO, "count_at": COUNT_AT}).get_json()
    assert r["ok"] and r["dry_run"] and r["count_missed"]
    assert r["restored_cs"] == 1120 and len(r["lines"]) == 12
    assert _total(store["inv"]) == 404
    assert all(u["source"] == "on_order_absorbed" for u in store["usage"])


def test_restore_live_puts_the_cases_back_and_keeps_them(monkeypatch):
    inv, usage = _absorbed_state()
    c, store = _admin_client(monkeypatch, inv, usage, _tracking())
    h = {"X-Inventory-Token": "tok"}
    body = {"po_number": PO, "count_at": COUNT_AT, "dry_run": False}
    assert c.post("/api/admin/restore-absorbed-po", headers=h, json=body).status_code == 400
    r = c.post("/api/admin/restore-absorbed-po", headers=h,
               json=dict(body, reason="Tom's 10/19 screen predates USF receiving")).get_json()
    assert r["ok"] and not r["dry_run"] and r["restored_cs"] == 1120
    assert _total(store["inv"]) == 404 + 1120
    rows = store["usage"]
    assert {u["source"] for u in rows} == {"on_order_rollover"}
    plain = next(u for u in rows if u["item_key"] == PLAIN)
    assert plain["amount"] == -224 and plain["not_in_count_at"] == COUNT_AT
    assert plain["delivery_confirmed"] is True and "absorbed by" in plain["absorbed_note"]
    assert sync._receipts_after_count(rows, PLAIN, COUNT_AT) == 224
    assert len(store["audit"]) == 12
    # nothing left to restore -> refused, not doubled
    again = c.post("/api/admin/restore-absorbed-po", headers=h,
                   json=dict(body, reason="again"))
    assert again.status_code == 404 and _total(store["inv"]) == 404 + 1120


def test_restore_refuses_when_the_count_could_hold_the_delivery(monkeypatch):
    inv, usage = _absorbed_state()
    for v, (h, o, w) in SCREEN.items():
        inv[_key(v)]["last_count_qty"] = float(h + o)
    c, store = _admin_client(monkeypatch, inv, usage, _tracking())
    r = c.post("/api/admin/restore-absorbed-po", headers={"X-Inventory-Token": "tok"},
               json={"po_number": PO, "count_at": COUNT_AT})
    assert r.status_code == 409


def test_restore_leaves_a_sku_counted_again_since(monkeypatch):
    inv, usage = _absorbed_state()
    inv[PLAIN]["last_count_at"] = "2026-10-26T15:00:00Z"
    c, store = _admin_client(monkeypatch, inv, usage, _tracking())
    r = c.post("/api/admin/restore-absorbed-po", headers={"X-Inventory-Token": "tok"},
               json={"po_number": PO, "count_at": COUNT_AT}).get_json()
    assert r["restored_cs"] == 1120 - 224
    assert any("Plain" in s for s in r["skipped"])
