#!/usr/bin/env python3
"""Regression tests for the scan-health / count-date / body-only logic
(roadmap #4). Runs under pytest OR standalone (`python3 test_scan_health.py`).
All offline -- no network, no Graph, no disk inventory required.
"""
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

sys.path.insert(0, ".")

from integrations.email_scanner import (
    _msg_event_candidate, _msg_date_iso, parse_message_with_errors, EmailEvent,
)
from integrations.email_scanner import SyncItem  # re-exported
from sync_inventory import _apply_email_event
from inventory_tracker import warehouse_freshness, STALE_COUNT_DAYS


def test_msg_event_candidate_matches_distributor_and_rep_not_noise():
    zeb = {"from": {"emailAddress": {"address": "maria.hernandez@usfoods.com"}},
           "toRecipients": [{"emailAddress": {"address": "JD@hhbagels.com"}}]}
    reply_to_dist = {"from": {"emailAddress": {"address": "info@hhbagels.com"}},
                     "toRecipients": [{"emailAddress": {"address": "rep@cheneybrothers.com"}}]}
    noise = {"from": {"emailAddress": {"address": "news@ordoro.com"}},
             "toRecipients": [{"emailAddress": {"address": "info@hhbagels.com"}}]}
    assert _msg_event_candidate(zeb) is True
    assert _msg_event_candidate(reply_to_dist) is True
    assert _msg_event_candidate(noise) is False


def test_msg_date_iso_parses_and_handles_missing():
    m = EmailMessage(); m["Date"] = "Mon, 15 Jun 2026 14:38:19 +0000"
    assert _msg_date_iso(m).startswith("2026-06-15T14:38:19")
    assert _msg_date_iso(EmailMessage()) == ""


def test_on_hand_uses_count_date_not_ingest_time():
    evt = EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=42.0, distributor="US Foods", variety="Plain",
                      warehouse="Zebulon, NC", name="Plain Bagel 4oz [USF - Zebulon]",
                      unit="cs", case_size=60, price=0.0, case_cost=27.0,
                      weekly_usage=None),
        count_date="2026-06-15T14:38:19+00:00",
    )
    key = "plain bagel 4oz [usf - zebulon]"
    inv = {key: {"name": "Plain Bagel 4oz [USF - Zebulon]", "quantity": 999.0,
                 "warehouse": "Zebulon, NC", "distributor": "US Foods",
                 "variety": "Plain", "unit": "cs", "case_size": 60, "price": 0.0,
                 "case_cost": 27.0, "weekly_usage": 0.0, "on_order": [],
                 "updated": "", "last_synced": "", "last_count_at": "2026-06-22T16:00:00"}}
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    _apply_email_event(evt, inv, [], "2026-06-22T23:59:59", report, dry_run=False)
    it = inv[key]
    assert report["unmatched"] == [], "event should have matched"
    assert it["last_count_at"] == "2026-06-15T14:38:19+00:00", it["last_count_at"]
    assert it["last_synced"] == "2026-06-22T23:59:59", "sync time stays ingest time"


def test_newest_report_wins_ordering():
    older = EmailEvent(event_type="on_hand", item=SyncItem(quantity=1, distributor="x"),
                       count_date="2026-06-08T10:00:00+00:00")
    newer = EmailEvent(event_type="on_hand", item=SyncItem(quantity=1, distributor="x"),
                       count_date="2026-06-15T14:38:19+00:00")
    ordered = sorted([newer, older], key=lambda e: getattr(e, "count_date", "") or "")
    assert ordered[-1] is newer, "newest count_date must be applied last (wins)"


def test_warehouse_freshness_flags_stale_and_missing():
    now = datetime(2026, 6, 22, 12, 0, 0)
    fresh_dt = (now - timedelta(days=2)).isoformat()
    stale_dt = (now - timedelta(days=10)).isoformat()
    inv = {
        "a": {"distributor": "US Foods", "warehouse": "Fresh", "last_count_at": fresh_dt},
        "b": {"distributor": "US Foods", "warehouse": "Stale", "last_count_at": stale_dt},
        "c": {"distributor": "Cheney Brothers", "warehouse": "Never", "last_count_at": ""},
    }
    rows = {r["warehouse"]: r for r in warehouse_freshness(now=now, inv=inv)}
    assert rows["Fresh"]["stale"] is False
    assert rows["Stale"]["stale"] is True
    assert rows["Never"]["stale"] is True and rows["Never"]["last_count_at"] is None
    assert rows["Fresh"]["days_since_count"] == 2.0


def test_rep_map_override_resolves_without_code_change(tmpfile=None):
    import os, json, tempfile
    from integrations import rep_map
    from integrations.usfoods_inventory_report import warehouse_for_sender as usf_wfs
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        json.dump({"NewRep@usfoods.com": ["US Foods", "Tampa, FL"]}, open(path, "w"))
        os.environ["REP_MAP_FILE"] = path
        rep_map._CACHE["key"] = None  # bust cache for the test
        assert usf_wfs("New Rep <newrep@usfoods.com>") == ("US Foods", "Tampa, FL")
        # a hardcoded rep still resolves (override only ADDS)
        assert usf_wfs("maria.hernandez@usfoods.com") == ("US Foods", "Zebulon, NC")
    finally:
        os.environ.pop("REP_MAP_FILE", None)
        rep_map._CACHE["key"] = None
        os.unlink(path)


def test_inventory_audit_append_and_cap():
    import tempfile, os
    from pathlib import Path as _P
    import inventory_tracker as it
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd)
    orig = it.INVENTORY_AUDIT_FILE
    it.INVENTORY_AUDIT_FILE = _P(path)
    try:
        it._FILE_CACHE.pop(str(path), None)
        it.append_inventory_audit([{"ts": "t1", "name": "A", "delta": 1}])
        it.append_inventory_audit([{"ts": "t2", "name": "B", "delta": 2}])
        rows = it.load_inventory_audit()
        assert rows[0]["ts"] == "t2", "newest first"
        assert len(rows) == 2
        it.append_inventory_audit([{"ts": "t%d" % i} for i in range(10)], cap=5)
        assert len(it.load_inventory_audit()) == 5, "capped"
    finally:
        it.INVENTORY_AUDIT_FILE = orig
        os.unlink(path)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"ok: {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL: {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
