"""Reporting blueprint — sales / Toast ingest + reports, traceability search,
PLH (productivity vs labor-hours), bakery sales, the forecast endpoints
(decrement-daily / backfill-historical / true-up), the summary report +
report-status pages, and the xlsx export. Extracted from app.py (refactor —
see docs/REFACTOR_PLAN.md). Shared helpers come from core/."""
import io
import time as _time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, send_file

from core.errors import _safe_err
from core.util import business_today, with_data_lock
from inventory_tracker import save_inventory_and_usage
from inventory_tracker import (
    load_inventory, load_usage, save_inventory, save_usage,
)

reporting_bp = Blueprint("reporting", __name__)


CASE_PRICE_BY_DISTRIBUTOR = {
    "US Foods":        27.00,
    "Cheney Brothers": 26.50,
}
CASE_PRICE_DEFAULT = 29.50
LABOR_RATE_DEFAULT = 17.00


def _case_price_for(distributor: str) -> float:
    return CASE_PRICE_BY_DISTRIBUTOR.get((distributor or "").strip(),
                                         CASE_PRICE_DEFAULT)


def _plh_bucket_keys(grain: str, offset: int = 0):
    """Return [(key, start_iso, end_iso, label)] for the buckets in scope.

    grain = "week"     -> 4 ISO weeks ending this week (Mon-Sun)
    grain = "month"    -> 4 trailing calendar months ending this month
    grain = "quarter"  -> the 4 quarters of a calendar year

    offset shifts the window backward by:
      week   -> 1 week per step  (4-week window slides one week at a time)
      month  -> 1 month per step (4-month window slides one month at a time)
      quarter-> 1 year per step  (4-quarter window jumps a full year)
    offset = 0 is the current window. Negative offsets are clamped to 0
    (no "future" windows past now).
    """
    from datetime import datetime, timedelta
    from core.util import business_today
    today = business_today()
    if offset < 0:
        offset = 0

    if grain == "month":
        # 4 trailing calendar months ending with the current month. Each
        # prev/next click steps the window back by 1 month (overlapping
        # navigation rather than jumping the whole window at once).
        cur_abs = today.year * 12 + (today.month - 1)
        # The newest month in this window:
        newest_abs = cur_abs - offset
        # Build oldest -> newest so the chart reads left to right naturally.
        out = []
        for i in range(3, -1, -1):
            abs_i = newest_abs - i
            yy = abs_i // 12
            mm = (abs_i % 12) + 1
            start = datetime(yy, mm, 1).date()
            nxt_abs = abs_i + 1
            n_yy = nxt_abs // 12
            n_mm = (nxt_abs % 12) + 1
            end = (datetime(n_yy, n_mm, 1).date() - timedelta(days=1))
            key   = start.strftime("%Y-%m")
            label = start.strftime("%b %Y")
            out.append((key, start.isoformat(), end.isoformat(), label))
        return out

    if grain == "quarter":
        anchor_year = today.year - offset
        out = []
        for q in range(1, 5):
            start_mm = (q - 1) * 3 + 1
            start = datetime(anchor_year, start_mm, 1).date()
            end_mm = start_mm + 3
            end_yy = anchor_year
            if end_mm > 12:
                end_mm = 1
                end_yy = anchor_year + 1
            end = (datetime(end_yy, end_mm, 1).date() - timedelta(days=1))
            key   = f"{anchor_year}-Q{q}"
            label = f"Q{q} {anchor_year}"
            out.append((key, start.isoformat(), end.isoformat(), label))
        return out

    # default: weekly. 4 ISO weeks anchored on the Monday `offset` weeks
    # back from this week (each prev/next click shifts the window by 1 week,
    # so navigation is smooth and overlapping rather than jumping a full
    # 4-week window at a time).
    day = today.weekday()        # Mon=0..Sun=6
    this_monday = today - timedelta(days=day)
    anchor_monday = this_monday - timedelta(days=7 * offset)
    out = []
    for i in range(3, -1, -1):    # 3 windows ago -> latest
        wk_start = anchor_monday - timedelta(days=7 * i)
        wk_end   = wk_start + timedelta(days=6)
        iso = wk_start.isocalendar()
        key   = f"{iso.year:04d}-W{iso.week:02d}"
        label = f"Week of {wk_start.strftime('%b %d')}"
        out.append((key, wk_start.isoformat(), wk_end.isoformat(), label))
    return out


def _plh_window_label(grain: str, buckets: list) -> str:
    """Human label for the chart header — e.g. 'Apr 20 – May 17, 2026',
    'Apr – Jun 2026', or 'Q1 – Q4 2026'."""
    if not buckets:
        return ""
    first_start = buckets[0][1]
    last_end    = buckets[-1][2]
    from datetime import date
    fs = date.fromisoformat(first_start)
    le = date.fromisoformat(last_end)
    if grain == "quarter":
        # Always within one year for this view.
        return f"Q1 – Q4 {fs.year}"
    if grain == "month":
        if fs.year == le.year:
            return f"{fs.strftime('%b')} – {le.strftime('%b')} {fs.year}"
        return f"{fs.strftime('%b %Y')} – {le.strftime('%b %Y')}"
    # week
    if fs.year == le.year:
        return f"{fs.strftime('%b %d')} – {le.strftime('%b %d, %Y')}"
    return f"{fs.strftime('%b %d, %Y')} – {le.strftime('%b %d, %Y')}"


def _date_in_range(d: str, start: str, end: str) -> bool:
    if not d:
        return False
    d = d[:10]
    return start <= d <= end


def _bucket_is_empty(start: str, end: str, prod, labor, bsales) -> bool:
    """Cheap check: does any production / labor / bakery-sales row fall in
    the [start, end] window? Used to skip 'we have not gotten there yet'
    periods when anchoring the chart's default view."""
    for r in prod:
        if _date_in_range(r.get("production_date") or "", start, end):
            return False
    for e in labor:
        if _date_in_range(e.get("date") or "", start, end):
            if (e.get("hours") or 0) > 0 or (e.get("dollars") or 0) > 0:
                return False
    for w in bsales:
        if _date_in_range(w.get("week_start") or "", start, end):
            if (w.get("total") or 0) > 0:
                return False
    return True


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


@reporting_bp.route("/api/sales/ingest", methods=["POST"])
@with_data_lock
def api_sales_ingest():
    """Accept Toast product-mix rows pushed by an external aggregator.

    Body: {
      "rows": [
        {"restaurant_guid": "...", "location": "UES",
         "business_date": "2026-05-12",
         "item_guid": "...", "item": "Plain Bagel",
         "menu_group": "Bagels",          # optional
         "qty": 124, "gross": 372.00, "net": 369.50}
      ],
      "replace_dates": false   # if true, wipe existing rows for the
                               # (location, date) pairs in this batch
                               # before appending. Useful to refresh a
                               # day without orphaning items removed
                               # from the catalog.
    }

    Dedupe key: (restaurant_guid, business_date, item_guid). Posting the
    same item for the same day overwrites the prior row.
    """
    from inventory_tracker import load_sales, save_sales
    body = request.json or {}
    rows = body.get("rows") or []
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "rows must be a list"}), 400
    replace_dates = bool(body.get("replace_dates", False))

    existing = load_sales()
    # Key existing rows.
    by_key = {(r.get("restaurant_guid"), r.get("business_date"), r.get("item_guid")): i
              for i, r in enumerate(existing)}

    if replace_dates:
        seed = set((r.get("restaurant_guid"), r.get("business_date")) for r in rows)
        existing = [r for r in existing
                    if (r.get("restaurant_guid"), r.get("business_date")) not in seed]
        by_key = {(r.get("restaurant_guid"), r.get("business_date"), r.get("item_guid")): i
                  for i, r in enumerate(existing)}

    added, updated = 0, 0
    for r in rows:
        if not r.get("restaurant_guid") or not r.get("business_date") or not r.get("item_guid"):
            continue
        key = (r.get("restaurant_guid"), r.get("business_date"), r.get("item_guid"))
        if key in by_key:
            existing[by_key[key]] = r
            updated += 1
        else:
            existing.append(r)
            by_key[key] = len(existing) - 1
            added += 1
    save_sales(existing)
    return jsonify({"ok": True, "added": added, "updated": updated,
                    "total_rows": len(existing)})


# Live (today / yesterday) Toast days: when each was last re-pulled, per
# worker process. In memory on purpose -- a throttle, not data.
_TOAST_REFRESHED: dict = {}
_TOAST_REFRESH_S = 15 * 60


def _toast_empty_days_path():
    from inventory_tracker import DATA_DIR
    return DATA_DIR / "toast_empty_days.json"


def _load_toast_empty_days() -> dict:
    """{"<restaurant_guid>|<YYYY-MM-DD>": marked_at} -- days Toast confirmed
    had no orders, so the Top Consumed card stops re-requesting them."""
    from inventory_tracker import _read_json
    data = _read_json(_toast_empty_days_path(), {})
    return data if isinstance(data, dict) else {}


def _save_toast_empty_days(marks: dict) -> None:
    from inventory_tracker import _write_json
    _write_json(_toast_empty_days_path(), marks)


def _toast_fetch_log_path():
    from inventory_tracker import DATA_DIR
    return DATA_DIR / "toast_fetch_log.json"


def _load_toast_fetch_log() -> dict:
    """{"<restaurant_guid>|<YYYY-MM-DD>": "<UTC ISO when that day was last
    pulled from Toast>"} -- how old each stored day is, and which days were
    pulled before Toast had finalised them."""
    from inventory_tracker import _read_json
    data = _read_json(_toast_fetch_log_path(), {})
    return data if isinstance(data, dict) else {}


def _save_toast_fetch_log(log: dict) -> None:
    from inventory_tracker import _write_json
    _write_json(_toast_fetch_log_path(), log)


def _merge_toast_pull(new_rows: list, ok_pairs, fetched_at: str, final_before: str):
    """Merge one Toast pull into sales.json under data_lock. Returns the
    merged rows.

    * a day that came back with rows is REPLACED wholesale;
    * a completed pull with no rows keeps what was stored, and is remembered
      as empty only when the day is older than ``final_before``;
    * every completed (guid, date) gets its fetched_at stamped;
    * failed pairs are simply absent from ``ok_pairs`` -- nothing changes.
    """
    from inventory_tracker import data_lock, load_sales, save_sales
    got_keys = {(r.get("restaurant_guid") or "", r.get("business_date") or "")
                for r in new_rows or []}
    newly_empty = {f"{g}|{d}" for (g, d) in (ok_pairs or set())
                   if (g, d) not in got_keys and d < final_before}
    with data_lock():
        # Re-read under the lock: another view / the nightly pull may have
        # merged meanwhile.
        merged = load_sales() or []
        if new_rows:
            merged = [r for r in merged
                      if ((r.get("restaurant_guid") or ""),
                          (r.get("business_date") or "")) not in got_keys]
            merged.extend(new_rows)
            save_sales(merged)
        if newly_empty:
            marks = _load_toast_empty_days()
            for k in newly_empty:
                marks.setdefault(k, fetched_at)
            _save_toast_empty_days(marks)
        if ok_pairs:
            log = _load_toast_fetch_log()
            for (g, d) in ok_pairs:
                log[f"{g}|{d}"] = fetched_at
            _save_toast_fetch_log(log)
    return merged


@reporting_bp.route("/api/report/toast-sales")
def api_report_toast_sales():
    """Top-selling items aggregated by week or month, optionally by
    location. Output is the data behind the Report -> Top Consumed
    section.

    Query params:
      period        'week' | 'month' (default 'week')
      buckets       int, default 8 (weeks) / 6 (months)
      location      restaurant_guid OR location string, optional
      top_n         int, default 10
    """
    from inventory_tracker import load_sales
    from datetime import datetime as _dt, timedelta as _td

    args = request.args
    period = (args.get("period") or "week").lower()
    if period not in ("week", "month"):
        return jsonify({"ok": False, "error": "period must be week|month"}), 400
    try:
        buckets = int(args.get("buckets") or (8 if period == "week" else 6))
    except (TypeError, ValueError):
        buckets = 8 if period == "week" else 6
    try:
        top_n = int(args.get("top_n") or 10)
    except (TypeError, ValueError):
        top_n = 10
    location_q = (args.get("location") or "").strip()
    # Anchor date — the Report page's Prev/Next/Today buttons and the
    # date picker all set this to a single YYYY-MM-DD. When present we
    # return the SINGLE week/month bucket containing that date so the
    # user sees the period they picked (matching the Restock card's
    # one-window-at-a-time semantics). When absent we fall back to
    # returning the most recent N buckets that have data.
    end_date_q = (args.get("end_date") or "").strip()
    # The Report tab is served from the store (the nightly pull keeps it
    # current). A live Toast pull happens ONLY when the caller asks with
    # live=1 -- the tab's "Refresh from Toast" button. Opening the tab used
    # to fan out up to 17 locations x 7 days of Toast calls inside the page
    # load.
    live_q = (args.get("live") or "0").strip().lower() in ("1", "true", "yes")

    # Bucket each row.
    def _bucket_key(date_iso: str) -> str:
        try:
            d = _dt.strptime(date_iso, "%Y-%m-%d")
        except ValueError:
            return ""
        if period == "week":
            # ISO week, Monday-start. Anchor on the Monday of that week.
            mon = d - _td(days=d.weekday())
            return mon.strftime("%Y-%m-%d")
        return d.strftime("%Y-%m")

    def _bucket_range(anchor_iso: str):
        """Return (start_date_iso, end_date_iso) inclusive for the
        bucket containing anchor_iso. Caps end at today — we never
        ask Toast for future dates."""
        ed = _dt.strptime(anchor_iso, "%Y-%m-%d")
        if period == "week":
            start = ed - _td(days=ed.weekday())
            end   = start + _td(days=6)
        else:
            start = ed.replace(day=1)
            if ed.month == 12:
                end = ed.replace(day=31)
            else:
                end = ed.replace(month=ed.month + 1, day=1) - _td(days=1)
        today = business_today()
        if end.date() > today:
            end = _dt.combine(today, _dt.min.time())
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    def _target_locations(loc_q: str):
        """Return [(restaurant_guid, location_name), ...] for the live
        fetch. If a specific location is selected, returns just that
        one. Otherwise returns every active retail location."""
        from inventory_tracker import TOAST_RETAIL_LOCATIONS
        if loc_q:
            if "-" in loc_q or len(loc_q) == 36:
                for L in TOAST_RETAIL_LOCATIONS:
                    if L["restaurant_guid"] == loc_q:
                        return [(L["restaurant_guid"], L["location"])]
                return [(loc_q, "")]
            q = loc_q.lower()
            return [(L["restaurant_guid"], L["location"])
                    for L in TOAST_RETAIL_LOCATIONS
                    if q in (L.get("location") or "").lower()]
        return [(L["restaurant_guid"], L["location"])
                for L in TOAST_RETAIL_LOCATIONS
                if (L.get("status") or "active") == "active"]

    def _ensure_bucket_cached(all_rows: list, anchor_iso: str, loc_q: str):
        """Make sure sales.json covers the bucket containing anchor_iso for the
        target locations, pulling from Toast what is missing. Returns
        (updated_rows, meta dict) where meta carries fetch info for the client
        (rows_fetched, fetch_error, fetched_at).

        Which (location, date) pairs are fetched:
          * TODAY and YESTERDAY -- always, and the fresh rows REPLACE that
            day's stored rows. "Any row cached = done" froze a day at whatever
            had sold by the first view (a 9am look kept showing the 9am
            numbers forever; review 2026-10-22).
          * older dates -- only when no rows are stored AND the day isn't
            marked empty. A completed fetch that returns no orders marks the
            day empty (data/toast_empty_days.json), so a closed day is not
            re-requested on every view; a FAILED fetch is not marked and is
            retried next time.
        """
        meta = {"rows_fetched": 0, "fetch_error": None, "fetched_at": None,
                "live": bool(live_q), "requested": 0, "failed": 0}
        if not live_q:
            return all_rows, meta
        try:
            start_iso, end_iso = _bucket_range(anchor_iso)
        except ValueError:
            return all_rows, meta
        targets = _target_locations(loc_q)
        if not targets:
            return all_rows, meta
        fresh_from = (business_today() - _td(days=1)).isoformat()
        # Which (guid, date) pairs already have at least 1 row?
        cached_keys = set()
        for r in all_rows:
            d = (r.get("business_date") or "").strip()
            if start_iso <= d <= end_iso:
                cached_keys.add((r.get("restaurant_guid") or "", d))
        empty_days = _load_toast_empty_days()
        # Enumerate every (target_location, date) pair in the bucket
        # range and decide which to (re)fetch -- see the docstring.
        missing = []
        s = _dt.strptime(start_iso, "%Y-%m-%d")
        e = _dt.strptime(end_iso,   "%Y-%m-%d")
        d_cursor = s
        while d_cursor <= e:
            d_iso = d_cursor.strftime("%Y-%m-%d")
            for (guid, name) in targets:
                if d_iso >= fresh_from:
                    # Re-pull a live day at most every _TOAST_REFRESH_S per
                    # worker: the Report tab is opened on today's week, and 17
                    # locations x 2 days on EVERY view would hammer Toast.
                    _last = _TOAST_REFRESHED.get((guid, d_iso))
                    if ((guid, d_iso) in cached_keys and _last is not None
                            and _time.monotonic() - _last < _TOAST_REFRESH_S):
                        continue
                    missing.append((guid, d_iso, name))
                elif ((guid, d_iso) not in cached_keys
                      and f"{guid}|{d_iso}" not in empty_days):
                    missing.append((guid, d_iso, name))
            d_cursor += _td(days=1)
        if not missing:
            return all_rows, meta
        try:
            from integrations import toast_api
            if not toast_api.is_configured():
                meta["fetch_error"] = "toast_not_configured"
                return all_rows, meta
            # Network I/O -- deliberately OUTSIDE data_lock.
            new_rows, ok_pairs = toast_api.fetch_product_mix_batch_with_status(
                missing, max_workers=6, timeout_s=28.0)
        except Exception as ex:
            meta["fetch_error"] = _safe_err(ex, "toast fetch")
            return all_rows, meta
        from datetime import timezone as _tz
        meta["fetched_at"] = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _now_m = _time.monotonic()
        for (g, d) in (ok_pairs or set()):
            if d >= fresh_from:
                _TOAST_REFRESHED[(g, d)] = _now_m
        # Say so when location-days failed. All-failed used to come back as
        # no rows + no error, and the tab said "no new orders found".
        meta["requested"] = len(missing)
        meta["failed"] = len({(g, d) for (g, d, _n) in missing} - set(ok_pairs or ()))
        if meta["failed"]:
            meta["fetch_error"] = (f"{meta['failed']} of {meta['requested']} "
                                   f"location-days failed")
        if not new_rows and not ok_pairs:
            return all_rows, meta
        merged = _merge_toast_pull(new_rows, ok_pairs, meta["fetched_at"], fresh_from)
        meta["rows_fetched"] = len(new_rows or [])
        return merged, meta

    def _store_age(anchor_iso: str, loc_q: str) -> dict:
        """How current the stored rows for this bucket are."""
        try:
            start_iso, end_iso = _bucket_range(anchor_iso)
        except ValueError:
            return {}
        log = _load_toast_fetch_log()
        empty_days = _load_toast_empty_days()
        have = {((r.get("restaurant_guid") or ""), (r.get("business_date") or ""))
                for r in rows_all if start_iso <= (r.get("business_date") or "") <= end_iso}
        stamps, missing_n, pairs_n = [], 0, 0
        d_cursor = _dt.strptime(start_iso, "%Y-%m-%d")
        e = _dt.strptime(end_iso, "%Y-%m-%d")
        targets = _target_locations(loc_q)
        while d_cursor <= e:
            d_iso = d_cursor.strftime("%Y-%m-%d")
            for (guid, _name) in targets:
                pairs_n += 1
                st = log.get(f"{guid}|{d_iso}")
                if st:
                    stamps.append(st)
                elif (guid, d_iso) not in have and f"{guid}|{d_iso}" not in empty_days:
                    missing_n += 1
            d_cursor += _td(days=1)
        return {"oldest_fetched_at": min(stamps) if stamps else None,
                "newest_fetched_at": max(stamps) if stamps else None,
                "location_days": pairs_n, "location_days_not_pulled": missing_n,
                "start": start_iso, "end": end_iso}

    # Load the full row set ONCE. If an anchor date is set we may
    # mutate this with a Toast fetch before bucketing.
    rows = load_sales() or []
    fetch_meta = {"rows_fetched": 0, "fetch_error": None, "fetched_at": None,
                  "live": bool(live_q), "requested": 0, "failed": 0}
    if end_date_q:
        try:
            rows, fetch_meta = _ensure_bucket_cached(rows, end_date_q,
                                                    location_q)
        except Exception as ex:
            fetch_meta["fetch_error"] = _safe_err(ex, "toast fetch")
    rows_all = rows
    store_meta = {}
    if end_date_q:
        try:
            store_meta = _store_age(end_date_q, location_q)
        except Exception as ex:  # noqa: BLE001 -- informational only
            store_meta = {"error": _safe_err(ex, "toast store age")}

    # Apply the location filter (after the live fetch so newly-pulled
    # rows are visible).
    if location_q:
        if "-" in location_q or len(location_q) == 36:
            rows = [r for r in rows if r.get("restaurant_guid") == location_q]
        else:
            q = location_q.lower()
            rows = [r for r in rows if q in (r.get("location") or "").lower()]

    if not rows and not end_date_q:
        return jsonify({"ok": True, "period": period, "buckets": [],
                        "total_rows": 0, "fetch": fetch_meta})

    # Aggregate by NORMALIZED display name (+ menu group) rather than
    # item_guid. Toast issues a fresh item_guid every time a menu item
    # is re-published (price change, name tweak, modifier shuffle), so
    # the same user-visible product can appear under several guids in
    # a single week. Keying on guid surfaces these as duplicate rows;
    # keying on name collapses them. The underlying sales.json still
    # keeps the raw guid for traceability.
    def _norm(s: str) -> str:
        return " ".join((s or "").lower().split())

    buckets_map: dict = {}
    for r in rows:
        bk = _bucket_key(r.get("business_date") or "")
        if not bk:
            continue
        slot = buckets_map.setdefault(bk, {})
        name      = (r.get("item") or "").strip()
        menu_grp  = (r.get("menu_group") or "").strip()
        if not name and not r.get("item_guid"):
            continue
        item_key  = (_norm(name), _norm(menu_grp))
        agg = slot.setdefault(item_key, {
            "item":       name,
            "menu_group": menu_grp,
            "qty":        0,
            "gross":      0.0,
            "net":        0.0,
            "guid_count": 0,
        })
        agg["qty"]        += int(r.get("qty") or 0)
        agg["gross"]      += float(r.get("gross") or 0)
        agg["net"]        += float(r.get("net") or 0)
        agg["guid_count"] += 1  # informational; not used in render
        # Prefer the longest/non-empty observed values for display.
        if name and len(name) > len(agg["item"]):
            agg["item"] = name
        if menu_grp and not agg["menu_group"]:
            agg["menu_group"] = menu_grp

    def _label_for(bk: str) -> str:
        if period == "week":
            d = _dt.strptime(bk, "%Y-%m-%d")
            return f"{d.strftime('%b %-d')} \u2013 {(d + _td(days=6)).strftime('%b %-d, %Y')}"
        d = _dt.strptime(bk, "%Y-%m")
        return d.strftime("%B %Y")

    def _serialize(bk: str) -> dict:
        items = list(buckets_map.get(bk, {}).values())
        items.sort(key=lambda x: x["gross"], reverse=True)
        total = sum(x["gross"] for x in items) or 1
        for it in items:
            it["mix_pct"] = round(100 * it["gross"] / total, 2)
            it["gross"]   = round(it["gross"], 2)
            it["net"]     = round(it["net"], 2)
        return {
            "key":         bk,
            "label":       _label_for(bk),
            "total_gross": round(total, 2) if items else 0.0,
            "items":       items[:top_n],
            "item_count":  len(items),
        }

    # When a specific date is picked, return ONLY the bucket containing
    # that date \u2014 even if it has no rows, so the UI can render a clear
    # "no data for week of X" state instead of falling silently empty.
    if end_date_q:
        try:
            ed = _dt.strptime(end_date_q, "%Y-%m-%d")
            if period == "week":
                anchor = (ed - _td(days=ed.weekday())).strftime("%Y-%m-%d")
            else:
                anchor = ed.strftime("%Y-%m")
            return jsonify({"ok": True, "period": period,
                            "buckets":     [_serialize(anchor)],
                            "anchor":      anchor,
                            "anchor_date": end_date_q,
                            "total_rows":  len(rows),
                            "store":       store_meta,
                            "fetch":       fetch_meta})
        except ValueError:
            pass

    # No anchor date \u2014 fall back to the most recent N buckets with data.
    bucket_keys = sorted(buckets_map.keys(), reverse=True)
    out_buckets = [_serialize(bk) for bk in bucket_keys[:buckets]]

    return jsonify({"ok": True, "period": period,
                    "buckets": out_buckets,
                    "total_rows": len(rows)})


# --- Nightly Toast pull --------------------------------------------------------
#: Business dates re-pulled every night (yesterday back this many days): Toast
#: keeps revising a day for ~4 days after it closes.
TOAST_NIGHTLY_DAYS = 4
#: A day is final this many days after it closes; a day last pulled before
#: that is pulled once more.
TOAST_FINAL_AFTER_DAYS = 4
#: How far back the nightly pull looks for days that were never pulled, or
#: were pulled before they were final.
TOAST_REPULL_LOOKBACK_DAYS = 21
#: Hard time budget. Gunicorn kills a request at 180 s; stop well inside it
#: and leave the rest for the next run.
TOAST_NIGHTLY_BUDGET_S = 120.0
_TOAST_NIGHTLY_BATCH = 24
_TOAST_NIGHTLY_LOCK = __import__("threading").Lock()


def toast_nightly_targets(today, sales_rows, fetch_log, *, days=TOAST_NIGHTLY_DAYS,
                          now_utc=None):
    """[(guid, date_iso, name, status)] the nightly pull should fetch, most
    urgent first: the trailing ``days`` business dates for every registry
    store (active and pre-launch), then older days inside the look-back that
    were never pulled or were last pulled before they were final. A pair
    pulled in the last 30 minutes is skipped (a second worker's scheduler
    firing the same job)."""
    from datetime import timezone
    from inventory_tracker import TOAST_RETAIL_LOCATIONS
    now_utc = now_utc or datetime.now(timezone.utc)
    recent = (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stores = [(L["restaurant_guid"], L.get("location") or "", (L.get("status") or "active"))
              for L in TOAST_RETAIL_LOCATIONS
              if (L.get("status") or "active") in ("active", "pre_launch")
              and L.get("restaurant_guid")]
    have = {((r.get("restaurant_guid") or ""), (r.get("business_date") or ""))
            for r in sales_rows or []}
    trailing, older = [], []
    for back in range(1, TOAST_REPULL_LOOKBACK_DAYS + 1):
        d = today - timedelta(days=back)
        d_iso = d.isoformat()
        final_on = (d + timedelta(days=1 + TOAST_FINAL_AFTER_DAYS)).isoformat()
        for (guid, name, status) in stores:
            stamp = str(fetch_log.get(f"{guid}|{d_iso}") or "")
            if stamp and stamp >= recent:
                continue
            if back <= days:
                trailing.append((guid, d_iso, name, status))
            elif not stamp:
                # Never pulled by us. Pre-launch stores have no history worth
                # chasing; an active store's unstamped day is pulled once.
                if status == "active" or (guid, d_iso) in have:
                    older.append((guid, d_iso, name, status))
            elif stamp[:10] < final_on:
                older.append((guid, d_iso, name, status))   # pulled before final
    return trailing + older


@reporting_bp.route("/api/toast/nightly-pull", methods=["POST"])
def api_toast_nightly_pull():
    """Pull Toast product mix into the store so the Report tab and the
    Planner's trend note never depend on someone opening a page.

    Called daily at 08:15 UTC by the in-process scheduler with an empty JSON
    body. Optional body: {"days": 4, "max_seconds": 120, "dry_run": false}.

    Pulls the trailing 4 business dates for every registry store (active and
    pre-launch), plus any day in the last 21 that was never pulled or was
    pulled before it was final (close + 4 days). Stops at ~120 s and reports
    what is left; the next run picks it up. Each completed (store, day) gets
    a fetched_at stamp (data/toast_fetch_log.json). Network I/O happens
    outside data_lock; each merge is one locked read-modify-write.
    """
    from datetime import timezone
    from core import feed_health
    from core.errors import _log_exc
    from inventory_tracker import load_sales
    body = request.get_json(silent=True) or {}
    try:
        days = max(1, min(int(body.get("days") or TOAST_NIGHTLY_DAYS), 14))
    except (TypeError, ValueError):
        days = TOAST_NIGHTLY_DAYS
    try:
        budget = max(5.0, min(float(body.get("max_seconds") or TOAST_NIGHTLY_BUDGET_S), 150.0))
    except (TypeError, ValueError):
        budget = TOAST_NIGHTLY_BUDGET_S
    dry_run = bool(body.get("dry_run"))

    from integrations import toast_api
    if not toast_api.is_configured():
        feed_health.record("toast-nightly", False, "Toast credentials are not set on the server")
        return jsonify({"ok": False, "error": "toast_not_configured"}), 503
    if not _TOAST_NIGHTLY_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "a Toast pull is already running"}), 409
    try:
        started = _time.monotonic()
        today = business_today()
        pending = toast_nightly_targets(today, load_sales() or [], _load_toast_fetch_log(),
                                        days=days)
        requested = len(pending)
        if dry_run:
            return jsonify({"ok": True, "dry_run": True, "requested": requested,
                            "pairs": [list(p[:3]) for p in pending[:60]]})
        fresh_from = (today - timedelta(days=1)).isoformat()
        fetched = rows_n = 0
        failed, not_live = [], []
        while pending:
            left = budget - (_time.monotonic() - started)
            if left < 8:
                break
            batch, pending = pending[:_TOAST_NIGHTLY_BATCH], pending[_TOAST_NIGHTLY_BATCH:]
            status_of = {(g, d): st for (g, d, _n, st) in batch}
            name_of = {(g, d): n for (g, d, n, _st) in batch}
            try:
                # Network I/O -- deliberately OUTSIDE data_lock.
                new_rows, ok_pairs = toast_api.fetch_product_mix_batch_with_status(
                    [(g, d, n) for (g, d, n, _st) in batch],
                    max_workers=6, timeout_s=max(5.0, min(40.0, left - 5)))
            except Exception as ex:  # noqa: BLE001
                _log_exc(ex, "toast nightly batch")
                new_rows, ok_pairs = [], set()
            ok_pairs = set(ok_pairs or ())
            # Only rows for pairs that completed are merged.
            new_rows = [r for r in (new_rows or [])
                        if ((r.get("restaurant_guid") or ""),
                            (r.get("business_date") or "")) in ok_pairs]
            for key in status_of:
                if key in ok_pairs:
                    continue
                # A store that is not open yet has nothing to pull; that is
                # not a failure of the feed.
                (not_live if status_of[key] == "pre_launch" else failed).append(
                    {"restaurant_guid": key[0], "location": name_of[key], "date": key[1]})
            if ok_pairs:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                _merge_toast_pull(new_rows, ok_pairs, stamp, fresh_from)
                fetched += len(ok_pairs)
                rows_n += len(new_rows)
        remaining = len(pending)
        elapsed = round(_time.monotonic() - started, 1)
        ok = not failed
        detail = (f"{fetched} of {requested} location-days pulled, {rows_n} rows"
                  + (f"; {len(failed)} failed" if failed else "")
                  + (f"; {remaining} left for the next run" if remaining else ""))
        feed_health.record("toast-nightly", ok, detail, requested=requested,
                           fetched=fetched, failed=len(failed), remaining=remaining,
                           rows=rows_n, prelaunch_unavailable=len(not_live),
                           elapsed_s=elapsed,
                           data_through=(today - timedelta(days=1)).isoformat()
                           if ok and not remaining else "")
        return jsonify({
            "ok": ok, "requested": requested, "fetched": fetched, "rows": rows_n,
            "failed": len(failed), "failed_pairs": failed[:25],
            "fetch_error": (f"{len(failed)} of {requested} location-days failed"
                            if failed else None),
            "prelaunch_unavailable": len(not_live),
            "remaining": remaining,
            "remaining_pairs": [{"restaurant_guid": g, "date": d, "location": n}
                                for (g, d, n, _st) in pending[:25]],
            "elapsed_s": elapsed, "budget_s": budget, "days": days,
        })
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "toast nightly pull")
        feed_health.record("toast-nightly", False, "the pull crashed (see server log)")
        return jsonify({"ok": False, "error": "internal error"}), 500
    finally:
        _TOAST_NIGHTLY_LOCK.release()


@reporting_bp.route("/api/sales/locations")
def api_sales_locations():
    """List every retail Toast location in the registry, with row
    counts and date ranges from the sales store. Locations with no
    ingested data return rows=0 — they still show in the dropdown so
    the user can pick any location for comparison.
    """
    from inventory_tracker import load_sales, TOAST_RETAIL_LOCATIONS
    rows = load_sales() or []
    # Start with the registry — every retail location guaranteed, with
    # state abbreviation so the UI can render "Penn Station, NY" without
    # any per-location lookup.
    by_loc: dict = {}
    for L in TOAST_RETAIL_LOCATIONS:
        by_loc[L["restaurant_guid"]] = {
            "restaurant_guid": L["restaurant_guid"],
            "location":        L["location"],
            "state":           L.get("state") or "",
            "status":          L.get("status") or "active",
            "rows":            0,
            "min_date":        "",
            "max_date":        "",
        }
    # Layer in any ingested data — increment counts + date range.
    for r in rows:
        guid = r.get("restaurant_guid") or ""
        if not guid:
            continue
        slot = by_loc.setdefault(guid, {
            "restaurant_guid": guid,
            "location":        r.get("location") or "",
            "state":           "",
            "rows":            0,
            "min_date":        r.get("business_date") or "",
            "max_date":        r.get("business_date") or "",
        })
        slot["rows"] += 1
        d = r.get("business_date") or ""
        if d:
            if not slot["min_date"] or d < slot["min_date"]:
                slot["min_date"] = d
            if not slot["max_date"] or d > slot["max_date"]:
                slot["max_date"] = d
    return jsonify({"ok": True,
                    "locations": sorted(by_loc.values(),
                                        key=lambda x: x["location"])})


@reporting_bp.route("/api/traceability/search")
def api_traceability_search():
    """Cross-source traceability search.

    Query params:
      ``from``        ISO date — only include records on/after this date.
      ``to``          ISO date — only include records on/before this date.
      ``lot``         Substring match on lot_number (case-insensitive).
      ``mfg_code``    4-digit item code (matches the first 4 digits of
                      any lot OR the variety via HH_MFG_CODE_TO_VARIETY).
      ``warehouse``   Exact warehouse name.
      ``distributor`` Exact distributor name.

    Returns a list of matching production records, each with these
    downstream fields populated for the requesting line:
      ``po_status``      pending | arrived | canceled | unknown
      ``ship_date``      from the on_order entry, if pending.
      ``arrival_date``   from the on_order entry, if pending.
      ``eta``            from the on_order entry, if pending.
      ``lines[].on_hand_now``  current cs on hand for that variety at
                                that warehouse (live inventory).
      ``lines[].usage_total_cs`` total cs moved on usage log for this
                                PO + variety (from usage.json).
      ``lines[].usage_event_count``  count of usage events.
    """
    from inventory_tracker import (
        load_production, load_inventory, load_usage,
        load_canceled_pos,
    )
    try:
        from integrations.hh_mfg_codes import HH_MFG_CODE_TO_VARIETY
    except Exception:
        HH_MFG_CODE_TO_VARIETY = {}

    args = request.args
    q_from = (args.get("from") or "").strip()
    q_to   = (args.get("to") or "").strip()
    q_lot  = (args.get("lot") or "").strip().lower()
    q_code = (args.get("mfg_code") or "").strip()
    q_wh   = (args.get("warehouse") or "").strip()
    q_dist = (args.get("distributor") or "").strip()

    records = load_production()
    from integrations.production_pdf_parser import ledger_warehouse_for

    # Apply filters.
    out = []
    code_variety = HH_MFG_CODE_TO_VARIETY.get(q_code) if q_code else None
    for r in records:
        if q_from and (r.get("production_date") or "") < q_from:
            continue
        if q_to and (r.get("production_date") or "") > q_to:
            continue
        # Accept the PO ledger's name for a Chefs Warehouse DC too ("Bronx,
        # NY" for "Chefs Warehouse, NY"); stored records are not renamed.
        if q_wh and q_wh not in ((r.get("warehouse") or ""), ledger_warehouse_for(
                r.get("warehouse") or "", r.get("distributor") or "")):
            continue
        if q_dist and (r.get("distributor") or "") != q_dist:
            continue
        # Lot / mfg-code filter only keeps the record if at least one
        # line matches. We do NOT prune the other lines from the record
        # — operators usually want to see the full sheet for context.
        if q_lot or q_code:
            matched_any = False
            for L in r.get("lines", []):
                lot = (L.get("lot_number") or "")
                if q_lot and q_lot in lot.lower():
                    matched_any = True
                    break
                if q_code:
                    # Match either the first 4 digits of the lot, or the
                    # variety mapped from the code.
                    if lot.startswith(q_code):
                        matched_any = True
                        break
                    if code_variety and (L.get("variety") or "") == code_variety:
                        matched_any = True
                        break
            if not matched_any:
                continue
        out.append(r)

    # Build downstream lookups.
    inv = load_inventory()
    # {(warehouse, variety) -> on_hand_qty} for currently-on-hand snapshot
    on_hand = {}
    for item in inv.values():
        wh = item.get("warehouse") or ""
        name = item.get("name") or ""
        variety = name.split(" Bagel")[0] if " Bagel" in name else name
        on_hand[(wh, variety)] = (on_hand.get((wh, variety), 0)
                                  + float(item.get("quantity") or 0))

    # PO status comes from the canonical PO ledger, keyed by PO number --
    # USF, Cheney AND Chefs Warehouse. Inferring it from on_order + the CW
    # store marked every DELIVERED Chefs Warehouse PO "pending" (and so red
    # "overdue" forever: 10 of 10 CW sheets since August).
    from core.util import _norm_po_key
    try:
        from blueprints.pos import build_po_ledger
        ledger_by_po = {_norm_po_key(x.get("po_number") or ""): x
                        for x in build_po_ledger()}
    except Exception:  # noqa: BLE001 -- status is an overlay; never fatal
        ledger_by_po = {}

    # Usage rollup keyed by (po_number, variety).
    usage_log = load_usage() or []
    usage_roll: dict = {}  # {(po, variety): {cs, n}}
    for e in usage_log:
        po = (e.get("po_number") or "").strip()
        if not po:
            continue
        name = e.get("item_name") or ""
        variety = name.split(" Bagel")[0] if " Bagel" in name else name
        slot = usage_roll.setdefault((po, variety), {"cs": 0.0, "n": 0})
        slot["cs"] += abs(float(e.get("amount") or 0))
        slot["n"] += 1

    # Enrich each record.
    enriched = []
    for r in out:
        rcopy = dict(r)
        po = (r.get("po_number") or "").strip()
        wh = r.get("warehouse") or ""
        led = ledger_by_po.get(_norm_po_key(po)) if po else None
        lst = (led or {}).get("status") or ""
        if led and lst == "canceled":
            rcopy["po_status"] = "canceled"
        elif led and lst == "arrived":
            rcopy["po_status"] = "arrived"
            rcopy["arrival_date"] = led.get("arrival_date") or ""
        elif led:
            # pending / in_transit / in_production / any override: still open
            rcopy["po_status"]    = "pending"
            rcopy["ship_date"]    = led.get("ship_date") or ""
            rcopy["arrival_date"] = led.get("arrival_date") or ""
            rcopy["eta"]          = led.get("eta") or ""
        else:
            # No such PO on file (typo'd / untracked customer PO): say so
            # rather than claiming it arrived.
            rcopy["po_status"] = "unknown"

        enriched_lines = []
        for L in r.get("lines") or []:
            v = L.get("variety") or ""
            lcopy = dict(L)
            lcopy["on_hand_now"] = round(on_hand.get((wh, v), 0), 2)
            ur = usage_roll.get((po, v)) if po else None
            lcopy["usage_total_cs"]    = round(ur["cs"], 2) if ur else 0
            lcopy["usage_event_count"] = ur["n"] if ur else 0
            enriched_lines.append(lcopy)
        rcopy["lines"] = enriched_lines
        enriched.append(rcopy)

    # Sort newest-first by production date for stable grouping.
    enriched.sort(key=lambda r: (r.get("production_date") or "",
                                  r.get("po_number") or ""),
                  reverse=True)
    return jsonify({
        "ok": True,
        "filters": {
            "from": q_from, "to": q_to, "lot": q_lot, "mfg_code": q_code,
            "warehouse": q_wh, "distributor": q_dist,
        },
        "count": len(enriched),
        "records": enriched,
        "mfg_code_map": HH_MFG_CODE_TO_VARIETY,
    })


@reporting_bp.route("/api/report/plh")
def api_report_plh():
    """Production revenue, labor hours, and $PLH per time bucket.

    Query params:
      grain   "week" (default) | "month" | "quarter"
      offset  int >= 0. 0 = current window. Each step shifts back one
              full window: 4 weeks / 1 quarter / 1 year.
    """
    from inventory_tracker import load_production, load_labor, load_bakery_sales
    grain = (request.args.get("grain") or "week").lower()
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    prod  = load_production()
    labor = load_labor()
    bsales = load_bakery_sales()

    # For week/month grain, anchor the view at the most recent POPULATED
    # period instead of "today." If the current week or month hasn't been
    # filled in yet (the bakery model spreadsheet is updated weekly), the
    # auto_shift slides the window back so we still show four populated
    # bars instead of three + a blank.
    auto_shift = 0
    if grain in ("week", "month"):
        # Probe up to 4 periods back; if everything is empty, stop shifting
        # and just render the empties so the user can see the gap.
        for probe in range(0, 5):
            pb = _plh_bucket_keys(grain, offset=probe)
            ls, le = pb[-1][1], pb[-1][2]
            if not _bucket_is_empty(ls, le, prod, labor, bsales):
                auto_shift = probe
                break
    effective_offset = offset + auto_shift
    buckets = _plh_bucket_keys(grain, offset=effective_offset)

    out = []
    for key, start, end, label in buckets:
        bucket = {
            "key": key, "label": label, "start": start, "end": end,
            "total_cs": 0,
            "revenue_dollars": 0.0,            # cs * case price (production-side)
            "by_distributor": {},
            "labor_hours": 0.0,
            "labor_dollars": 0.0,
            "bakery_sales_dollars": 0.0,        # true sales from bakery_sales.json
            "bakery_sales_by_channel": {},
            "bakery_sales_weeks": 0,            # how many weekly rows landed here
            "splh": None,                       # sales per labor hour
            "labor_pct_of_sales": None,         # labor $ / sales $
            "plh": None,                        # legacy: revenue / hours
        }
        for r in prod:
            if not _date_in_range(r.get("production_date") or "", start, end):
                continue
            dist = r.get("distributor") or ""
            price = _case_price_for(dist)
            for line in (r.get("lines") or []):
                cs = int(line.get("cs_count") or 0)
                bucket["total_cs"] += cs
                bucket["revenue_dollars"] += cs * price
                bdist = bucket["by_distributor"].setdefault(
                    dist or "Other", {"cs": 0, "revenue_dollars": 0.0})
                bdist["cs"] += cs
                bdist["revenue_dollars"] += cs * price
        # Track bakery-xlsx labor separately so the sales-vs-labor metrics
        # (SPLH, labor % of sales) reconcile to the workbook's I11/O10
        # numbers exactly. The "all labor" pool still drives total labor
        # hours/cost and the production-side $PLH, since those reflect the
        # full picture of what the bakery actually paid out.
        bx_hours = 0.0
        bx_dollars = 0.0
        for e in labor:
            if not _date_in_range(e.get("date") or "", start, end):
                continue
            hrs = float(e.get("hours") or 0)
            dol = float(e.get("dollars") or 0)
            bucket["labor_hours"]   += hrs
            bucket["labor_dollars"] += dol
            if (e.get("source") or "") == "bakery-xlsx":
                bx_hours   += hrs
                bx_dollars += dol
        bucket["bakery_xlsx_labor_hours"]   = round(bx_hours, 4)
        bucket["bakery_xlsx_labor_dollars"] = round(bx_dollars, 2)
        # Aggregate bakery weekly sales whose week_start falls in this
        # bucket. A week is counted toward whichever bucket its Monday
        # lands in -- accepting the small straddle effect at month/
        # quarter boundaries in exchange for clean, deterministic math.
        for w in bsales:
            ws = (w.get("week_start") or "")
            if not _date_in_range(ws, start, end):
                continue
            bucket["bakery_sales_dollars"] += float(w.get("total") or 0)
            bucket["bakery_sales_weeks"]   += 1
            for ch, amt in (w.get("channels") or {}).items():
                bucket["bakery_sales_by_channel"][ch] = (
                    bucket["bakery_sales_by_channel"].get(ch, 0.0)
                    + float(amt or 0)
                )
        # If we have hours but no dollars on any entry, impute via the default
        # rate so the cost picture is at least directionally right.
        if bucket["labor_hours"] and not bucket["labor_dollars"]:
            bucket["labor_dollars"] = bucket["labor_hours"] * LABOR_RATE_DEFAULT
            bucket["labor_dollars_imputed"] = True
        if bucket["labor_hours"]:
            bucket["plh"] = bucket["revenue_dollars"] / bucket["labor_hours"]
        # Sales-vs-labor metrics ALWAYS use the bakery-xlsx labor pool so
        # the chart matches the workbook the user updates each week.
        if bx_hours and bucket["bakery_sales_dollars"]:
            bucket["splh"] = bucket["bakery_sales_dollars"] / bx_hours
        if bx_dollars and bucket["bakery_sales_dollars"]:
            bucket["labor_pct_of_sales"] = (
                bx_dollars / bucket["bakery_sales_dollars"]
            )
        out.append(bucket)

    return jsonify({
        "grain":            grain,
        "offset":           offset,
        "effective_offset": effective_offset,
        "auto_shift":       auto_shift,
        # The window may have slid back to the last populated period
        # (auto_shift); say what the newest data actually is so the chart
        # can show "data through <week>" and go red when it is old.
        "bakery_data":      bakery_data_status(bsales),
        "window_label":     _plh_window_label(grain, buckets),
        "case_prices": {
            "US Foods": 27.00, "Cheney Brothers": 26.50, "default": 29.50,
        },
        "labor_rate_default": LABOR_RATE_DEFAULT,
        "buckets":          out,
    })


def _ingest_labor(incoming: list, replace: bool) -> dict:
    """Merge labor entries into labor.json. Caller holds data_lock."""
    from inventory_tracker import load_labor, save_labor
    existing = load_labor()
    sources_in_incoming = {(e.get("source") or "manual") for e in incoming}
    if replace:
        existing = [e for e in existing if (e.get("source") or "manual")
                    not in sources_in_incoming]

    # Dedupe by (date, source). Newer wins.
    by_key = {}
    for e in existing:
        by_key[(e.get("date"), e.get("source") or "manual")] = e
    for e in incoming:
        d = (e.get("date") or "").strip()
        if not d:
            continue
        rec = {
            "date":    d,
            "hours":   float(e.get("hours") or 0),
            "dollars": float(e.get("dollars") or 0),
            "source":  e.get("source") or "manual",
            "ingested_at": datetime_now_iso(),
        }
        by_key[(rec["date"], rec["source"])] = rec
    merged = list(by_key.values())
    save_labor(merged)
    return {"ok": True, "total_entries": len(merged),
            "added_or_updated": len(incoming)}


def _ingest_bakery_sales(incoming: list, replace: bool) -> dict:
    """Merge weekly bakery sales into bakery_sales.json. Caller holds data_lock."""
    from inventory_tracker import load_bakery_sales, save_bakery_sales
    existing = load_bakery_sales()
    sources_in_incoming = {(e.get("source") or "bakery-xlsx") for e in incoming}
    if replace:
        existing = [e for e in existing
                    if (e.get("source") or "bakery-xlsx")
                    not in sources_in_incoming]

    by_key = {}
    for e in existing:
        by_key[(e.get("week_start"), e.get("source") or "bakery-xlsx")] = e
    added = 0
    for e in incoming:
        ws_ = (e.get("week_start") or "").strip()
        if not ws_:
            continue
        rec = {
            "week_start": ws_,
            "week_end":   (e.get("week_end") or "").strip() or None,
            "location":   e.get("location") or "Bakery",
            "channels":   {k: float(v or 0) for k, v in
                           (e.get("channels") or {}).items()},
            "total":      float(e.get("total") or 0),
            "splh":       (float(e["splh"]) if e.get("splh") not in
                           (None, "", "#DIV/0!") else None),
            "source":     e.get("source") or "bakery-xlsx",
            "ingested_at": datetime_now_iso(),
        }
        by_key[(rec["week_start"], rec["source"])] = rec
        added += 1
    merged = sorted(by_key.values(), key=lambda r: r.get("week_start") or "")
    save_bakery_sales(merged)
    return {"ok": True, "total_entries": len(merged),
            "added_or_updated": added}


@reporting_bp.route("/api/admin/labor/ingest", methods=["POST"])
@with_data_lock
def api_admin_labor_ingest():
    """Append or replace bakery labor entries.

    Body:
      entries:  [{date: YYYY-MM-DD, hours: float, dollars?: float, source?: str}]
      replace:  bool — when true, REPLACE all entries from the same `source`
                with the new set; when false (default), append + dedupe by
                (date, source) keeping the new entry.

    No Toast call here — this is the "feed me labor data from any source"
    endpoint. A separate script can pull from Toast and POST. CSV upload
    or manual entry can use this same endpoint. Load -> merge -> save runs
    under data_lock (two overlapping ingests used to lose one).
    """
    body = request.json or {}
    incoming = body.get("entries") or []
    replace = bool(body.get("replace", False))
    if not incoming:
        return jsonify({"ok": False, "error": "entries required"}), 400
    return jsonify(_ingest_labor(incoming, replace))


@reporting_bp.route("/api/admin/bakery-sales/ingest", methods=["POST"])
@with_data_lock
def api_admin_bakery_sales_ingest():
    """Append or replace bakery weekly sales entries.

    Body:
      entries:  [
        {
          week_start:  "YYYY-MM-DD",   # Monday -- required, used as the key
          week_end:    "YYYY-MM-DD",
          channels:    {channel: dollars, ...},
          total:       float,
          splh:        float | null,
          source:      str,            # default "bakery-xlsx"
        }, ...
      ]
      replace:  bool -- when true, REPLACE all entries that share a source
                with the incoming set; when false (default), dedupe by
                (week_start, source) keeping the new entry.
    Runs under data_lock.
    """
    body = request.json or {}
    incoming = body.get("entries") or []
    replace = bool(body.get("replace", False))
    if not incoming:
        return jsonify({"ok": False, "error": "entries required"}), 400
    return jsonify(_ingest_bakery_sales(incoming, replace))


# --- Bakery workbook on the server ---------------------------------------------
#: Largest workbook accepted (the real one is ~1-2 MB).
BAKERY_WORKBOOK_MAX_BYTES = 30 * 1024 * 1024
#: The Sales vs Labor chart goes red when its newest week ended this long ago.
BAKERY_DATA_STALE_DAYS = 10


def _bakery_workbook_state_path():
    from inventory_tracker import DATA_DIR
    return DATA_DIR / "bakery_workbook_state.json"


def _load_bakery_workbook_state() -> dict:
    from inventory_tracker import _read_json
    st = _read_json(_bakery_workbook_state_path(), {})
    return st if isinstance(st, dict) else {}


def bakery_data_status(bsales=None) -> dict:
    """How current the bakery Sales vs Labor data is: the newest week with
    sales, how long ago it ended (bakery calendar), and the last upload."""
    from datetime import date as _date
    if bsales is None:
        from inventory_tracker import load_bakery_sales
        bsales = load_bakery_sales() or []
    weeks = sorted((w for w in bsales if float(w.get("total") or 0) > 0),
                   key=lambda w: w.get("week_start") or "")
    last = weeks[-1] if weeks else None
    through = (last or {}).get("week_start") or ""
    through_end = (last or {}).get("week_end") or ""
    age = None
    if through:
        try:
            end = _date.fromisoformat(through_end) if through_end \
                else _date.fromisoformat(through) + timedelta(days=6)
            through_end = end.isoformat()
            age = (business_today() - end).days
        except ValueError:
            age = None
    st = _load_bakery_workbook_state()
    return {
        "data_through": through, "data_through_end": through_end,
        "days_since_data_through": age,
        "stale_after_days": BAKERY_DATA_STALE_DAYS,
        "stale": (age is None) or age > BAKERY_DATA_STALE_DAYS,
        "weeks": len(weeks),
        "uploaded_at": st.get("uploaded_at") or "",
        "uploaded_filename": st.get("filename") or "",
        "uploaded_by": st.get("uploaded_by") or "",
    }


@reporting_bp.route("/api/admin/bakery-workbook/status")
def api_admin_bakery_workbook_status():
    """'data through <week>, uploaded <when>' for the Report tab."""
    return jsonify({"ok": True, **bakery_data_status()})


@reporting_bp.route("/api/admin/bakery-workbook/upload", methods=["POST"])
def api_admin_bakery_workbook_upload():
    """Ingest the "Bakery Model - Sales v. Labor" workbook on the server.

    Send the .xlsx as multipart form field ``file`` OR as the raw request
    body. ``force=1`` (query, form field or X-Force header) overrides the two
    safety stops below. Auth: the usual login session or API token.

    Steps: validate the zip (a truncated Dropbox copy is rejected with a
    clear message) -> skip when the file's sha256 equals the last ingested
    one -> parse (no lock held) -> under data_lock, refuse if the sheet dates
    drift or the workbook holds FEWER weeks than are stored (both replace all
    stored bakery rows, so a bad file must not overwrite good history) ->
    apply the same two ingests the laptop script posted (labor + weekly
    sales, replace=True).

    Returns {ok, weeks, labor_rows, sales_rows, data_through, unchanged}.
    """
    import hashlib
    from datetime import timezone
    from flask import session
    from core import feed_health
    from core.errors import _log_exc
    from integrations.bakery_workbook import (
        SOURCE_TAG, WorkbookError, parse_workbook_detailed, validate_xlsx_bytes,
    )
    from inventory_tracker import (_write_json, data_lock, load_bakery_sales)

    force = str(request.args.get("force") or request.form.get("force")
                or request.headers.get("X-Force") or "").strip().lower() \
        in ("1", "true", "yes", "on")
    up = request.files.get("file")
    filename = ""
    if up is not None:
        data = up.read(BAKERY_WORKBOOK_MAX_BYTES + 1)
        filename = (up.filename or "")[:200]
    else:
        data = request.get_data(cache=False) or b""
        from urllib.parse import unquote
        filename = unquote(request.headers.get("X-Filename") or "")[:200]
    if not data:
        return jsonify({"ok": False, "error": "No file received. Send the .xlsx as "
                        "form field 'file' or as the request body."}), 400
    if len(data) > BAKERY_WORKBOOK_MAX_BYTES:
        return jsonify({"ok": False, "error": "That file is too large to be the "
                        "bakery workbook."}), 413

    def _fail(msg, code=400, problems=None):
        feed_health.record("bakery-workbook", False, msg[:300])
        out = {"ok": False, "error": msg}
        if problems:
            out["problems"] = problems[:40]
        return jsonify(out), code

    try:
        validate_xlsx_bytes(data)
    except WorkbookError as exc:
        return _fail(str(exc))
    sha = hashlib.sha256(data).hexdigest()
    state = _load_bakery_workbook_state()
    if not force and sha == state.get("sha256"):
        status = bakery_data_status()
        feed_health.record("bakery-workbook", True, "same file as last upload - nothing to do",
                           data_through=status["data_through"])
        return jsonify({"ok": True, "unchanged": True,
                        "weeks": state.get("weeks") or 0,
                        "labor_rows": state.get("labor_rows") or 0,
                        "sales_rows": state.get("sales_rows") or 0,
                        "data_through": status["data_through"],
                        "uploaded_at": state.get("uploaded_at") or ""})
    try:
        res = parse_workbook_detailed(data, force=force)       # CPU only, no lock
    except WorkbookError as exc:
        return _fail(str(exc), problems=exc.problems)
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "bakery workbook parse")
        return _fail("The workbook could not be read (see server log).", 500)
    labor, weeks = res["labor"], res["weeks"]
    if not weeks:
        return _fail("No week with sales or labor was found in this workbook, "
                     "so nothing was changed.")
    try:
        with data_lock():
            stored = [w for w in (load_bakery_sales() or [])
                      if (w.get("source") or "bakery-xlsx") == SOURCE_TAG]
            if len(weeks) < len(stored) and not force:
                return _fail(
                    f"This workbook has {len(weeks)} weeks but {len(stored)} are "
                    f"already stored. Uploading it would delete history, so "
                    f"nothing was changed. If that is intended, upload again "
                    f"with force.", 409)
            labor_out = _ingest_labor(labor, True) if labor else {"added_or_updated": 0}
            sales_out = _ingest_bakery_sales(weeks, True)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state = {"sha256": sha, "uploaded_at": now, "filename": filename,
                     "uploaded_by": str(session.get("user") or "api-token"),
                     "weeks": len(weeks), "labor_rows": len(labor),
                     "sales_rows": sales_out.get("added_or_updated") or 0,
                     "data_through": res["data_through"], "forced": force,
                     "notes": res["notes"][:20], "problems": res["problems"][:20]}
            _write_json(_bakery_workbook_state_path(), state)
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "bakery workbook ingest")
        return _fail("The workbook was read but could not be saved (see server log).", 500)
    feed_health.record("bakery-workbook", True,
                       f"{len(weeks)} weeks, data through week of {res['data_through']}",
                       data_through=res["data_through"], weeks=len(weeks),
                       labor_rows=len(labor))
    return jsonify({
        "ok": True, "unchanged": False,
        "weeks": len(weeks), "labor_rows": len(labor),
        "sales_rows": sales_out.get("added_or_updated") or 0,
        "data_through": res["data_through"], "data_through_end": res["data_through_end"],
        "uploaded_at": state["uploaded_at"], "forced": force,
        "notes": res["notes"][:20], "problems": res["problems"][:20],
        "labor_total_entries": labor_out.get("total_entries"),
    })


@reporting_bp.route("/api/report/bakery-sales")
def api_report_bakery_sales():
    """Return bakery weekly sales rows, optionally filtered by date range.

    Query params:
      start  YYYY-MM-DD  (inclusive -- compared against week_start)
      end    YYYY-MM-DD  (inclusive -- compared against week_start)
      limit  int         (default 26 -- most recent N weeks)
    """
    from inventory_tracker import load_bakery_sales
    rows = load_bakery_sales() or []
    args = request.args
    start = (args.get("start") or "").strip()
    end   = (args.get("end") or "").strip()
    if start:
        rows = [r for r in rows if (r.get("week_start") or "") >= start]
    if end:
        rows = [r for r in rows if (r.get("week_start") or "") <= end]
    try:
        limit = int(args.get("limit") or 26)
    except (TypeError, ValueError):
        limit = 26
    rows = sorted(rows, key=lambda r: r.get("week_start") or "")
    if limit and limit > 0:
        rows = rows[-limit:]
    channel_totals: dict[str, float] = {}
    grand_total = 0.0
    for r in rows:
        grand_total += float(r.get("total") or 0)
        for ch, v in (r.get("channels") or {}).items():
            channel_totals[ch] = channel_totals.get(ch, 0.0) + float(v or 0)
    return jsonify({
        "rows": rows,
        "summary": {
            "weeks":          len(rows),
            "total":          grand_total,
            "channel_totals": channel_totals,
        },
    })


@reporting_bp.route("/api/forecast/decrement-daily", methods=["POST"])
@with_data_lock
def api_forecast_decrement_daily():
    """Apply one day's worth of forecast usage to every SKU.

    Idempotent: for each SKU, if a non-reversed entry already exists today
    with source == "forecast-daily", that SKU is skipped. Safe to re-run
    or to call mid-day after the cron.

    Body (all optional):
      dry_run             bool   compute deltas without writing
      date                str    YYYY-MM-DD override for testing (defaults
                                 to today UTC). The idempotency check keys
                                 off this value.
      warehouse_prefix    str    restrict to SKUs whose `warehouse` field
                                 starts with this string (e.g. "Zebulon").
                                 Default: all SKUs with weekly_usage > 0.

    Returns {ok, date, applied: [...], skipped_existing: [...], dry_run}.
    """
    from datetime import datetime, timezone
    from inventory_tracker import (
        load_inventory, save_inventory, load_usage, save_usage,
        _variety_from_name,
    )

    body = request.json or {}
    dry_run = bool(body.get("dry_run"))
    date_iso = (body.get("date") or "").strip()
    wh_prefix = (body.get("warehouse_prefix") or "").strip()
    if not date_iso:
        date_iso = datetime.now(timezone.utc).date().isoformat()
    else:
        # {"date": "YYYY-MM-DD"} back-fills a missed day. It is the
        # idempotency key, so it must be a real calendar date.
        try:
            date_iso = datetime.strptime(date_iso[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return jsonify({"ok": False,
                            "error": "date must be YYYY-MM-DD"}), 400

    inv = load_inventory()
    usage = load_usage()

    # Index existing forecast-daily entries by (item_key, YYYY-MM-DD).
    # Reversed entries don't count — the slot is "open" to re-fill.
    have_today: set[tuple[str, str]] = set()
    for e in usage:
        if e.get("source") != "forecast-daily":
            continue
        if e.get("reversed"):
            continue
        # Keyed on the day the burn is FOR (forecast_date), not the day it was
        # written: a backfill call with {"date": <missed day>} made before the
        # 05:05 run stamped today's timestamp, and today's real burn then
        # skipped as "already done". Rows predating forecast_date fall back.
        ts = (e.get("forecast_date") or e.get("timestamp") or "")[:10]
        if not ts:
            continue
        have_today.add((e.get("item_key") or "", ts))

    applied: list[dict] = []
    skipped: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for key, item in inv.items():
        wkly = float(item.get("weekly_usage") or 0)
        if wkly <= 0:
            continue
        wh = item.get("warehouse") or ""
        if wh_prefix and not wh.startswith(wh_prefix):
            continue
        if (key, date_iso) in have_today:
            skipped.append(item.get("name") or key)
            continue
        daily_amount = round(wkly / 7.0, 4)
        if daily_amount <= 0:
            continue
        cur_qty = float(item.get("quantity") or 0)
        new_qty = round(cur_qty - daily_amount, 4)
        # Floor at 0 — we don't model negative inventory; the operator
        # will see the SKU pinned at 0 and reorder.
        if new_qty < 0:
            new_qty = 0.0
            daily_amount = round(cur_qty, 4)
            if daily_amount <= 0:
                # Already at zero; nothing to decrement, but record
                # nothing so the SKU stays eligible tomorrow.
                continue
        applied.append({
            "name": item.get("name"),
            "warehouse": wh,
            "weekly_usage": wkly,
            "daily_amount": daily_amount,
            "old_quantity": cur_qty,
            "new_quantity": new_qty,
        })
        if not dry_run:
            entry = {
                "item_key": key,
                "item_name": item.get("name") or "",
                "amount": daily_amount,
                "unit": item.get("unit") or "",
                "note": f"Forecast burn (weekly_usage/7) for {date_iso}",
                "timestamp": now_iso,
                "source": "forecast-daily",
                "forecast_date": date_iso,
            }
            if wh:
                entry["warehouse"] = wh
            v = _variety_from_name(item.get("name") or "")
            if v:
                entry["variety"] = v
            usage.append(entry)
            item["quantity"] = new_qty
            item["updated"] = now_iso

    if not dry_run and applied:
        save_inventory_and_usage(inv, usage)

    return jsonify({
        "ok": True,
        "date": date_iso,
        "dry_run": dry_run,
        "applied": applied,
        "skipped_existing": skipped,
    })


@reporting_bp.route("/api/forecast/backfill-historical", methods=["POST"])
@with_data_lock
def api_forecast_backfill_historical():
    """One-shot opening reconciliation between lot production and on-hand.

    Why this exists:
        Production lots get recorded as they arrive (each PO bakes new
        lots into data/production.json), but FIFO consumption only ever
        knew about *future* burns through the usage ledger. There was no
        equivalent for the "between PO arrival and the first vendor
        snapshot" gap — so the lots-by-pair view sat at cs_consumed=0
        forever, even on SKUs whose on-hand was clearly much smaller
        than total cases received.

    What this does:
        For each (warehouse, variety) pair on an inventory item:

            target_consumed = max(0, sum(lots_produced) - quantity)
            existing_consumed = sum(positive non-reversed usage entries)
            delta = target_consumed - existing_consumed

        If delta > 0, append one positive usage event of `delta` tagged
        `source = "historical-backfill"` so FIFO drains oldest-first
        until the lot remaining sums match the on-hand quantity. The
        endpoint does NOT touch item.quantity — quantity is already
        ground truth from the vendor; this purely seeds the consumption
        side of the ledger.

    Body (all optional):
        warehouse   str    limit to one warehouse (e.g. "Zebulon, NC").
                           Default: every warehouse.
        dry_run     bool   compute deltas without writing.

    Returns {ok, applied: [...], no_op: [...], dry_run}.
    """
    from datetime import datetime, timezone
    from inventory_tracker import (
        load_inventory, load_production, load_usage, save_usage,
        _variety_from_name,
    )

    body = request.json or {}
    warehouse = (body.get("warehouse") or "").strip()
    dry_run = bool(body.get("dry_run"))

    inv = load_inventory()
    prod = load_production()
    usage = load_usage()

    # Sum produced cases per (warehouse, variety).
    produced_by_pair: dict[tuple[str, str], float] = {}
    for r in prod:
        wh = r.get("warehouse") or ""
        for L in r.get("lines") or []:
            v = (L.get("variety") or "").strip()
            if not wh or not v:
                continue
            produced_by_pair[(wh, v)] = (
                produced_by_pair.get((wh, v), 0.0)
                + float(L.get("cs_count") or 0)
            )

    # Existing positive (non-reversed) consumption per (warehouse, variety).
    inv_for_lookup = inv
    consumed_by_pair: dict[tuple[str, str], float] = {}
    for e in usage:
        if e.get("reversed"):
            continue
        if e.get("source") == "reversal":
            continue
        amt = float(e.get("amount") or 0)
        if amt <= 0:
            continue
        e_wh = e.get("warehouse") or ""
        e_var = e.get("variety") or ""
        if not e_wh or not e_var:
            it = inv_for_lookup.get(e.get("item_key") or "") or {}
            e_wh = e_wh or it.get("warehouse", "")
            e_var = e_var or _variety_from_name(
                it.get("name") or e.get("item_name") or "")
        if not e_wh or not e_var:
            continue
        consumed_by_pair[(e_wh, e_var)] = (
            consumed_by_pair.get((e_wh, e_var), 0.0) + amt
        )

    applied: list[dict] = []
    no_op: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for key, item in inv.items():
        wh = item.get("warehouse") or ""
        if warehouse and wh != warehouse:
            continue
        name = item.get("name") or ""
        v = _variety_from_name(name)
        if not wh or not v:
            continue
        produced = produced_by_pair.get((wh, v), 0.0)
        if produced <= 0:
            continue
        cur_qty = float(item.get("quantity") or 0)
        target_consumed = max(0.0, produced - cur_qty)
        existing = consumed_by_pair.get((wh, v), 0.0)
        delta = round(target_consumed - existing, 4)
        if delta <= 0.001:
            no_op.append({
                "name": name, "warehouse": wh,
                "produced": produced, "quantity": cur_qty,
                "target_consumed": target_consumed,
                "existing_consumed": existing,
            })
            continue
        applied.append({
            "name": name, "warehouse": wh,
            "produced": produced, "quantity": cur_qty,
            "target_consumed": target_consumed,
            "existing_consumed": existing,
            "backfill_amount": delta,
        })
        if not dry_run:
            usage.append({
                "item_key": key,
                "item_name": name,
                "amount": delta,
                "unit": item.get("unit") or "cs",
                "note": (f"Opening reconciliation: align FIFO lots "
                         f"with vendor on-hand ({cur_qty} cs)"),
                "timestamp": now_iso,
                "source": "historical-backfill",
                "warehouse": wh,
                "variety": v,
            })

    if not dry_run and applied:
        save_usage(usage)

    return jsonify({
        "ok": True,
        "warehouse": warehouse or "(all)",
        "dry_run": dry_run,
        "applied": applied,
        "no_op": no_op,
    })


@reporting_bp.route("/api/forecast/true-up", methods=["POST"])
@with_data_lock
def api_forecast_true_up():
    """Reconcile against a vendor on-hand snapshot.

    The vendor's number is ground truth. We reverse every uncovered
    "forecast-daily" entry on each named SKU since the last vendor-truth
    (or since the SKU was added if there has never been one), then post a
    single "vendor-truth" usage entry equal to:

        actual_used = prior_truth_qty + arrivals_since - reported_qty

    where:
        prior_truth_qty   = item.quantity (live) + forecast_burned - arrivals_since
                            (i.e. what on-hand WAS at the last truth date)
        arrivals_since    = sum of negative-amount entries since last truth
        forecast_burned   = sum of positive forecast-daily entries since last
                            truth that are NOT already reversed
        reported_qty      = the new vendor snapshot

    If actual_used <= 0 we skip the positive entry (vendor reports more
    than expected — that's a count correction, not a consumption event).

    Quantity is set to reported_qty in inventory.json so the dashboard
    matches the vendor's number.

    Body:
      warehouse     str    label that matches inventory items' `warehouse`
                           field (e.g. "Zebulon, NC"). Required.
      reported_at   str    ISO date/datetime of the snapshot. Defaults to
                           now (UTC).
      items         list   [{name: str, reported_qty: float}, ...]

    Returns {ok, reported_at, reconciled: [...], skipped: [...]}.
    """
    from datetime import datetime, timezone
    from inventory_tracker import (
        load_inventory, save_inventory, load_usage, save_usage,
        _variety_from_name,
    )

    body = request.json or {}
    warehouse = (body.get("warehouse") or "").strip()
    if not warehouse:
        return jsonify({"ok": False, "error": "warehouse required"}), 400
    items_in = body.get("items") or []
    if not isinstance(items_in, list) or not items_in:
        return jsonify({"ok": False, "error": "items must be a non-empty list"}), 400
    reported_at = (body.get("reported_at") or "").strip()
    if not reported_at:
        reported_at = datetime.now(timezone.utc).isoformat()
    dry_run = bool(body.get("dry_run"))

    inv = load_inventory()
    usage = load_usage()

    # Build a lookup of usage entries by item_key for fast filtering.
    by_key: dict[str, list[dict]] = {}
    for idx, e in enumerate(usage):
        k = e.get("item_key") or ""
        if k:
            by_key.setdefault(k, []).append((idx, e))

    reconciled: list[dict] = []
    skipped: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for payload in items_in:
        name = (payload.get("name") or "").strip()
        if not name:
            skipped.append({"name": "", "reason": "missing name"})
            continue
        try:
            reported_qty = float(payload.get("reported_qty"))
        except (TypeError, ValueError):
            skipped.append({"name": name, "reason": "reported_qty not numeric"})
            continue
        key = name.lower()
        item = inv.get(key)
        if not item:
            skipped.append({"name": name, "reason": "SKU not found"})
            continue
        if (item.get("warehouse") or "") != warehouse:
            skipped.append({"name": name, "reason": f"warehouse mismatch ({item.get('warehouse')})"})
            continue

        # Find last vendor-truth timestamp for this SKU (exclusive lower
        # bound for the reconciliation window). Empty string = no prior
        # truth, so window is "all history".
        prior_truth_ts = ""
        for _idx, e in by_key.get(key, []):
            if e.get("source") != "vendor-truth":
                continue
            if e.get("reversed"):
                continue
            ts = e.get("timestamp") or ""
            if ts > prior_truth_ts:
                prior_truth_ts = ts

        arrivals_since = 0.0       # sum of negative amounts (restocks)
        forecast_burned = 0.0      # positive forecast-daily not reversed
        reverse_indices: list[int] = []
        for idx, e in by_key.get(key, []):
            if e.get("reversed"):
                continue
            ts = e.get("timestamp") or ""
            if prior_truth_ts and ts <= prior_truth_ts:
                continue
            amt = float(e.get("amount") or 0)
            if amt < 0:
                arrivals_since += -amt
            elif amt > 0 and e.get("source") == "forecast-daily":
                forecast_burned += amt
                reverse_indices.append(idx)
            # Positive non-forecast entries (manual /api/use, prior
            # vendor-truth) are left untouched — they represent real
            # consumption the operator already booked.

        cur_qty = float(item.get("quantity") or 0)
        prior_truth_qty = cur_qty + forecast_burned - arrivals_since
        actual_used = prior_truth_qty + arrivals_since - reported_qty
        actual_used = round(actual_used, 4)

        entry_record = {
            "name": name,
            "warehouse": warehouse,
            "prior_truth_ts": prior_truth_ts or None,
            "prior_truth_qty": round(prior_truth_qty, 4),
            "arrivals_since": round(arrivals_since, 4),
            "forecast_burned_reversed": round(forecast_burned, 4),
            "reported_qty": reported_qty,
            "actual_used": actual_used,
            "old_quantity": cur_qty,
            "new_quantity": reported_qty,
        }
        reconciled.append(entry_record)

        if dry_run:
            continue

        # Reverse the forecast-daily entries that fell in this window.
        for idx in reverse_indices:
            usage[idx]["reversed"] = True
            usage[idx]["reversed_at"] = now_iso
            usage[idx]["reversed_by"] = "vendor-truth"

        # Post the vendor-truth event. Skip when actual_used <= 0
        # (vendor reported MORE than expected — a positive count
        # correction we model as the quantity bump only, no consumption).
        if actual_used > 0:
            truth_entry = {
                "item_key": key,
                "item_name": item.get("name") or name,
                "amount": actual_used,
                "unit": item.get("unit") or "",
                "note": f"Vendor on-hand true-up at {reported_at}",
                "timestamp": now_iso,
                "source": "vendor-truth",
                "reported_at": reported_at,
                "warehouse": warehouse,
            }
            v = _variety_from_name(item.get("name") or name)
            if v:
                truth_entry["variety"] = v
            usage.append(truth_entry)

        item["quantity"] = reported_qty
        item["updated"] = now_iso
        item["last_synced"] = now_iso
        item["last_synced_from"] = "vendor-truth"
        item["last_count_at"] = now_iso

    if not dry_run and reconciled:
        save_inventory_and_usage(inv, usage)

    return jsonify({
        "ok": True,
        "warehouse": warehouse,
        "reported_at": reported_at,
        "dry_run": dry_run,
        "reconciled": reconciled,
        "skipped": skipped,
    })


@reporting_bp.route("/api/report")
def api_report():
    inv = load_inventory()
    usage = load_usage()

    total_value = sum(i["quantity"] * i["price"] for i in inv.values())
    total_items = len(inv)
    low_stock = [i for i in inv.values() if i["quantity"] <= i["low_stock_threshold"]]

    consumed: dict = {}
    restocked: dict = {}
    for e in usage:
        key = e.get("item_key")
        if not key:
            continue
        # Undone rows and audit rows are not consumption: a reversed forecast
        # burn plus its vendor-truth replacement counted twice, and a reopen
        # reversal counted as a use.
        if e.get("reversed") or e.get("source") in ("reversal", "on_order_absorbed"):
            continue
        amt = float(e.get("amount") or 0)
        if amt < 0:
            restocked[key] = restocked.get(key, 0) + abs(amt)
        else:
            consumed[key] = consumed.get(key, 0) + amt

    top_consumed = sorted(
        [{"key": k, "name": inv.get(k, {}).get("name", k),
          "unit": inv.get(k, {}).get("unit", ""), "total": v}
         for k, v in consumed.items()],
        key=lambda x: x["total"], reverse=True
    )[:10]

    top_restocked = sorted(
        [{"key": k, "name": inv.get(k, {}).get("name", k),
          "unit": inv.get(k, {}).get("unit", ""), "total": v}
         for k, v in restocked.items()],
        key=lambda x: x["total"], reverse=True
    )[:10]

    return jsonify({
        "total_value": round(total_value, 2),
        "total_items": total_items,
        "low_stock_count": len(low_stock),
        "low_stock": low_stock,
        "top_consumed": top_consumed,
        "top_restocked": top_restocked,
        "total_usage_events": len(usage),
    })


@reporting_bp.route("/api/report-status")
def api_report_status():
    """JSON: which distributor warehouses have sent this week\'s inventory & usage report."""
    from integrations import report_status as _rs
    force = request.args.get("refresh") in ("1", "true", "yes", "on")
    return jsonify(_rs.get_status(force=force))


@reporting_bp.route("/report-status")
def report_status_page():
    """Mobile-friendly weekly-report status page (read-only; requires login)."""
    from flask import Response
    from integrations import report_status as _rs
    force = request.args.get("refresh") in ("1", "true", "yes", "on")
    html = _rs.render_html(_rs.get_status(force=force))
    return Response(html, mimetype="text/html")


@reporting_bp.route("/api/export.xlsx")
def api_export_xlsx():
    from openpyxl import Workbook
    from export_bagels_xlsx import _write_summary_sheet, _write_items_sheet

    inv = load_inventory()
    items = list(inv.values())
    cheney = [i for i in items if (i.get("distributor") or "") == "Cheney Brothers"]
    usfoods = [i for i in items if (i.get("distributor") or "") == "US Foods"]

    wb = Workbook()
    _write_summary_sheet(wb.active, inv)
    wb.active.title = "Summary"
    _write_items_sheet(wb.create_sheet("Unified List"), items)
    _write_items_sheet(wb.create_sheet("Cheney Brothers"), cheney)
    _write_items_sheet(wb.create_sheet("US Foods"), usfoods)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="bagel_inventory.xlsx",
    )
