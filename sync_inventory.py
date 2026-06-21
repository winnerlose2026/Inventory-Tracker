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

import os
import sys
from datetime import datetime, timedelta
from typing import Iterable

from integrations import (
    CheneyBrothersClient, USFoodsClient,
    DistributorClient, EmailInboxClient, NotConfiguredError, SyncItem,
)
from inventory_tracker import (
    load_inventory, save_inventory, load_usage, save_usage,
    load_chefs_warehouse_pos, save_chefs_warehouse_pos,
)


# Must match the naming convention in seed_bagels.py
DISTRIBUTOR_TAG = {"Cheney Brothers": "CB", "US Foods": "USF"}

# Incoming POs are booked to on_order with this lead time before promoting
# into quantity. Overridable via env var for testing.
def _po_lead_days() -> int:
    try:
        return max(0, int(os.environ.get("PO_LEAD_DAYS", "30")))
    except (TypeError, ValueError):
        return 30


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

# USF occasionally re-issues a PO with the literal revision token
# "REPRINT" (an entire copy of the latest state, not a numbered rev).
# In USF's workflow REPRINT is ALWAYS newer than any numbered revision
# that preceded it -- so we sort it after every integer. Same treatment
# for any other non-numeric token we might see in the future.
_REPRINT_REV_SENTINEL = 10_000_000


def _po_rev_int(s) -> int:
    """Coerce a PO revision string to an int suitable for ordering.

    Integer strings ('0000002', '2', '17') round-trip to their int.
    Non-numeric tokens (USF's 'REPRINT', any other free-form revision
    label) sort AFTER any plausible numeric revision via a fixed
    sentinel. Missing / empty strings are treated as rev 0 (older than
    everything) so an unrevised PO doesn't accidentally outrank a real
    revision.
    """
    if not s:
        return 0
    raw = str(s).strip()
    if not raw:
        return 0
    try:
        return int(raw.lstrip("0") or "0")
    except (ValueError, TypeError):
        # Non-numeric token (REPRINT etc.) — treat as latest.
        return _REPRINT_REV_SENTINEL


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


#: Distributors that get an automatic 30-day ETA on every PO. For every
#: other distributor (Chefs Warehouse, DeliBag, Carmela Foods, H&H, etc.)
#: the PO sits in the pending list with no ETA until an operator types a
#: ship_date, which sets arrival_date = ship_date + 7 days.
_AUTO_ETA_DISTRIBUTORS = frozenset({"US Foods", "Cheney Brothers"})


def _apply_po_on_order(evt, item: dict, key: str, now: str,
                       report: dict, dry_run: bool) -> None:
    """Record a PO-tagged restock as a pending on_order entry instead of
    bumping on-hand quantity. The rollover in inventory_tracker promotes
    it once ETA passes — but only when an ETA is actually set.

    Auto-ETA rule (as of 2026-05-27): only US Foods and Cheney Brothers
    POs get a 30-day fallback ETA. For every other distributor we leave
    eta="" so the entry stays pending until an operator manually types a
    ship_date (which triggers arrival_date = ship_date + 7 days)."""
    amount = float(evt.item.quantity or 0)
    if amount <= 0:
        return
    distributor = (evt.item.distributor or item.get("distributor") or "").strip()
    auto_eta = distributor in _AUTO_ETA_DISTRIBUTORS

    lead_days = _po_lead_days() if auto_eta else 0
    # Anchor ordered_at to the PO's actual order date (parsed from the
    # PDF) rather than "now". When a backlogged PO is scanned weeks
    # after it was placed, this lets the 30-day rollover into quantity
    # track real lead time instead of restarting the clock at ingest.
    # Falls back to `now` when the parser didn't surface a date.
    po_date_iso = (getattr(evt, "po_order_date", "") or "").strip()
    if po_date_iso:
        try:
            ordered_at_dt = datetime.fromisoformat(po_date_iso)
        except ValueError:
            ordered_at_dt = None
    else:
        ordered_at_dt = None
    if ordered_at_dt is None:
        try:
            ordered_at_dt = datetime.fromisoformat(now)
        except (TypeError, ValueError):
            ordered_at_dt = datetime.now()
    pending = item.get("on_order") or []
    existing_qty = sum(float(p.get("qty") or 0) for p in pending)

    # Look for an existing pending entry at the SAME (po_number, po_revision).
    # If one is there, this is a re-ingest of the same scan — preserve its
    # ordered_at (and ship_date / arrival_date if the operator set them) so
    # downstream views don't flip to "today" every time the cron re-fires.
    new_rev_tag = getattr(evt, "po_revision", "") or ""
    same_rev_existing = None
    for p in pending:
        if (p.get("po_number") == evt.po_number
                and (p.get("po_revision") or "") == new_rev_tag):
            same_rev_existing = p
            break

    if same_rev_existing is not None and not po_date_iso:
        # Incoming event didn't carry the PDF's Order Date and we already
        # have a date booked on this PO — keep the booked one instead of
        # resetting to now.
        prior = (same_rev_existing.get("ordered_at") or "").strip()
        if prior:
            try:
                ordered_at_dt = datetime.fromisoformat(prior)
            except ValueError:
                pass  # malformed, fall back to whatever ordered_at_dt is

    # ETA stays blank for non-auto-ETA distributors; the rollover skips
    # entries with no resolvable arrival date.
    eta_iso = (ordered_at_dt + timedelta(days=lead_days)).isoformat() if auto_eta else ""

    entry = {
        "qty": amount,
        "unit": item.get("unit", ""),
        "eta": eta_iso,
        "ordered_at": ordered_at_dt.isoformat(),
        "po_number": evt.po_number,
        "po_revision": new_rev_tag,
        "source": "Email Inbox",
        "source_subject": (evt.source_subject or "")[:120],
        "lead_days": lead_days if auto_eta else 0,
    }
    # Carry forward operator-set ship_date / arrival_date too, so a re-scan
    # doesn't wipe them. _apply_po_on_order_ship_date is the only path
    # that sets these; they should outlive any number of re-ingests.
    if same_rev_existing is not None:
        for k in ("ship_date", "arrival_date"):
            if same_rev_existing.get(k):
                entry[k] = same_rev_existing[k]
        # Drop the old in-place so we don't end up with two rows for the
        # same (po, rev). Order preserved below by re-appending.
        pending = [p for p in pending if p is not same_rev_existing]

    report["changes"].append({
        "name": item["name"],
        "warehouse": item.get("warehouse", ""),
        "event_type": "on_order",
        "old_quantity": item["quantity"],
        "new_quantity": item["quantity"],
        "delta": 0,
        "on_order_delta": round(amount, 2),
        "on_order_total": round(existing_qty + amount, 2),
        "eta": eta_iso,
        "po_number": evt.po_number,
        "po_revision": entry["po_revision"],
    })
    report["updated"] += 1
    report.setdefault("by_event_type", {})["on_order"] = \
        report["by_event_type"].get("on_order", 0) + 1

    if dry_run:
        return

    item["on_order"] = pending + [entry]
    item["updated"] = now
    item["last_synced"] = now
    item["last_synced_from"] = "Email Inbox"


def _remove_on_order_by_po(po_number: str, new_rev: str, inv: dict,
                           now: str, report: dict, dry_run: bool) -> None:
    """Drop pending on_order entries for a SUPERSEDED PO revision.

    Same-revision entries are intentionally kept so that
    _apply_po_on_order can replace them in place, preserving
    ordered_at across re-ingests of the same scan (which would
    otherwise reset the date to "now"). Older revisions still get
    nuked here; reversal audit lives in _reverse_po_entries."""
    new_rev_int = _po_rev_int(new_rev)
    for key, item in inv.items():
        pending = item.get("on_order") or []
        if not pending:
            continue
        # Match same PO; partition by revision.
        same_po = [p for p in pending if p.get("po_number") == po_number]
        if not same_po:
            continue
        removed = [p for p in same_po
                   if _po_rev_int(p.get("po_revision") or "") < new_rev_int]
        if not removed:
            continue
        kept = [p for p in pending
                if p.get("po_number") != po_number
                or _po_rev_int(p.get("po_revision") or "") >= new_rev_int]
        removed_qty = round(sum(float(p.get("qty") or 0) for p in removed), 2)
        report["changes"].append({
            "name": item["name"],
            "warehouse": item.get("warehouse", ""),
            "event_type": "on_order_reversed",
            "old_quantity": item["quantity"],
            "new_quantity": item["quantity"],
            "delta": 0,
            "on_order_delta": -removed_qty,
            "po_number": po_number,
            "superseded_by_revision": str(new_rev or ""),
        })
        if not dry_run:
            item["on_order"] = kept
            item["updated"] = now


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
    po_num = getattr(evt, "po_number", "") or ""

    # PO-tagged restocks don't hit on-hand immediately. They're parked in
    # item["on_order"] with a lead-time ETA and promoted by the rollover
    # in inventory_tracker.load_inventory when ETA passes.
    if evt.event_type == "restock" and po_num:
        _apply_po_on_order(evt, item, key, now, report, dry_run)
        return

    if evt.event_type == "usage_rate":
        # A reported average weekly-usage refresh -- e.g. Cheney's case-
        # movement export converted to cases/week. Updates the variety's
        # weekly_usage reference ONLY: it does not touch cases on hand and
        # writes no movement entry to the usage ledger (usage here is
        # reported, not inferred from a count delta).
        new_wu = evt.item.weekly_usage
        old_wu = item.get("weekly_usage")
        wu_changed = (new_wu is not None
                      and abs(float(new_wu) - float(old_wu or 0)) >= 1e-9)
        if not dry_run:
            item["last_usage_report_at"] = now
        if not wu_changed:
            report["unchanged"] += 1
            return
        report["changes"].append({
            "name": item["name"],
            "warehouse": item.get("warehouse", ""),
            "event_type": "usage_rate",
            "old_quantity": old_qty,
            "new_quantity": old_qty,
            "delta": 0,
            "old_weekly_usage": old_wu,
            "new_weekly_usage": round(float(new_wu), 2),
        })
        report["updated"] += 1
        if dry_run:
            return
        item["weekly_usage"] = round(float(new_wu), 2)
        item["updated"] = now
        item["last_synced"] = now
        item["last_synced_from"] = "Email Inbox"
        return

    if evt.event_type == "on_hand":
        # An inventory worksheet carries both the on-hand count and the rep's
        # average weekly usage. Treat the event as a no-op only when NEITHER
        # changed, so a weekly-usage refresh still lands even if cases on hand
        # happen to match.
        new_wu = evt.item.weekly_usage
        old_wu = item.get("weekly_usage")
        qty_changed = abs(amount - old_qty) >= 1e-9
        wu_changed = (new_wu is not None
                      and abs(float(new_wu) - float(old_wu or 0)) >= 1e-9)
        # Record that a fresh count was received for this warehouse today,
        # even when the numbers match last week's. Drives the per-warehouse
        # freshness indicator on the Inventory page.
        if not dry_run:
            item["last_count_at"] = now
        if not qty_changed and not wu_changed:
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

    change = {
        "name": item["name"],
        "warehouse": item.get("warehouse", ""),
        "event_type": evt.event_type,
        "old_quantity": old_qty,
        "new_quantity": new_qty,
        "delta": round(new_qty - old_qty, 2),
    }
    # Surface a weekly-usage refresh (inventory worksheets carry it) so the
    # report shows it even when cases-on-hand didn't move.
    if evt.event_type == "on_hand" and evt.item.weekly_usage is not None:
        change["old_weekly_usage"] = item.get("weekly_usage")
        change["new_weekly_usage"] = round(float(evt.item.weekly_usage), 2)
    report["changes"].append(change)
    report["updated"] += 1

    if dry_run:
        return

    item["quantity"] = new_qty
    item["updated"] = now
    item["last_synced"] = now
    item["last_synced_from"] = "Email Inbox"
    # On-hand worksheets also refresh the variety's average weekly usage.
    if evt.event_type == "on_hand" and evt.item.weekly_usage is not None:
        item["weekly_usage"] = round(float(evt.item.weekly_usage), 2)
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


def _apply_cw_pos(cw_pos: list,
                  dry_run: bool = False,
                  source: str = "Email Inbox") -> dict:
    """Apply a list of parsed Chefs Warehouse PO records.

    CW POs live in their own file (data/chefs_warehouse_pos.json) and
    never touch inventory.json. This function:
      - Normalizes each record (ordered_at, eta, ingested_at fields)
      - Replaces any prior record with the same po_number (idempotent)
      - Drops records whose PO# is in the canceled-POs list
      - Returns a per-PO change summary

    Returns a dict with the same shape as _apply_events for symmetry,
    but the counters refer to CW PO records rather than EmailEvents.
    """
    from inventory_tracker import load_canceled_pos

    report = {
        "distributor": "Chefs Warehouse",
        "source": source,
        "status": "ok",
        "fetched": len(cw_pos),
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_canceled": 0,
        "skipped_invalid":  0,
        "changes": [],
        "error": None,
    }

    if not cw_pos:
        return report

    try:
        existing = load_chefs_warehouse_pos()
    except Exception as exc:  # noqa: BLE001
        report["status"] = "error"
        report["error"]  = f"load_chefs_warehouse_pos failed: {exc}"
        return report

    by_po = {str(r.get("po_number") or "").strip(): r
             for r in existing if r.get("po_number")}

    try:
        canceled = load_canceled_pos()
    except Exception:
        canceled = {}

    lead_days = _po_lead_days()
    now_iso = datetime.now().isoformat()

    def _parse_cw_date(s: str):
        """Accept MM-DD-YYYY or YYYY-MM-DD."""
        s = (s or "").strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%m-%d-%Y")
        except ValueError:
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                return None

    for record in cw_pos:
        po_num = str(record.get("po_number") or "").strip()
        if not po_num:
            report["skipped_invalid"] += 1
            continue
        if po_num in canceled:
            report["skipped_canceled"] += 1
            continue

        # Derive ordered_at from the PO date. ordered_at falls back to
        # today if the parser didn't pick up an order_date.
        order_dt = _parse_cw_date(record.get("order_date"))
        ordered_at = (order_dt or datetime.now()).isoformat()
        # Auto-ETA rule (2026-05-27): CW POs do NOT get an automatic ETA,
        # neither from the printed delivery date nor from a 30-day fallback.
        # They stay pending until an operator types a ship_date, which sets
        # arrival_date = ship_date + 7 days. The printed delivery_date is
        # still preserved on the record (via {**record}) for reference.
        eta = ""

        existing_rec = by_po.get(po_num)
        # Preserve operator-set fields on re-ingest: ship_date,
        # arrival_date, and the ingested_at of the first booking.
        ship_date    = ""
        arrival_date = ""
        first_ingest = now_iso
        if existing_rec:
            ship_date    = existing_rec.get("ship_date") or ""
            arrival_date = existing_rec.get("arrival_date") or ""
            first_ingest = existing_rec.get("ingested_at") or now_iso

        normalized = {
            **record,
            "po_number":    po_num,
            "distributor":  record.get("distributor") or "Chefs Warehouse",
            "ordered_at":   ordered_at,
            "eta":          eta,
            "ship_date":    ship_date,
            "arrival_date": arrival_date,
            "ingested_at":  first_ingest,
            "last_synced":  now_iso,
            "last_synced_from": source,
        }

        if existing_rec is None:
            report["added"] += 1
            change_kind = "added"
        else:
            same = (
                round(float(existing_rec.get("total_cs") or 0), 2)
                    == round(float(normalized.get("total_cs") or 0), 2)
                and len(existing_rec.get("lines") or [])
                    == len(normalized.get("lines") or [])
            )
            if same:
                report["unchanged"] += 1
                change_kind = "unchanged"
            else:
                report["updated"] += 1
                change_kind = "updated"

        report["changes"].append({
            "po_number":   po_num,
            "warehouse":   normalized.get("warehouse", ""),
            "dc_code":     normalized.get("dc_code", ""),
            "total_cs":    normalized.get("total_cs"),
            "total_usd":   normalized.get("total_usd"),
            "event_type":  f"cw_po_{change_kind}",
            "ordered_at":  ordered_at,
            "eta":         eta,
        })

        by_po[po_num] = normalized

    if not dry_run:
        try:
            save_chefs_warehouse_pos(list(by_po.values()))
        except Exception as exc:  # noqa: BLE001
            report["status"] = "error"
            report["error"]  = f"save_chefs_warehouse_pos failed: {exc}"

    return report


def _apply_events(events: list,
                  messages_seen: int = 0,
                  messages_parsed: int = 0,
                  errors: list | None = None,
                  dry_run: bool = False,
                  source: str = "Email Inbox") -> dict:
    """Apply a list of already-parsed EmailEvent objects to inventory.

    This is the "second half" of scan_email() -- it handles PO revision
    replace / supersede, on_order tracking, and writing to usage+inventory.
    Callers that parse email themselves (e.g. a Cowork routine running the
    M365 fetch outside the web service) can POST events to
    /api/email/ingest-events, which invokes this helper. No mailbox access
    is performed here.
    """
    errors = list(errors or [])
    # Log raw error detail server-side only; never let it reach the HTTP
    # response (CodeQL py/stack-trace-exposure — sanitize at the source so
    # every _apply_events caller's report is exception-text-free).
    if errors:
        import sys as _sys
        print('[apply_events] ' + str(len(errors)) + ' parse error(s): '
              + '; '.join(str(e) for e in errors[:10]), file=_sys.stderr)
    report = {
        "distributor": "Email Inbox",
        "source": source,
        "status": "ok",
        "fetched": len(events),
        "updated": 0,
        "unchanged": 0,
        "unmatched": [],
        "changes": [],
        "error": (str(len(errors)) + " parse error(s) (see server log)") if errors else None,
        "messages_seen": messages_seen,
        "messages_parsed": messages_parsed,
        "by_event_type": {"on_hand": 0, "restock": 0, "usage": 0},
    }

    inv = load_inventory()
    usage = load_usage()
    now = datetime.now().isoformat()

    # Count event types up front so the by-type totals reflect everything
    # the scanner produced, even if a later revision ends up skipped.
    for evt in events:
        report["by_event_type"][evt.event_type] = \
            report["by_event_type"].get(evt.event_type, 0) + 1

    # Split events into PO-tagged groups (subject to revision semantics) and
    # untagged events (applied as before). All events from a single parsed
    # PO share the same po_number and po_revision, so grouping is safe.
    po_groups: dict[str, list] = {}
    non_po_events: list = []
    for evt in events:
        po_num = getattr(evt, "po_number", "")
        if po_num:
            po_groups.setdefault(po_num, []).append(evt)
        else:
            non_po_events.append(evt)

    report["po_revisions_skipped"] = []
    report["po_revisions_superseded"] = []

    for po_num, grp in po_groups.items():
        # Collapse duplicate lines that arrived in the same scan. The
        # scanner pulls from multiple mailboxes (JD@ and info@), and the
        # same PO email is often delivered to both — and the same PO PDF
        # often gets re-attached on a reply or forward (e.g., a "RE:
        # Weekly Bagel Inventory & Usage Report" thread that quoted the
        # original USF confirmation). When two messages parse to the same
        # SKU with the SAME qty, dropping the dupe is obvious. When they
        # parse to the same SKU with DIFFERENT qty (because one email
        # carried a stale or earlier version of the PDF), the previous
        # rule kept BOTH — and the SKU got double-counted (e.g., PO
        # 6454635G shipped 16 cs of Onion but stored as 8 + 16 = 24,
        # inflating the PO total from 448 to 456 cs). Fix: collapse to
        # one event per (variety, warehouse, distributor) and keep the
        # MAX qty across all messages. The max is the right pick because
        # stale forwarded versions of a PO are typically partial (smaller
        # qtys); the largest qty represents the canonical / latest order.
        best: dict[tuple, "EmailEvent"] = {}
        for _evt in grp:
            sku_key = (
                (_evt.item.variety or "").strip().lower(),
                (_evt.item.warehouse or "").strip().lower(),
                (_evt.item.distributor or "").strip().lower(),
            )
            cur = best.get(sku_key)
            if cur is None or float(_evt.item.quantity or 0) > float(
                cur.item.quantity or 0
            ):
                best[sku_key] = _evt
        deduped_grp = list(best.values())
        if len(deduped_grp) < len(grp):
            report.setdefault("dedup_dropped", []).append(
                f"PO {po_num}: collapsed {len(grp) - len(deduped_grp)} "
                f"duplicate line(s) within the scan batch "
                f"(kept max qty per SKU)."
            )
        grp = deduped_grp

        # All events in a group share the same revision; take it from the
        # first one. Fall back to "" if the parser didn't set it.
        new_rev = getattr(grp[0], "po_revision", "") or ""
        new_rev_int = _po_rev_int(new_rev)
        existing_rev_int, active_idx = _highest_applied_rev(usage, po_num)

        if active_idx and new_rev_int <= existing_rev_int:
            # Idempotent skip: we've already booked this PO at the same-or-
            # higher revision. Guard is `active_idx`, not `existing_rev_int`,
            # so POs that don't expose a revision (e.g. Cheney) — which parse
            # to rev_int 0 — still skip correctly on replay.
            report["po_revisions_skipped"].append(
                f"PO {po_num} rev {new_rev or '(none)'}: already applied at "
                f"rev {existing_rev_int} or higher - skipped {len(grp)} event(s)."
            )
            continue

        if existing_rev_int and new_rev_int > existing_rev_int:
            # Higher revision arriving for a PO we've already booked - reverse
            # prior entries before posting the new ones.
            _reverse_po_entries(po_num, new_rev, active_idx, inv, usage, now,
                                report, dry_run)
            report["po_revisions_superseded"].append(
                f"PO {po_num}: rev {existing_rev_int} superseded by rev "
                f"{new_rev_int} ({len(active_idx)} line(s) reversed)."
            )

        # Always clear pending on_order rows tagged with this PO before
        # posting new ones. Covers the case where the prior revision never
        # finished its lead time (so nothing is in usage yet).
        _remove_on_order_by_po(po_num, new_rev, inv, now, report, dry_run)

        for evt in grp:
            _apply_email_event(evt, inv, usage, now, report, dry_run)

    for evt in non_po_events:
        _apply_email_event(evt, inv, usage, now, report, dry_run)

    if not dry_run:
        save_inventory(inv)
        save_usage(usage)
    return report


def scan_email(dry_run: bool = False,
               client: EmailInboxClient | None = None) -> dict:
    """Scan the mailbox and apply extracted events. Returns a report dict.

    Thin wrapper: fetch a ScanResult via EmailInboxClient.scan() and delegate
    the apply pipeline to _apply_events(). External callers that fetch mail
    themselves (e.g. a Cowork routine with delegated Outlook MCP access) can
    call _apply_events directly or POST to /api/email/ingest-events.
    """
    client = client or EmailInboxClient()
    try:
        scan = client.scan()
    except NotConfiguredError as exc:
        return {
            "distributor": "Email Inbox",
            "source": client.source(),
            "status": "not_configured",
            "fetched": 0, "updated": 0, "unchanged": 0,
            "unmatched": [], "changes": [],
            "error": str(exc),
            "messages_seen": 0, "messages_parsed": 0,
            "by_event_type": {"on_hand": 0, "restock": 0, "usage": 0},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "distributor": "Email Inbox",
            "source": client.source(),
            "status": "error",
            "fetched": 0, "updated": 0, "unchanged": 0,
            "unmatched": [], "changes": [],
            "error": str(exc),
            "messages_seen": 0, "messages_parsed": 0,
            "by_event_type": {"on_hand": 0, "restock": 0, "usage": 0},
        }

    report = _apply_events(
        events=list(scan.events),
        messages_seen=scan.messages_seen,
        messages_parsed=scan.messages_parsed,
        errors=list(scan.errors or []),
        dry_run=dry_run,
        source=client.source(),
    )
    # Chefs Warehouse POs ride alongside events but land in their own
    # data file. Surface the summary on the same report so the UI can
    # show "N CW POs ingested" without a second round-trip.
    cw_pos = list(getattr(scan, "cw_pos", None) or [])
    if cw_pos:
        cw_report = _apply_cw_pos(cw_pos, dry_run=dry_run, source=client.source())
        report["chefs_warehouse"] = cw_report
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
            report