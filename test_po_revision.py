#!/usr/bin/env python3
"""Verify PO revision-replace semantics end-to-end.

Scenario:
  1. rev 1 arrives with 3 lines      -> 3 restock events applied, 3 usage rows
  2. rev 2 arrives with edits         -> rev 1 reversed, rev 2 applied on top
     - line A: qty changes 8 -> 12
     - line B: dropped entirely (not present in rev 2)
     - line C: unchanged
     - line D: added (new line)
  3. rev 2 replayed                   -> idempotent no-op
  4. negative on-hand check           -> warning fires when stock was consumed
                                          between revisions
"""
import importlib.util, sys, pathlib, types

OUTPUTS = pathlib.Path("/sessions/lucid-trusting-clarke/mnt/outputs")

# Stub out the integrations package to avoid pulling the full module tree.
# sync_inventory only needs the names exposed at import time.
fake_integrations = types.ModuleType("integrations")
class _Client:
    name = "stub"
    def _has_live_credentials(self): return False
    def fetch_inventory(self): return []
    def source(self): return "stub"
    def scan(self): raise Exception("not used in test")
class NotConfiguredError(Exception): pass
from dataclasses import dataclass, field
@dataclass
class SyncItem:
    name: str = ""
    variety: str = ""
    warehouse: str = ""
    distributor: str = ""
    quantity: float = 0.0
    unit: str = ""
    price: float = None
    case_cost: float = None
    case_size: int = None
    weekly_usage: float = None
    distributor_sku: str = ""
@dataclass
class EmailEvent:
    event_type: str = "restock"
    item: SyncItem = None
    source_message_id: str = ""
    source_subject: str = ""
    po_number: str = ""
    po_revision: str = ""
fake_integrations.CheneyBrothersClient = _Client
fake_integrations.USFoodsClient = _Client
fake_integrations.DistributorClient = _Client
fake_integrations.EmailInboxClient = _Client
fake_integrations.NotConfiguredError = NotConfiguredError
fake_integrations.SyncItem = SyncItem
sys.modules["integrations"] = fake_integrations

# Stub inventory_tracker - we'll drive inv/usage directly.
fake_tracker = types.ModuleType("inventory_tracker")
fake_tracker.load_inventory = lambda: {}
fake_tracker.save_inventory = lambda _: None
fake_tracker.load_usage = lambda: []
fake_tracker.save_usage = lambda _: None
sys.modules["inventory_tracker"] = fake_tracker

spec = importlib.util.spec_from_file_location("sync_inventory", OUTPUTS/"sync_inventory.py")
si = importlib.util.module_from_spec(spec); spec.loader.exec_module(si)

def make_sku(variety, warehouse, qty):
    key = f"{variety} Bagel 4oz [USF - {warehouse}]".lower().strip()
    return key, {
        "name": f"{variety} Bagel 4oz [USF - {warehouse}]",
        "variety": variety,
        "warehouse": f"{warehouse}, VA",
        "quantity": qty,
        "unit": "dozen",
        "distributor": "US Foods",
    }

def make_evt(variety, warehouse, qty, po_number, po_revision, unit="cases"):
    item = SyncItem(
        name=f"{variety} Bagel 4oz [USF - {warehouse}]",
        variety=variety, warehouse=f"{warehouse}, VA",
        distributor="US Foods", quantity=qty, unit=unit,
    )
    return EmailEvent(
        event_type="restock", item=item,
        source_subject=f"US Foods PO {po_number}",
        po_number=po_number, po_revision=po_revision,
    )

def fresh_state():
    inv = {}
    for variety in ("Plain", "Sesame", "Everything", "Poppy Seed"):
        k, v = make_sku(variety, "Manassas", 0)
        inv[k] = v
    return inv, []

def apply_events(events, inv, usage, report_key="r"):
    report = {"changes": [], "unmatched": [], "updated": 0, "unchanged": 0}
    # Replicate the scan_email grouping / revision logic on a fixed event list
    po_groups, non_po = {}, []
    for e in events:
        (po_groups.setdefault(e.po_number, []) if e.po_number else non_po).append(e) if e.po_number else None
    # Bad one-liner — redo cleanly:
    po_groups, non_po = {}, []
    for e in events:
        if e.po_number:
            po_groups.setdefault(e.po_number, []).append(e)
        else:
            non_po.append(e)
    report["po_revisions_skipped"] = []
    report["po_revisions_superseded"] = []
    from datetime import datetime
    now = datetime.now().isoformat()
    for po_num, grp in po_groups.items():
        new_rev = grp[0].po_revision or ""
        new_rev_int = si._po_rev_int(new_rev)
        existing_rev_int, active_idx = si._highest_applied_rev(usage, po_num)
        if active_idx and new_rev_int <= existing_rev_int:
            report["po_revisions_skipped"].append(
                f"PO {po_num} rev {new_rev}: already applied at {existing_rev_int}."
            )
            continue
        if existing_rev_int and new_rev_int > existing_rev_int:
            si._reverse_po_entries(po_num, new_rev, active_idx, inv, usage, now, report, False)
            report["po_revisions_superseded"].append(
                f"PO {po_num}: rev {existing_rev_int} -> {new_rev_int}"
            )
        for e in grp:
            si._apply_email_event(e, inv, usage, now, report, False)
    for e in non_po:
        si._apply_email_event(e, inv, usage, now, report, False)
    return report

# -----------------------------------------------------------------
# STEP 1: rev 1 arrives
# -----------------------------------------------------------------
inv, usage = fresh_state()
PO = "3885495O"
rev1 = [
    make_evt("Plain",      "Manassas", 8,  PO, "0000001"),
    make_evt("Sesame",     "Manassas", 6,  PO, "0000001"),
    make_evt("Everything", "Manassas", 10, PO, "0000001"),
]
r1 = apply_events(rev1, inv, usage)
print("== STEP 1: rev 1 applied ==")
print(f"  On-hand: Plain={inv[list(inv)[0]]['quantity']}  "
      f"Sesame={inv[list(inv)[1]]['quantity']}  "
      f"Everything={inv[list(inv)[2]]['quantity']}  "
      f"Poppy Seed={inv[list(inv)[3]]['quantity']}")
print(f"  Usage rows: {len(usage)}  "
      f"(amounts: {[u['amount'] for u in usage]})")
assert inv[list(inv)[0]]["quantity"] == 8
assert inv[list(inv)[1]]["quantity"] == 6
assert inv[list(inv)[2]]["quantity"] == 10
assert len(usage) == 3
assert all(u.get("po_number") == PO for u in usage)
assert all(u.get("po_revision") == "0000001" for u in usage)
print("  [OK] rev 1 booked 3 restock entries, on-hand matches, po tagging correct.")

# -----------------------------------------------------------------
# STEP 2: rev 2 arrives
#   - Plain:      qty 8  -> 12   (change)
#   - Sesame:     dropped         (missing from rev 2)
#   - Everything: qty 10 -> 10   (unchanged)
#   - Poppy Seed: NEW added qty 4
# -----------------------------------------------------------------
rev2 = [
    make_evt("Plain",      "Manassas", 12, PO, "0000002"),
    make_evt("Everything", "Manassas", 10, PO, "0000002"),
    make_evt("Poppy Seed", "Manassas", 4,  PO, "0000002"),
]
r2 = apply_events(rev2, inv, usage)
print("\n== STEP 2: rev 2 applied ==")
print(f"  On-hand: Plain={inv[list(inv)[0]]['quantity']}  "
      f"Sesame={inv[list(inv)[1]]['quantity']}  "
      f"Everything={inv[list(inv)[2]]['quantity']}  "
      f"Poppy Seed={inv[list(inv)[3]]['quantity']}")
print(f"  superseded: {r2['po_revisions_superseded']}")
print(f"  Usage rows: {len(usage)}")
# Expected: reversals bring Plain/Sesame/Everything to 0, then rev2 reapplies.
assert inv[list(inv)[0]]["quantity"] == 12,  f"Plain should be 12, got {inv[list(inv)[0]]['quantity']}"
assert inv[list(inv)[1]]["quantity"] == 0,   f"Sesame (dropped) should be 0, got {inv[list(inv)[1]]['quantity']}"
assert inv[list(inv)[2]]["quantity"] == 10,  f"Everything should be 10"
assert inv[list(inv)[3]]["quantity"] == 4,   f"Poppy Seed should be 4"
# rev1 entries should be marked superseded
rev1_entries = [u for u in usage if u.get("po_revision") == "0000001" and not u.get("reversal_of_revision")]
assert all(e.get("superseded_by_revision") == "0000002" for e in rev1_entries)
# Three reversal audit rows should exist
reversals = [u for u in usage if u.get("reversal_of_revision") == "0000001"]
assert len(reversals) == 3, f"Expected 3 reversal rows, got {len(reversals)}"
# Three new restock rows tagged rev 2
rev2_entries = [u for u in usage if u.get("po_revision") == "0000002" and not u.get("reversal_of_revision")]
assert len(rev2_entries) == 3
print("  [OK] rev 1 entries superseded, 3 reversal audit rows, rev 2 booked cleanly.")

# -----------------------------------------------------------------
# STEP 3: idempotent replay of rev 2
# -----------------------------------------------------------------
usage_before = len(usage)
inv_snapshot = {k: v["quantity"] for k, v in inv.items()}
r3 = apply_events(rev2, inv, usage)
print("\n== STEP 3: rev 2 replayed ==")
print(f"  skipped: {r3['po_revisions_skipped']}")
print(f"  Usage rows before={usage_before}, after={len(usage)}")
assert len(usage) == usage_before, "Idempotency broken — usage grew on replay."
for k in inv: assert inv[k]["quantity"] == inv_snapshot[k], f"{k} qty drifted"
assert r3["po_revisions_skipped"], "Expected skip notice"
print("  [OK] replay is a no-op — nothing reversed, nothing re-booked.")

# -----------------------------------------------------------------
# STEP 4: old rev after new rev is also a no-op
# -----------------------------------------------------------------
usage_before = len(usage)
r4 = apply_events(rev1, inv, usage)
print("\n== STEP 4: stale rev 1 after rev 2 ==")
print(f"  skipped: {r4['po_revisions_skipped']}")
assert len(usage) == usage_before, "Old revision should not reopen anything."
print("  [OK] stale rev 1 skipped.")

# -----------------------------------------------------------------
# STEP 5: negative on-hand warning
#   Simulate consumption between revisions: on-hand drifts below what
#   rev1 booked. When rev2 reverses rev1, the reversal should go
#   negative and emit a warning.
# -----------------------------------------------------------------
inv, usage = fresh_state()
PO2 = "9999999A"
rev1 = [make_evt("Plain", "Manassas", 10, PO2, "0000001")]
apply_events(rev1, inv, usage)
# Simulate some cases getting used after the PO posted:
inv[list(inv)[0]]["quantity"] = 3   # shipped to store, burned some stock
# Now rev 2 arrives with a different qty — reversal must push -10 on top of 3 -> -7
rev2 = [make_evt("Plain", "Manassas", 14, PO2, "0000002")]
r5 = apply_events(rev2, inv, usage)
print("\n== STEP 5: negative on-hand warning ==")
print(f"  warnings: {r5.get('warnings', [])}")
print(f"  Plain on-hand after rev2: {inv[list(inv)[0]]['quantity']}")
assert r5.get("warnings"), "Expected a below-zero warning"
# Final state: -10 (reverse) + 14 (rev2) = +4 net applied to 3 -> 7
assert inv[list(inv)[0]]["quantity"] == 7
print("  [OK] warning surfaced; final on-hand = 3 - 10 + 14 = 7.")

# ------------------

# -----------------------------------------------------------------
# STEP 6: Cheney-style no-revision PO is idempotent on replay
#   Cheney doesn't expose a revision marker on PO PDFs, so po_revision
#   parses as "" -> rev_int 0. The skip check must guard on
#   `active_idx` truthiness (not on existing_rev_int), otherwise a
#   replayed Cheney PO would double-book.
# -----------------------------------------------------------------
inv, usage = fresh_state()
PO_CH = "014511694485"
cheney_events = [
    make_evt("Plain",      "Manassas", 24, PO_CH, ""),
    make_evt("Everything", "Manassas", 16, PO_CH, ""),
    make_evt("Poppy Seed", "Manassas", 24, PO_CH, ""),
]
r_first = apply_events(cheney_events, inv, usage)
assert len(usage) == 3, f"first apply should book 3 rows, got {len(usage)}"
qty_snapshot = {k: v["quantity"] for k, v in inv.items()}

r_replay = apply_events(cheney_events, inv, usage)
print("\n== STEP 6: Cheney no-revision replay ==")
print(f"  skipped: {r_replay['po_revisions_skipped']}")
print(f"  Usage rows: {len(usage)} (want 3)")
assert len(usage) == 3, (
    f"No-rev replay must be idempotent - usage should stay at 3, got {len(usage)}"
)
for k, q in qty_snapshot.items():
    assert inv[k]["quantity"] == q, f"{k} qty drifted on replay"
assert r_replay["po_revisions_skipped"], "Expected skip notice for no-rev replay"
print("  [OK] Cheney-style no-revision PO replays as no-op.")

print("\nAll revision-replace invariants hold.")
