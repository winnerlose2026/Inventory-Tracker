"""Toast Standard API client for live product-mix fetching.

This is a thin wrapper used by /api/report/toast-sales when the user
picks a date whose week isn't yet cached in sales.json. It pages through
the Orders Bulk API for a single (restaurant_guid, business_date) and
aggregates selections into the same row shape the cached store uses.

Env vars expected on Render:
  TOAST_CLIENT_ID
  TOAST_CLIENT_SECRET
  TOAST_API_HOSTNAME       e.g. https://ws-api.toasttab.com
  TOAST_USER_ACCESS_TYPE   defaults to TOAST_MACHINE_CLIENT

If any of those are missing the module's public functions raise
ToastClientNotConfigured so callers can degrade gracefully instead of
crashing the request.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable


class ToastClientNotConfigured(RuntimeError):
    """Raised when TOAST_CLIENT_ID/SECRET/HOSTNAME aren't set."""


class ToastAPIError(RuntimeError):
    """Raised when Toast returns a non-2xx response we can't recover from."""


def _config() -> dict:
    cid    = os.environ.get("TOAST_CLIENT_ID", "").strip()
    secret = os.environ.get("TOAST_CLIENT_SECRET", "").strip()
    host   = os.environ.get("TOAST_API_HOSTNAME", "").strip().rstrip("/")
    if not (cid and secret and host):
        raise ToastClientNotConfigured(
            "Toast not configured: set TOAST_CLIENT_ID, "
            "TOAST_CLIENT_SECRET, and TOAST_API_HOSTNAME on the service.")
    return {
        "client_id":     cid,
        "client_secret": secret,
        "host":          host,
        "user_access":   os.environ.get("TOAST_USER_ACCESS_TYPE",
                                        "TOAST_MACHINE_CLIENT").strip(),
    }


_token_lock = threading.Lock()
_token_cache: dict = {"access_token": None, "expires_at": 0.0}


def _access_token() -> str:
    """Fetch + cache a Toast OAuth token. Refresh ~5 minutes before
    expiry to avoid mid-request 401s."""
    cfg = _config()
    with _token_lock:
        now = time.time()
        if _token_cache["access_token"] and now < _token_cache["expires_at"]:
            return _token_cache["access_token"]
        payload = json.dumps({
            "clientId":       cfg["client_id"],
            "clientSecret":   cfg["client_secret"],
            "userAccessType": cfg["user_access"],
        }).encode()
        req = urllib.request.Request(
            f"{cfg['host']}/authentication/v1/authentication/login",
            data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            raise ToastAPIError(
                f"Toast auth failed: HTTP {e.code} {e.reason} - "
                f"{detail}") from e
        token_obj = (body or {}).get("token") or {}
        access    = token_obj.get("accessToken")
        ttl_s     = int(token_obj.get("expiresIn") or 0)
        if not access:
            raise ToastAPIError(f"Toast auth returned no accessToken: {body}")
        _token_cache["access_token"] = access
        _token_cache["expires_at"]   = now + max(60, (ttl_s or 3600) - 300)
        return access


def _get(path: str, params: dict, restaurant_guid: str) -> dict:
    """GET a Toast Standard API path with auth + Toast-Restaurant-External-ID
    header. Returns parsed JSON. Retries once on 401 (stale token)."""
    cfg = _config()
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}"
                  for k, v in (params or {}).items() if v is not None)
    url = f"{cfg['host']}{path}"
    if qs:
        url = f"{url}?{qs}"

    def _do(token: str):
        req = urllib.request.Request(
            url, headers={
                "Authorization":                f"Bearer {token}",
                "Toast-Restaurant-External-ID": restaurant_guid,
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "null")

    try:
        return _do(_access_token())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            with _token_lock:
                _token_cache["access_token"] = None
                _token_cache["expires_at"]   = 0.0
            return _do(_access_token())
        if e.code == 404:
            # No orders for that date - return empty rather than raising.
            return []
        body = ""
        try:
            body = e.read().decode()[:300]
        except Exception:
            pass
        raise ToastAPIError(
            f"Toast {path} failed: HTTP {e.code} {e.reason} - {body}") from e


def _yyyymmdd(date_iso: str) -> str:
    return date_iso.replace("-", "")


def fetch_product_mix(restaurant_guid: str, business_date: str,
                      location_name: str = "") -> list:
    """Return aggregated product-mix rows for one (restaurant, business_date).

    Row shape matches /api/sales/ingest:
      {restaurant_guid, location, business_date, item_guid, item,
       menu_group, qty, gross, net}

    Walks /orders/v2/ordersBulk pages and sums selections by item_guid.
    Voided + deleted selections are ignored. Returns [] when Toast has
    no orders for that date.
    """
    if not restaurant_guid or not business_date:
        return []

    yyyymmdd = _yyyymmdd(business_date)
    page = 1
    page_size = 100
    by_item: dict = {}

    while True:
        body = _get(
            "/orders/v2/ordersBulk",
            {"businessDate": yyyymmdd,
             "pageSize":     page_size,
             "page":         page},
            restaurant_guid,
        )
        orders = body if isinstance(body, list) else (body or {}).get("orders") or []
        if not orders:
            break

        for order in orders:
            if order.get("voided") or order.get("deleted"):
                continue
            for check in (order.get("checks") or []):
                if check.get("voided") or check.get("deleted"):
                    continue
                for sel in (check.get("selections") or []):
                    if sel.get("voided") or sel.get("deleted"):
                        continue
                    item_guid  = ((sel.get("item")      or {}).get("guid")
                                  or sel.get("itemGuid"))
                    menu_group = ((sel.get("itemGroup") or {}).get("name")
                                  or (sel.get("salesCategory") or {}).get("name")
                                  or "")
                    if not item_guid:
                        continue
                    qty   = float(sel.get("quantity")          or 0)
                    price = float(sel.get("price")             or 0)
                    pre_d = float(sel.get("preDiscountPrice")  or price)
                    name  = sel.get("displayName") or sel.get("name") or ""

                    slot = by_item.setdefault(item_guid, {
                        "restaurant_guid": restaurant_guid,
                        "location":        location_name,
                        "business_date":   business_date,
                        "item_guid":       item_guid,
                        "item":            name,
                        "menu_group":      menu_group,
                        "qty":             0,
                        "gross":           0.0,
                        "net":             0.0,
                    })
                    slot["qty"]   += int(qty)
                    slot["gross"] += pre_d
                    slot["net"]   += price
                    if not slot["item"] and name:
                        slot["item"] = name
                    if not slot["menu_group"] and menu_group:
                        slot["menu_group"] = menu_group

        if len(orders) < page_size or page >= 50:
            break
        page += 1

    return list(by_item.values())


def fetch_product_mix_batch(pairs, max_workers: int = 6,
                            timeout_s: float = 28.0) -> list:
    """Fetch product mix for many (restaurant_guid, business_date,
    location_name) tuples concurrently. Returns a flat list of rows.
    Per-pair failures are logged but don't kill the batch."""
    pairs = list(pairs)
    if not pairs:
        return []
    out: list = []
    deadline = time.time() + timeout_s
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(fetch_product_mix, guid, date, name): (guid, date, name)
            for (guid, date, name) in pairs
        }
        try:
            for fut in as_completed(futures, timeout=timeout_s):
                guid, date, name = futures[fut]
                if time.time() > deadline:
                    break
                try:
                    out.extend(fut.result() or [])
                except Exception as e:
                    print(f"[toast_api] fetch failed "
                          f"{name or guid} {date}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[toast_api] batch fetch terminated early: {e}",
                  file=sys.stderr)
    return out


def is_configured() -> bool:
    """True when all required env vars are present. Lets the caller
    fall through to cache-only behavior without raising."""
    try:
        _config()
        return True
    except ToastClientNotConfigured:
        return False
