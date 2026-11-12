"""On-hand sanity: whole-cell numbers, hard bounds, and the plausibility hold
(review 2026-11-12, item 4)."""
import pytest

import inventory_tracker as it
import sync_inventory as sync
from integrations import bagel_inventory_worksheet as W
from integrations import email_scanner as ES
from integrations import usfoods_inventory_report as R
from integrations.base import SyncItem

from ingest_fakes import isolate, mime, sku

VARIETIES = ["Plain", "Everything", "Sesame", "Onion", "Egg", "Asiago",
             "Blueberry", "Poppy Seed"]
WH = "Zebulon, NC"
PREV = "2026-11-02T14:00:00Z"
MSG = "<report-1109@usfoods.com>"


@pytest.mark.parametrize("mod", [R, W])
def test_to_float_takes_whole_cell_numbers_only(mod):
    f = mod._to_float
    assert f("23") == 23.0 and f(" 1,204 ") == 1204.0 and f("5.39") == 5.39
    assert f(17) == 17.0 and f("-4") == -4.0
    for bad in ("12 (3 on hold)", "N/A", "6/10", "", None, "23 cs", "nan",
                float("nan"), float("inf"), True):
        assert f(bad) is None, bad


def test_report_names_the_non_numeric_cell_and_keeps_the_other_rows():
    html = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>CURRENT ON HAND</td>"
            "<td>Forecast 11/8/2026</td></tr>"
            "<tr><td>7095637</td><td>1150</td><td>12 (3 on hold)</td><td>22</td></tr>"
            "<tr><td>1055010</td><td>1184</td><td>N/A</td><td>5</td></tr>"
            "<tr><td>7309056</td><td>1158</td><td>101</td><td>25</td></tr></table>")
    rep = R.parse_report_html(html)
    assert [l.cases_on_hand for l in rep.lines] == [101.0]
    assert len(rep.bad_cells) == 2 and "12 (3 on hold)" in rep.bad_cells[0]
    events, errors, _ = ES.parse_message_with_errors(
        mime("maria.hernandez@usfoods.com", "Weekly report", html=html))
    assert [e.item.quantity for e in events] == [101.0]
    assert any("not a number" in e and "N/A" in e for e in errors)


def _inv(prev_qty=40.0, unit="cs"):
    inv = dict(sku(v, "Zebulon", WH, qty=prev_qty, last_count_at=PREV,
                   last_count_qty=prev_qty, unit=unit) for v in VARIETIES)
    return inv


def _evt(variety, qty, count_date="2026-11-09T14:00:00Z", msg=MSG, unit="cs"):
    return ES.EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=qty, distributor="US Foods", variety=variety,
                      warehouse=WH, unit=unit, case_size=60),
        source_message_id=msg, source_subject="Weekly report", count_date=count_date)


def _apply_one(qty, inv=None):
    inv = inv or _inv()
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    sync._apply_email_event(_evt("Plain", qty), inv, [], "2026-11-09T15:00:00",
                            report, dry_run=False)
    key = "plain bagel 4oz [usf - zebulon]"
    return inv[key], report


@pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan"), 7095637.0,
                                 12345678.0, 2001.0])
def test_impossible_on_hand_is_rejected_and_flagged(bad):
    item, report = _apply_one(bad)
    assert item["quantity"] == 40.0 and item["last_count_at"] == PREV
    assert len(report["rejected"]) == 1 and "refused" in report["rejected"][0]
    assert report["issues"][0]["key"].startswith("onhand-rejected:plain bagel")
    assert MSG in report["issues"][0]["key"]


def test_relative_ceiling_scales_with_the_last_count():
    item, report = _apply_one(2500.0, _inv(prev_qty=300.0))   # 10*300+200 = 3200
    assert item["quantity"] == 2500.0 and not report.get("rejected")
    item, report = _apply_one(3300.0, _inv(prev_qty=300.0))
    assert item["quantity"] == 300.0 and report["rejected"]


def test_plausible_values_and_each_units_still_apply():
    item, report = _apply_one(0.0)
    assert item["quantity"] == 0.0 and not report.get("rejected")
    item, report = _apply_one(1999.0)
    assert item["quantity"] == 1999.0
    item, report = _apply_one(28800.0, _inv(unit="each"))   # 480 cs in eaches
    assert item["quantity"] == 28800.0 and not report.get("rejected")


def _run(tmp_path, monkeypatch, events, inv=None):
    isolate(tmp_path, monkeypatch, inv or _inv())
    it._write_json(tmp_path / "usage.json", [])
    return sync._apply_events(events, dry_run=False)


def test_report_repeating_last_weeks_count_is_held(tmp_path, monkeypatch):
    # 6 of 8 identical to the previous count, 7 days later.
    events = [_evt(v, 40.0 if i < 6 else 31.0) for i, v in enumerate(VARIETIES)]
    rep = _run(tmp_path, monkeypatch, events)
    inv = it.load_inventory()
    assert all(i["last_count_at"] == PREV and i["quantity"] == 40.0
               for i in inv.values())
    assert rep["updated"] == 0
    held = rep["held_reports"][0]
    assert held["warehouse"] == WH and held["equal"] == 6 and held["compared"] == 8
    assert rep["issues"][0]["key"] == f"report-held:{WH}:{MSG}"
    assert "report held: " in rep["rejected"][0]


def test_hold_needs_all_three_conditions(tmp_path, monkeypatch):
    # Only 4 of 8 equal: applied.
    events = [_evt(v, 40.0 if i < 4 else 31.0) for i, v in enumerate(VARIETIES)]
    rep = _run(tmp_path, monkeypatch, events)
    assert not rep.get("held_reports") and rep["updated"] == 4
    # All equal but only 2 days after the previous count: applied (unchanged).
    events = [_evt(v, 40.0, count_date="2026-11-04T14:00:00Z") for v in VARIETIES]
    rep = _run(tmp_path, monkeypatch, events)
    assert not rep.get("held_reports")
    assert it.load_inventory()["plain bagel 4oz [usf - zebulon]"][
        "last_count_at"] == "2026-11-04T14:00:00Z"
    # Fewer than 6 SKUs: applied.
    rep = _run(tmp_path, monkeypatch, [_evt(v, 40.0) for v in VARIETIES[:5]])
    assert not rep.get("held_reports")
    # Out of stock both weeks is not evidence of a repeat.
    rep = _run(tmp_path, monkeypatch, [_evt(v, 0.0) for v in VARIETIES],
               inv=_inv(prev_qty=0.0))
    assert not rep.get("held_reports")


def test_released_report_is_applied_and_env_switch_disables(tmp_path, monkeypatch):
    events = [_evt(v, 40.0) for v in VARIETIES]
    isolate(tmp_path, monkeypatch, _inv())
    it._write_json(tmp_path / "usage.json", [])
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    assert sync._apply_events(events, dry_run=False)["held_reports"]
    from app import app
    r = app.test_client().post("/api/email/held-reports/release",
                               json={"message_id": MSG},
                               headers={"X-Inventory-Token": "tok"})
    assert r.get_json()["released"] == [MSG]
    rep = sync._apply_events(events, dry_run=False)
    assert not rep.get("held_reports")
    assert it.load_inventory()["plain bagel 4oz [usf - zebulon]"][
        "last_count_at"] == "2026-11-09T14:00:00Z"
    # And the kill switch.
    isolate(tmp_path, monkeypatch, _inv())
    monkeypatch.setenv("PLAUSIBILITY_HOLD", "off")
    assert not sync._apply_events(
        [_evt(v, 40.0, msg="<other>") for v in VARIETIES],
        dry_run=False).get("held_reports")


def test_route_raises_feed_health_issue_for_a_rejected_on_hand(tmp_path, monkeypatch):
    from core import feed_health
    from ingest_fakes import route_scan
    res = ES.ScanResult(source="ms365", messages_seen=1, messages_parsed=1,
                        events=[_evt("Plain", 7095637.0)])
    _c, r, _ = route_scan(monkeypatch, tmp_path, res, inventory=_inv())
    rep = r.get_json()["reports"][0]
    assert rep["rejected"] and rep["updated"] == 0
    assert any(k.startswith("onhand-rejected:") for k in feed_health.snapshot()["issues"])
