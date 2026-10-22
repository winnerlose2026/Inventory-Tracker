"""Report tab "Top Consumed": today and yesterday are always re-pulled from
Toast (and replace what was stored); older days Toast confirmed empty are not
re-requested on every view; a failed pull is retried (review 2026-10-22).

Before, any stored row for a (location, day) marked it done forever, so a 9am
view froze that day at the 9am numbers."""
import datetime as _d
import sys
from pathlib import Path

sys.path.insert(0, ".")

import inventory_tracker as it
from integrations import toast_api

H = {"X-Inventory-Token": "tok"}


def _isolate(tmp: Path, monkeypatch):
    old = it.DATA_DIR
    monkeypatch.setattr(it, "DATA_DIR", tmp)
    for name in dir(it):
        v = getattr(it, name)
        # Earlier tests re-point it.DATA_DIR without restoring it, so match
        # the *_FILE constants by name as well as by directory.
        if isinstance(v, Path) and (v.parent == old or name.endswith("_FILE")):
            monkeypatch.setattr(it, name, tmp / v.name)


def _row(guid, day, qty, item="plain"):
    return {"restaurant_guid": guid, "location": "X", "business_date": day,
            "item_guid": item, "item": "Plain Bagel", "menu_group": "Bagels",
            "qty": qty, "gross": 0, "net": 0}


def _setup(tmp_path, monkeypatch, answer):
    """answer(pairs, call_no) -> (rows, ok_pairs)"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    calls = []
    monkeypatch.setattr(toast_api, "is_configured", lambda: True)

    def batch(pairs, max_workers=6, timeout_s=28.0):
        pairs = list(pairs)
        calls.append(pairs)
        return answer(pairs, len(calls))
    monkeypatch.setattr(toast_api, "fetch_product_mix_batch_with_status", batch)
    guid = next(L["restaurant_guid"] for L in it.TOAST_RETAIL_LOCATIONS
                if (L.get("status") or "active") == "active")
    from app import app
    return app.test_client(), guid, calls


def _view(c, guid, day):
    r = c.get(f"/api/report/toast-sales?period=week&end_date={day}&location={guid}",
              headers=H)
    j = r.get_json()
    return sum(i.get("qty") or 0 for b in j.get("buckets", []) for i in b.get("items", []))


def test_today_is_refetched_and_replaced(tmp_path, monkeypatch):
    today = _d.date.today().isoformat()

    def answer(pairs, n):
        rows = [_row(g, d, 40 if n == 1 else 400) for (g, d, _name) in pairs if d == today]
        return rows, {(g, d) for (g, d, _name) in pairs}
    c, guid, calls = _setup(tmp_path, monkeypatch, answer)
    from blueprints import reporting as RP
    RP._TOAST_REFRESHED.clear()
    assert _view(c, guid, today) == 40          # morning
    # A view a minute later is served from the store (per-worker throttle) ...
    n = len(calls)
    assert _view(c, guid, today) == 40
    assert not any(p[0] == guid and p[1] == today for call in calls[n:] for p in call)
    # ... and once the throttle window has passed, today is re-pulled.
    monkeypatch.setattr(RP, "_TOAST_REFRESH_S", 0)
    assert _view(c, guid, today) == 400         # evening: re-pulled, replaced
    assert any(p[0] == guid and p[1] == today for p in calls[-1])
    stored = [r for r in it.load_sales() if r["business_date"] == today]
    assert [r["qty"] for r in stored] == [400]  # replaced, not appended


def test_older_empty_days_are_marked_and_not_refetched(tmp_path, monkeypatch):
    today = _d.date.today()
    week_ago = today - _d.timedelta(days=9)     # a fully-past day, not yesterday

    def answer(pairs, n):
        return [], {(g, d) for (g, d, _name) in pairs}   # completed, no orders
    c, guid, calls = _setup(tmp_path, monkeypatch, answer)
    _view(c, guid, week_ago.isoformat())
    first = {p[1] for p in calls[0] if p[0] == guid}
    assert week_ago.isoformat() in first
    _view(c, guid, week_ago.isoformat())
    later = {p[1] for call in calls[1:] for p in call if p[0] == guid}
    assert week_ago.isoformat() not in later    # marked empty -> not re-asked


def test_a_failed_pull_is_not_marked_empty(tmp_path, monkeypatch):
    past = (_d.date.today() - _d.timedelta(days=9)).isoformat()

    def answer(pairs, n):
        return [], set()                         # every request failed
    c, guid, calls = _setup(tmp_path, monkeypatch, answer)
    _view(c, guid, past)
    _view(c, guid, past)
    assert sum(1 for call in calls for p in call if p[0] == guid and p[1] == past) == 2


def test_failed_refresh_keeps_todays_stored_rows(tmp_path, monkeypatch):
    today = _d.date.today().isoformat()

    def answer(pairs, n):
        if n == 1:
            return [_row(g, d, 40) for (g, d, _n) in pairs if d == today], \
                {(g, d) for (g, d, _n) in pairs}
        return [], set()                         # Toast down on the 2nd view
    c, guid, _calls = _setup(tmp_path, monkeypatch, answer)
    from blueprints import reporting as RP
    monkeypatch.setattr(RP, "_TOAST_REFRESH_S", 0)
    assert _view(c, guid, today) == 40
    assert _view(c, guid, today) == 40
