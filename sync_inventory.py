#!/usr/bin/env python3
"""Sync on-hand bagel quantities from Cheney Brothers and US Foods.

For each distributor client, fetches current inventory (live API if
credentials are configured, otherwise a CSV drop at
integrations/<slug>_inventory.csv) and applies the numbers to our local
inventory. Every quantity change is written to the usage log so it is
auditable.

Usage:
    python sync_inventory.py              # sync both distributors
    python sync_inventory.py --dry-run    # show what would change, make no edits
    python sync_inventory.py --cheney     # sync only Cheney Brothers
    python sync_inventory.py --usfoods    # sync only US Foods
"""

import sys
from datetime import datetime
from typing import Iterable

from integrations import (
    CheneyBrothersClient, USFoodsClient,
    DistributorClient, NotConfiguredError, SyncItem,
)
from inventory_tracker import load_inventory, save_inventory, load_usage, save_usage


# Must match the naming convention in seed_bagels.py
DISTRIBUTOR_TAG = {"Cheney Brothers": "CB", "US Foods": "USF"}


def _warehouse_short(full: str) -> str:
    return full.split(",")[0].strip() if full else ""


def _candidate_names(item: SyncItem) -> list[str]:
    """Build plausible local SKU names for a sync row."""
    candidates: list[str] = []
    if item.name:
        candidates.append(item.name)
    if item.variety and item.warehouse and item.distributor:
        tag = DISTRIBUTOR_TAG.get(item.distributor, item.distributor)
        short = _warehouse_short(item.warehouse)
        candidates.append(f"{item.variety} Bagel 4oz [{tag} - {short}]")
    return candidates


def _find_local_key(inv: dict, item: SyncItem) -> str | None:
    for name in _candidate_names(item):
        key = name.lower().strip()
        if key in inv:
            return key
    return None


def _sync_one(client: DistributorClient, inv: dict, usage: list,
              dry_run: bool) -> dict:
    report = {
        "distributor": client.name,
        "source": "live" if client._has_live_credentials() else "csv",
        "status": "ok",
        "fetched": 0,
        "updated": 0,
        "unchanged": 0,
        "unmatched": [],
        "changes": [],
        "error": None,
    }

    try:
        items: Iterable[SyncItem] = client.fetch_inventory()
    except NotConfiguredError as e:
        report["status"] = "not_configured"
        report["error"] = str(e)
        return report

    now = datetime.now().isoformat()
    for sync_item in items:
        report["fetched"] += 1
        key = _find_local_key(inv, sync_item)
        if key is None:
            report["unmatched"].append(
                sync_item.name or f"{sync_item.variety}@{sync_item.warehouse}"
            )
            continue

        item = inv[key]
        old_qty = item["quantity"]
        old_price = item.get("price", 0.0)
        new_qty = sync_item.quantity
        new_price = sync_item.price if sync_item.price is not None else old_price

        qty_changed = abs(new_qty - old_qty) > 1e-9
        price_changed = sync_item.price is not None and abs(new_price - old_price) > 1e-9

        if not qty_changed and not price_changed:
            report["unchanged"] += 1
            continue

        change = {
            "name": item["name"],
            "warehouse": item.get("warehouse", ""),
            "old_quantity": old_qty,
            "new_quantity": new_qty,
            "delta": round(new_qty - old_qty, 2),
            "old_price": old_price,
            "new_price": new_price,
        }
        report["changes"].append(change)
        report["updated"] += 1

        if dry_run:
            continue

        item["quantity"] = new_qty
        if price_changed:
            item["price"] = new_price
        item["updated"] = now
        item["last_synced"] = now
        item["last_synced_from"] = client.name

        if qty_changed:
            usage.append({
                "item_key": key,
                "item_name": item["name"],
                "amount": round(old_qty - new_qty, 2),  # positive = consumed
                "unit": item["unit"],
                "note": f"Synced from {client.name}"
                        + (f" ({report['source']})" if report["source"] != "live" else ""),
                "timestamp": now,
            })

    return report


def sync_all(clients: list[DistributorClient] | None = None,
             dry_run: bool = False) -> list[dict]:
    if clients is None:
        clients = [CheneyBrothersClient(), USFoodsClient()]
    inv = load_inventory()
    usage = load_usage()
    reports = [_sync_one(c, inv, usage, dry_run) for c in clients]
    if not dry_run:
        save_inventory(inv)
        save_usage(usage)
    return reports


def _print_report(reports: list[dict], dry_run: bool):
    print()
    print("=" * 72)
    print(f"  {'INVENTORY SYNC' + (' (DRY RUN)' if dry_run else ''):^68}")
    print("=" * 72)
    for r in reports:
        print(f"\n  {r['distributor']} [{r['source']}]: {r['status']}")
        if r["status"] == "not_configured":
            print(f"    {r['error']}")
            continue
        print(f"    fetched   : {r['fetched']}")
        print(f"    updated   : {r['updated']}")
        print(f"    unchanged : {r['unchanged']}")
        print(f"    unmatched : {len(r['unmatched'])}")
        for u in r["unmatched"]:
            print(f"      - {u}")
        for c in r["changes"][:20]:
            sign = "+" if c["delta"] >= 0 else ""
            print(f"      {c['name']:<48} {c['old_quantity']:>7.1f} -> "
                  f"{c['new_quantity']:>7.1f}  ({sign}{c['delta']})")
        if len(r["changes"]) > 20:
            print(f"      ... and {len(r['changes']) - 20} more")
    print()


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    only_cheney = "--cheney" in args
    only_usfoods = "--usfoods" in args

    clients: list[DistributorClient] = []
    if only_cheney or not only_usfoods:
        clients.append(CheneyBrothersClient())
    if only_usfoods or not only_cheney:
        clients.append(USFoodsClient())
    # De-dup when both flags are set
    seen = set()
    clients = [c for c in clients if not (c.name in seen or seen.add(c.name))]

    reports = sync_all(clients, dry_run=dry_run)
    _print_report(reports, dry_run)


if __name__ == "__main__":
    main()
