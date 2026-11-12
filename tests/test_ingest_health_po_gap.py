"""PO cadence alert + stale-issue expiry in ingest_health.run_checks()
(feature C, 2026-11-12)."""
from datetime import date, datetime, timedelta, timezone

import inventory_tracker as it
from core import feed_health
from integrations import ingest_health as IH

from ingest_fakes import isolate, sku

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)


def _days(*offsets):
    """PO dates as days-before-NOW, oldest first."""
    return sorted(NOW.date() - timedelta(days=o) for o in offsets)


def test_verdict_needs_history_and_both_thresholds():
    today = NOW.date()
    # Weekly orderer, last PO 16 days ago: > 2 x 7 and > 14 -> overdue.
    v = IH.po_gap_verdict(_days(44, 37, 30, 23, 16), today)
    assert v["overdue"] and v["median_gap_days"] == 7 and v["days_since_last"] == 16
    # Same cadence, 13 days: more than twice the gap but not past 14 days.
    assert not IH.po_gap_verdict(_days(41, 34, 27, 20, 13), today)["overdue"]
    # Monthly orderer, 40 days: past 14 days but not twice its 30-day gap.
    assert not IH.po_gap_verdict(_days(160, 130, 100, 70, 40), today)["overdue"]
    assert IH.po_gap_verdict(_days(185, 155, 125, 95, 65), today)["overdue"]
    # Fewer than 4 gaps: no opinion.
    v = IH.po_gap_verdict(_days(60, 50, 40, 30), today)
    assert not v["overdue"] and v["reason"] == "not enough history"


def test_po_days_come_from_the_ledger_and_skip_canceled():
    ledger = [
        {"po_number": "1", "distributor": "US Foods", "warehouse": "Zebulon, NC",
         "ordered_at": "2026-10-01T00:00:00", "status": "arrived"},
        {"po_number": "2", "distributor": "US Foods", "warehouse": "Zebulon, NC",
         "ordered_at": "2026-10-01", "status": "arrived"},          # same day
        {"po_number": "3", "distributor": "US Foods", "warehouse": "Zebulon, NC",
         "ordered_at": "", "arrival_date": "2026-10-20T00:00:00", "status": "arrived"},
        {"po_number": "4", "distributor": "US Foods", "warehouse": "Zebulon, NC",
         "ordered_at": "2026-11-01", "status": "canceled"},
        {"po_number": "5", "distributor": "Chefs Warehouse", "warehouse": "Bronx, NY",
         "ordered_at": "2026-11-02", "status": "pending"},
        {"po_number": "6", "distributor": "US Foods", "warehouse": "",
         "ordered_at": "2026-11-02", "status": "pending"},
    ]
    got = IH.po_days_by_warehouse(ledger)
    assert got == {("US Foods", "Zebulon, NC"): [date(2026, 10, 1), date(2026, 10, 20)],
                   ("Chefs Warehouse", "Bronx, NY"): [date(2026, 11, 2)]}


def _inv_with_pos(order_offsets, warehouse="Zebulon, NC", short="Zebulon"):
    key, rec = sku("Plain", short, warehouse)
    rec["on_order"] = [
        {"po_number": f"PO{o}", "qty": 56,
         "ordered_at": (NOW - timedelta(days=o)).strftime("%Y-%m-%dT00:00:00"),
         "eta": "2027-01-01T00:00:00"} for o in order_offsets]
    return {key: rec}


def _setup(tmp_path, monkeypatch, inv):
    isolate(tmp_path, monkeypatch, inv)
    it._write_json(tmp_path / "usage.json", [])
    monkeypatch.delenv("PO_GAP_IGNORE", raising=False)


def test_run_checks_raises_then_clears_po_gap(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _inv_with_pos([44, 37, 30, 23, 16]))
    out = IH.check_po_cadence(now=NOW)
    assert out["Zebulon, NC"]["overdue"]
    issue = feed_health.snapshot()["issues"]["po-gap:Zebulon, NC"]
    assert "no purchase order for 16 days" in issue["message"]
    # A PO arrives: the issue closes on the next check.
    it._write_json(tmp_path / "inventory.json", _inv_with_pos([44, 37, 30, 23, 16, 1]))
    it._FILE_CACHE.clear()
    assert not IH.check_po_cadence(now=NOW)["Zebulon, NC"]["overdue"]
    assert "po-gap:Zebulon, NC" not in feed_health.snapshot()["issues"]


def test_ignore_list_and_run_checks_never_raises(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _inv_with_pos([440, 370, 300, 230, 160]))
    out = IH.run_checks()
    assert out["ok"] is True and out["checks"]["po_cadence"]["Zebulon, NC"]["overdue"]
    assert feed_health.snapshot()["feeds"]["ingest-checks"]["ok"] is True
    monkeypatch.setenv("PO_GAP_IGNORE", "Alcoa, TN; zebulon, nc")
    IH.run_checks()
    assert "po-gap:Zebulon, NC" not in feed_health.snapshot()["issues"]
    monkeypatch.setenv("PO_GAP_CHECK", "off")
    assert IH.run_checks()["checks"]["po_cadence"] == "disabled"
    # A check that blows up is contained and reported.
    monkeypatch.delenv("PO_GAP_CHECK")
    monkeypatch.setattr(IH, "check_po_cadence",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = IH.run_checks()
    assert out["ok"] is False and out["errors"] == ["po_cadence: RuntimeError"]
    assert feed_health.snapshot()["feeds"]["ingest-checks"]["ok"] is False


def test_stale_apply_issues_expire_and_held_report_closes_after_a_count(tmp_path, monkeypatch):
    key, rec = sku("Plain", "Zebulon", "Zebulon, NC")
    _setup(tmp_path, monkeypatch, {key: rec})
    feed_health.raise_issue("report-held:Zebulon, NC:<m1>", "held")
    feed_health.raise_issue("onhand-rejected:plain:<m2>", "rejected")
    feed_health.raise_issue("po-parse:<m3>", "not ours to expire")
    assert IH.expire_stale_issues(now=datetime.now(timezone.utc)) == []
    # 16 days on, nothing re-raised them.
    later = datetime.now(timezone.utc) + timedelta(days=16)
    assert sorted(IH.expire_stale_issues(now=later)) == [
        "onhand-rejected:plain:<m2>", "report-held:Zebulon, NC:<m1>"]
    assert list(feed_health.snapshot()["issues"]) == ["po-parse:<m3>"]
    # A held report closes as soon as its warehouse has a newer count.
    feed_health.raise_issue("report-held:Zebulon, NC:<m4>", "held")
    rec["last_count_at"] = (datetime.now(timezone.utc)
                            + timedelta(hours=1)).isoformat()
    it._write_json(tmp_path / "inventory.json", {key: rec})
    it._FILE_CACHE.clear()
    assert IH.expire_stale_issues() == ["report-held:Zebulon, NC:<m4>"]
