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
    from blueprints import reporting as RP
    # The app's "today" is the bakery's (Eastern) day; these tests use the
    # system date, so pin the two together.
    monkeypatch.setattr(RP, "business_today", lambda: _d.date.today())
    return app.test_client(), guid, calls


def _view(c, guid, day):
    r = c.get(f"/api/report/toast-sales?period=week&end_date={day}&location={guid}"
              f"&live=1", headers=H)
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


# --- review 2026-11-12 -------------------------------------------------------

def test_opening_the_tab_serves_the_store_and_never_calls_toast(tmp_path, monkeypatch):
    today = _d.date.today().isoformat()
    c, guid, calls = _setup(tmp_path, monkeypatch, lambda pairs, n: ([], set()))
    it.save_sales([_row(guid, today, 7)])
    r = c.get(f"/api/report/toast-sales?period=week&end_date={today}&location={guid}",
              headers=H).get_json()
    assert calls == [] and r["fetch"]["live"] is False
    assert sum(i["qty"] for b in r["buckets"] for i in b["items"]) == 7
    assert r["store"]["location_days_not_pulled"] >= 0 and "oldest_fetched_at" in r["store"]


def test_all_failed_refresh_says_so_instead_of_no_new_orders(tmp_path, monkeypatch):
    past = (_d.date.today() - _d.timedelta(days=9)).isoformat()
    c, guid, calls = _setup(tmp_path, monkeypatch, lambda pairs, n: ([], set()))
    r = c.get(f"/api/report/toast-sales?period=week&end_date={past}&location={guid}&live=1",
              headers=H).get_json()
    n = len(calls[0])
    assert r["fetch"]["fetch_error"] == f"{n} of {n} location-days failed"
    assert r["fetch"]["failed"] == n and r["fetch"]["requested"] == n


def test_refresh_stamps_fetched_at_per_location_day(tmp_path, monkeypatch):
    past = (_d.date.today() - _d.timedelta(days=9)).isoformat()

    def answer(pairs, n):
        return [_row(g, d, 5) for (g, d, _n) in pairs], {(g, d) for (g, d, _n) in pairs}
    c, guid, _calls = _setup(tmp_path, monkeypatch, answer)
    r = c.get(f"/api/report/toast-sales?period=week&end_date={past}&location={guid}&live=1",
              headers=H).get_json()
    from blueprints import reporting as RP
    log = RP._load_toast_fetch_log()
    assert f"{guid}|{past}" in log
    assert r["store"]["newest_fetched_at"] and r["store"]["location_days_not_pulled"] == 0


def _nightly(c, **body):
    r = c.post("/api/toast/nightly-pull", json=body, headers=H)
    return r.status_code, r.get_json()


def test_nightly_pull_fetches_trailing_days_for_active_and_prelaunch(tmp_path, monkeypatch):
    from core import feed_health

    def answer(pairs, n):
        return [_row(g, d, 3) for (g, d, _n) in pairs], {(g, d) for (g, d, _n) in pairs}
    c, guid, calls = _setup(tmp_path, monkeypatch, answer)
    code, out = _nightly(c)
    assert code == 200 and out["ok"] and out["failed"] == 0 and out["remaining"] == 0
    asked = {(p[0], p[1]) for call in calls for p in call}
    today = _d.date.today()
    trailing = {(today - _d.timedelta(days=k)).isoformat() for k in range(1, 5)}
    stores = {L["restaurant_guid"]: (L.get("status") or "active")
              for L in it.TOAST_RETAIL_LOCATIONS}
    fetchable = {g for g, st in stores.items() if st in ("active", "pre_launch")}
    assert {(g, d) for g in fetchable for d in trailing} <= asked
    assert today.isoformat() not in {d for _g, d in asked}       # the open day is not pulled
    assert any(st == "pre_launch" for st in stores.values())
    h = feed_health.snapshot()["feeds"]["toast-nightly"]
    assert h["ok"] and h["fetched"] == out["fetched"] and h["failed"] == 0
    from blueprints import reporting as RP
    assert len(RP._load_toast_fetch_log()) == out["fetched"]
    # an immediate second run (the other worker's scheduler) pulls nothing again
    n = len(calls)
    code, out2 = _nightly(c)
    assert out2["requested"] == 0 and len(calls) == n


def test_nightly_pull_repulls_a_day_fetched_before_it_was_final(tmp_path, monkeypatch):
    from blueprints import reporting as RP
    today = _d.date.today()
    day = (today - _d.timedelta(days=9)).isoformat()
    final_day = (today - _d.timedelta(days=10)).isoformat()

    def answer(pairs, n):
        return [_row(g, d, 99) for (g, d, _n) in pairs], {(g, d) for (g, d, _n) in pairs}
    c, guid, calls = _setup(tmp_path, monkeypatch, answer)
    it.save_sales([_row(guid, day, 40), _row(guid, final_day, 40)])
    RP._save_toast_fetch_log({
        f"{guid}|{day}": (today - _d.timedelta(days=8)).isoformat() + "T08:15:00Z",       # early
        f"{guid}|{final_day}": (today - _d.timedelta(days=2)).isoformat() + "T08:15:00Z",  # final
    })
    targets = RP.toast_nightly_targets(today, it.load_sales(), RP._load_toast_fetch_log())
    mine = {d for (g, d, _n, _s) in targets if g == guid}
    assert day in mine and final_day not in mine
    _nightly(c)
    assert [r["qty"] for r in it.load_sales()
            if r["restaurant_guid"] == guid and r["business_date"] == day] == [99]


def test_nightly_pull_reports_failures_and_keeps_what_completed(tmp_path, monkeypatch):
    from core import feed_health
    active = [L["restaurant_guid"] for L in it.TOAST_RETAIL_LOCATIONS
              if (L.get("status") or "active") == "active"]
    bad = active[0]

    def answer(pairs, n):
        good = [(g, d) for (g, d, _n) in pairs if g != bad]
        return [_row(g, d, 3) for (g, d) in good], set(good)
    c, guid, _calls = _setup(tmp_path, monkeypatch, answer)
    code, out = _nightly(c)
    assert code == 200 and out["ok"] is False and out["failed"] > 0 and out["fetched"] > 0
    assert out["fetch_error"] == f"{out['failed']} of {out['requested']} location-days failed"
    assert all(p["restaurant_guid"] == bad for p in out["failed_pairs"])
    h = feed_health.snapshot()["feeds"]["toast-nightly"]
    assert h["ok"] is False and "failed" in h["detail"]
    from blueprints import reporting as RP
    assert not any(k.startswith(bad + "|") for k in RP._load_toast_empty_days())


def test_nightly_pull_stops_inside_its_budget_and_says_what_is_left(tmp_path, monkeypatch):
    from blueprints import reporting as RP
    clock = {"t": 0.0}
    monkeypatch.setattr(RP._time, "monotonic", lambda: clock["t"])

    def answer(pairs, n):
        clock["t"] += 50.0                       # every batch "takes" 50 s
        return [], {(g, d) for (g, d, _n) in pairs}
    c, guid, calls = _setup(tmp_path, monkeypatch, answer)
    code, out = _nightly(c, max_seconds=120)
    assert out["remaining"] > 0 and out["remaining_pairs"] and len(calls) == 3
    assert out["fetched"] + out["remaining"] == out["requested"]


def test_nightly_pull_without_credentials_is_reported(tmp_path, monkeypatch):
    from core import feed_health
    c, guid, calls = _setup(tmp_path, monkeypatch, lambda pairs, n: ([], set()))
    monkeypatch.setattr(toast_api, "is_configured", lambda: False)
    code, out = _nightly(c)
    assert code == 503 and out["error"] == "toast_not_configured" and calls == []
    assert feed_health.snapshot()["feeds"]["toast-nightly"]["ok"] is False


# --- toast_api client --------------------------------------------------------

import urllib.error  # noqa: E402


def _http(code, hdrs=None):
    return urllib.error.HTTPError("u", code, "err", hdrs or {}, None)


def _client(monkeypatch, seq):
    monkeypatch.setenv("TOAST_CLIENT_ID", "x"); monkeypatch.setenv("TOAST_CLIENT_SECRET", "y")
    monkeypatch.setenv("TOAST_API_HOSTNAME", "https://toast.example")
    monkeypatch.setattr(toast_api, "_access_token", lambda: "tok")
    sleeps, n = [], {"i": 0}

    class _R:
        def __init__(self, b): self.b = b
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=None):
        item = seq[min(n["i"], len(seq) - 1)]; n["i"] += 1
        if isinstance(item, Exception):
            raise item
        return _R(item)
    monkeypatch.setattr(toast_api.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(toast_api.time, "sleep", lambda s: sleeps.append(s))
    return sleeps, n


def test_a_404_is_a_failure_not_an_empty_day(monkeypatch):
    _client(monkeypatch, [_http(404)])
    import pytest
    with pytest.raises(toast_api.ToastAPIError):
        toast_api.fetch_product_mix("g", "2026-11-10")


def test_429_is_retried_honouring_retry_after(monkeypatch):
    sleeps, n = _client(monkeypatch, [_http(429, {"Retry-After": "3"}), b"[]"])
    assert toast_api.fetch_product_mix("g", "2026-11-10") == []
    assert sleeps == [3.0] and n["i"] == 2


def test_batch_timeout_bounds_the_call_and_reports_failed_pairs(monkeypatch):
    import threading, time as _t
    gate = threading.Event()

    def slow(guid, date, name=""):
        if guid == "slow":
            gate.wait(5)
            return []
        if guid == "boom":
            raise toast_api.ToastAPIError("nope")
        return [{"restaurant_guid": guid, "business_date": date}]
    monkeypatch.setattr(toast_api, "fetch_product_mix", slow)
    t0 = _t.time()
    res = toast_api.fetch_product_mix_batch_detailed(
        [("ok", "2026-11-10", "A"), ("boom", "2026-11-10", "B"), ("slow", "2026-11-10", "C")],
        max_workers=3, timeout_s=0.3)
    took = _t.time() - t0
    gate.set()
    assert took < 2.0                                   # did not wait for the straggler
    assert res["ok_pairs"] == {("ok", "2026-11-10")} and len(res["rows"]) == 1
    assert {p[0] for p in res["failed_pairs"]} == {"boom", "slow"}
    assert res["error"] == "2 of 3 location-days failed" and res["timed_out"]
