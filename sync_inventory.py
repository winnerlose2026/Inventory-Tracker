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
    DistributorClient, EmailInboxClient, NotConfiguredError, SyncItem,
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

        case_cost_changed = (sync_item.case_cost is not None
                             and abs(sync_item.case_cost - (item.get("case_cost") or 0)) > 1e-9)
        case_size_changed = (sync_item.case_size is not None
                             and sync_item.case_size != (item.get("case_size") or 0))
        weekly_changed = (sync_item.weekly_usage is not None
                          and abs(sync_item.weekly_usage - (item.get("weekly_usage") or 0)) > 1e-9)

        if not (qty_changed or price_changed or case_cost_changed
                or case_size_changed or weekly_changed):
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
        if case_cost_changed:
            item["case_cost"] = sync_item.case_cost
        if case_size_changed:
            item["case_size"] = sync_item.case_size
        if weekly_changed:
            item["weekly_usage"] = sync_item.weekly_usage
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


# ---------------------------------------------------------------------------
# Email scanning
# ---------------------------------------------------------------------------

def _po_rev_int(s) -> int:
    """Coerce a PO revision string (e.g. '0000002' or '2') to an int.
    Returns 0 when the value is missing or non-numeric so older-than-any-
    real-rev comparisons still work."""
    if not s:
        return 0
    try:
        return int(str(s).lstrip("0") or "0")
    except (ValueError, TypeError):
        return 0


def _highest_applied_rev(usage: list, po_number: str) -> tuple[int, list[int]]:
    """Return (highest_rev_int, indices_of_active_entries) for a PO in the usage log.
    Active = tagged with this po_number and NOT yet marked superseded_by_revision.
    Used to decide whether an incoming revision supersedes or duplicates."""
    highest = 0
    indices = []
    for idx, entry in enumerate(usage):
        if entry.get("po_number") != po_number:
            continue
        if entry.get("superseded_by_revision"):
            continue
        if entry.get("reversal_of_revision"):
            # Reversal audit rows aren't themselves "applied restock" — skip.
            continue
        rev_int = _po_rev_int(entry.get("po_revision"))
        if rev_int > highest:
            highest = rev_int
        indices.append(idx)
    return highest, indices


def _reverse_po_entries(po_number: str, new_rev: str, active_indices: list[int],
                        inv: dict, usage: list, now: str, report: dict,
                        dry_run: bool) -> None:
    """Reverse previously-applied restock entries for a PO so a new revision
    can replace them. Inventory on-hand is rolled back; entries are marked
    with superseded_by_revision=<new_rev>; a mirror reversal entry is
    appended for audit."""
    new_rev_tag = str(new_rev or "")
    for idx in active_indices:
        entry = usage[idx]
        key = entry.get("item_key", "")
        item = inv.get(key)
        if item is None:
            # SKU got removed since the original restock — nothing to undo
            # on-hand for, but still mark the log entry superseded.
            if not dry_run:
                entry["superseded_by_revision"] = new_rev_tag
                entry["superseded_at"] = now
            continue

        # entry["amount"] is negative for a restock; reversing = add it back
        # to on-hand. e.g. original restock of 24 -> amount=-24 -> on_hand -= 24.
        reverse_delta = entry.get("amount", 0.0)
        old_qty = item["quantity"]
        new_qty = round(old_qty + reverse_delta, 2)

        report["changes"].append({
            "name": item.get("name", key),
            "warehouse": item.get("warehouse", ""),
            "event_type": "po_reversal",
            "old_quantity": old_qty,
            "new_quantity": new_qty,
            "delta": round(new_qty - old_qty, 2),
            "po_number": po_number,
            "superseded_revision": entry.get("po_revision", ""),
            "superseded_by_revision": new_rev_tag,
        })

        if new_qty < 0:
            report.setdefault("warnings", []).append(
                f"PO {po_number} rev {new_rev_tag}: reversing prior rev "
                f"{entry.get('po_revision', '?')} drives {item.get('name', key)} "
                f"below zero ({old_qty} -> {new_qty}). On-hand probably had "
                "consumption between revisions — please reconcile."
            )

        if dry_run:
            continue

        item["quantity"] = new_qty
        item["updated"] = now
        entry["superseded_by_revision"] = new_rev_tag
        entry["superseded_at"] = now

        # Append a reversal audit row so the usage log still reconciles.
        usage.append({
            "item_key": key,
            "item_name": item.get("name", key),
            "amount": -reverse_delta,  # flip sign vs original (original was -24 -> +24 "consumed")
            "unit": item.get("unit", entry.get("unit", "")),
            "note": (f"Reversed by PO {po_number} rev {new_rev_tag} "
                     f"(supersedes rev {entry.get('po_revision', '?')})"),
            "timestamp": now,
            "po_number": po_number,
            "po_revision": entry.get("po_revision", ""),
            "reversal_of_revision": entry.get("po_revision", ""),
        })


def _apply_email_event(evt, inv: dict, usage: list, now: str,
                       report: dict, dry_run: bool) -> None:
    """Apply a single EmailEvent to the inventory/usage log."""
    key = _find_local_key(inv, evt.item)
    if key is None:
        report["unmatched"].append(
            evt.item.name or f"{evt.item.variety}@{evt.item.warehouse}"
        )
        return
    item = inv[key]
    old_qty = item["quantity"]
    amount = evt.item.quantity

    if evt.event_type == "on_hand":
        if abs(amount - old_qty) < 1e-9:
            report["unchanged"] += 1
            return
        new_qty = amount
        delta_usage = round(old_qty - new_qty, 2)  # positive = consumed
        note = f"Email on-hand sync (subject: {evt.source_subject[:60]})"
    elif evt.event_type == "restock":
        new_qty = old_qty + amount
        delta_usage = -round(amount, 2)  # negative = restock in the log
        note = f"Email restock (subject: {evt.source_subject[:60]})"
    elif evt.event_type == "usage":
        new_qty = max(0.0, old_qty - amount)
        delta_usage = round(amount, 2)  # positive = consumed
        note = f"Email usage report (subject: {evt.source_subject[:60]})"
    else:
        return

    report["changes"].append({
        "name": item["name"],
        "warehouse": item.get("warehouse", ""),
        "event_type": evt.event_type,
        "old_quantity": old_qty,
        "new_quantity": new_qty,
        "delta": round(new_qty - old_qty, 2),
    })
    report["updated"] += 1

    if dry_run:
        return

    item["quantity"] = new_qty
    item["updated"] = now
    item["last_synced"] = now
    item["last_synced_from"] = "Email Inbox"
    entry = {
        "item_key": key,
        "item_name": item["name"],
        "amount": delta_usage,
        "unit": item["unit"],
        "note": note,
        "timestamp": now,
    }
    # Tag with PO number/revision so a later revision of the same PO can
    # locate and reverse this entry (see _reverse_po_entries).
    po_num = getattr(evt, "po_number", "")
    if po_num:
        entry["po_number"] = po_num
        entry["po_revision"] = getattr(evt, "po_revision", "") or ""
    usage.append(entry)


def scan_email(dry_run: bool = False,
               client: EmailInboxClient | None = None) -> dict:
    """Scan the mailbox and apply extracted events. Returns a report dict."""
    client = client or EmailInboxClient()
    report = {
        "distributor": "Email Inbox",
        "source": client.source(),
        "status": "ok",
        "fetched": 0,
        "updated": 0,
        "unchanged": 0,
        "unmatched": [],
        "changes": [],
        "error": None,
        "messages_seen": 0,
        "messages_parsed": 0,
        "by_event_type": {"on_hand": 0, "restock": 0, "usage": 0},
    }
    try:
        scan = client.scan()
    except NotConfiguredError as exc:
        report["status"] = "not_configured"
        report["error"] = str(exc)
        return report

    report["messages_seen"] = scan.messages_seen
    report["messages_parsed"] = scan.messages_parsed
    report["fetched"] = len(scan.events)
    if scan.errors:
        report["error"] = "; ".join(scan.errors[:5])

    inv = load_inventory()
    usage = load_usage()
    now = datetime.now().isoformat()

    # Count event types up front so the by-type totals reflect everything
    # the scanner produced, even if a later revision ends up skipped.
    for evt in scan.events:
        report["by_event_type"][evt.event_type] = \
            report["by_event_type"].get(evt.event_type, 0) + 1

    # Split events into PO-tagged groups (subject to revision semantics) and
    # untagged events (applied as before). All events from a single parsed
    # PO share the same po_number and po_revision, so grouping is safe.
    po_groups: dict[str, list] = {}
    non_po_events: list = []
    for evt in scan.events:
        po_num = getattr(evt, "po_number", "")
        if po_num:
            po_groups.setdefault(po_num, []).append(evt)
        else:
            non_po_events.append(evt)

    report["po_revisions_skipped"] = []
    report["po_revisions_superseded"] = []

    for po_num, grp in po_groups.items():
        # All events in a group share the same revision; take it from the
        # first one. Fall back to "" if the parser didn't set it.
        new_rev = getattr(grp[0], "po_revision", "") or ""
        new_rev_int = _po_rev_int(new_rev)
        existing_rev_int, active_idx = _highest_applied_rev(usage, po_num)

        if existing_rev_int and new_rev_int <= existing_rev_int:
            # Idempotent skip: the same-or-older revision is already applied.
            # This makes re-scans safe (e.g. after a restart) and means a
            # duplicate of rev1 after rev2 won't undo rev2.
            report["po_revisions_skipped"].append(
                f"PO {po_num} rev {new_rev or '(none)'}: already applied at "
                f"rev {existing_rev_int} or higher - skipped {len(grp)} event(s)."
            )
            continue

        if existing_rev_int:
            # Higher revision arriving for a PO we've already booked - reverse
            # prior entries before posting the new ones.
            _reverse_po_entries(po_num, new_rev, active_idx, inv, usage, now,
                                report, dry_run)
            report["po_revisions_superseded"].append(
                f"PO {po_num}: rev {existing_rev_int} superseded by rev "
                f"{new_rev_int} ({len(active_idx)} line(s) reversed)."
            )

        for evt in grp:
            _apply_email_event(evt, inv, usage, now, report, dry_run)

    for evt in non_po_events:
        _apply_email_event(evt, inv, usage, now, report, dry_run)

    if not dry_run:
        save_inventory(inv)
        save_usage(usage)
    return report


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
    only_email = "--email" in args

    reports: list[dict] = []

    if only_email:
        reports.append(scan_email(dry_run=dry_run))
    else:
        clients: list[DistributorClient] = []
        if only_cheney or not only_usfoods:
            clients.append(CheneyBrothersClient())
        if only_usfoods or not only_cheney:
            clients.append(USFoodsClient())
        seen = set()
        clients = [c for c in clients if not (c.name in seen or seen.add(c.name))]
        reports.extend(sync_all(clients, dry_run=dry_run))
        # Always offer email as an optional pass; it's silent when unconfigured.
        if "--with-email" in args:
            reports.append(scan_email(dry_run=dry_run))
    _print_report(reports, dry_run)


if __name__ == "__main__":
    main()
