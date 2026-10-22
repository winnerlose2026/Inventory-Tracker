"""One write path for Daily Production records.

Why this module exists
----------------------
Three callers turned a parsed sheet into a stored record -- the daily mailbox
scan, the Graph webhook and POST /api/production/ingest -- and each had its
own copy of "build the record, check the message id, append". They drifted:

* only /ingest had a content dedup, so a sheet re-sent with a fix (Ocala
  054511751617, 9/16) was stored twice and counted 224 cs for a 112-cs PO;
* the webhook and /ingest dropped ``derived_from``, so a later renormalize
  re-split each assorted share on its own (24 cs -> 8/8/4/4 not 6/6/6/6);
* deleting a record didn't stick -- the next scan re-ingested the email;
* the scan's end-of-run merge re-appended every record without a message id,
  doubling it on each run.

Everything here is pure over the ``records`` list the caller passes in; the
caller loads and saves production.json under ``inventory_tracker.data_lock()``
and does its network I/O (Graph) before taking the lock.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone

from integrations.production_pdf_parser import (
    choose_production_date,
    classify_warehouse_with_subject,
    lot_date_iso,
    subject_po,
    warehouse_matches_ledger,
)


def _it():
    import inventory_tracker as it
    return it


def _norm(po: str) -> str:
    from core.util import _norm_po_key
    return _norm_po_key(po or "")


def _has_digit(s: str) -> bool:
    return any(ch.isdigit() for ch in (s or ""))


# ---------------------------------------------------------------------------
# Tombstones -- message ids of deleted records the scan must not re-ingest
# ---------------------------------------------------------------------------

def _tombstone_path():
    return _it().DATA_DIR / "production_tombstones.json"


def load_tombstones() -> dict:
    """{source_message_id: {deleted_at, po_number, warehouse, production_date}}"""
    return _it()._read_json(_tombstone_path(), {})


def save_tombstones(data: dict) -> None:
    _it()._write_json(_tombstone_path(), data)


def seen_message_ids(records: list, tombstones: dict | None = None) -> set:
    """Every message id the store already accounts for: live records, the
    emails they replaced, and deleted (tombstoned) ones."""
    seen = set()
    for r in records or []:
        if r.get("source_message_id"):
            seen.add(r["source_message_id"])
        for m in r.get("replaced") or []:
            if m:
                seen.add(m)
    seen.update((tombstones or {}).keys())
    return seen


def synth_message_id(pdf_bytes: bytes) -> str:
    """Stable id for an /ingest call that sent no message_id: the same PDF
    always gets the same id, so a re-post is a duplicate, never a new row."""
    return "sha256:" + hashlib.sha256(pdf_bytes or b"").hexdigest()[:40]


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------

def record_from_sheet(sheet, *, message_id: str, subject: str = "",
                      sender: str = "", received_at: str = "",
                      now_iso: str | None = None) -> dict:
    """The stored shape of a parsed ProductionSheet (or a parse-error stub)."""
    now_iso = now_iso or datetime.now().isoformat()
    if getattr(sheet, "error", "") and not getattr(sheet, "lines", None):
        return {
            "production_date": "", "warehouse": "", "warehouse_raw": "",
            "distributor": "", "po_number": "", "lines": [],
            "total_cases": 0, "unmapped_varieties": [],
            "source_message_id": message_id, "source_subject": subject,
            "source_sender": sender, "received_at": received_at or now_iso,
            "ingested_at": now_iso, "parse_error": sheet.error,
        }
    return {
        "production_date":    sheet.production_date,
        "warehouse":          sheet.warehouse,
        "warehouse_raw":      sheet.warehouse_raw,
        "distributor":        sheet.distributor,
        "po_number":          sheet.po_number,
        "lines": [
            {"variety": L.variety, "raw_variety": L.raw_variety,
             "cs_count": L.cs_count, "lot_number": L.lot_number,
             "derived_from": getattr(L, "derived_from", "")}
            for L in sheet.lines
        ],
        "total_cases":        sheet.total_cases,
        "unmapped_varieties": sheet.unmapped_varieties,
        "source_message_id":  message_id,
        "source_subject":     subject,
        "source_sender":      sender,
        "received_at":        received_at or now_iso,
        "ingested_at":        now_iso,
        "parse_error":        "",
        # Raw PO sources, so resolve_po (and a later repair) can re-judge.
        "po_body":            getattr(sheet, "po_body", "") or "",
        "po_subject":         getattr(sheet, "po_subject", "") or "",
    }


# ---------------------------------------------------------------------------
# PDF-header PO vs subject PO
# ---------------------------------------------------------------------------

def ledger_po_index(ledger: list | None = None) -> dict:
    """{normalized PO: {po_number, warehouse, distributor, status, ...}} from
    build_po_ledger(). Never raises -- an empty index just means "neither PO
    is known", which keeps the body PO and flags the conflict."""
    if ledger is None:
        try:
            from blueprints.pos import build_po_ledger
            ledger = build_po_ledger()
        except Exception:  # noqa: BLE001
            ledger = []
    idx = {}
    for r in ledger or []:
        k = _norm(r.get("po_number") or "")
        if k:
            idx[k] = r
    return idx


def resolve_po(record: dict, body_po: str, subj_po: str, index: dict) -> None:
    """Set record["po_number"] from the PDF-header PO and the subject PO.

    The header PO used to win silently. Since April it was WRONG in 3 of the
    4 disagreements (a template whose header wasn't updated): the CWC 7/1
    sheet printed CWFL's 1135526, so Chicago's bake was booked to the
    Florida PO. Now, when they differ, the one that exists in the PO ledger
    wins -- and if both exist, the one whose ledger warehouse is this sheet's
    DC. Both-or-neither keeps the header PO and records ``po_conflict``.
    """
    record.pop("po_conflict", None)
    record.pop("po_resolved", None)
    body_po = (body_po or "").strip()
    subj_po = (subj_po or "").strip()
    b, s = _norm(body_po), _norm(subj_po)
    if not b:
        record["po_number"] = subj_po
        return
    if not s or b == s:
        record["po_number"] = body_po
        return
    eb, es = index.get(b), index.get(s)
    wh, dist = record.get("warehouse") or "", record.get("distributor") or ""

    def _wh_ok(e):
        return bool(e) and warehouse_matches_ledger(
            wh, dist, e.get("warehouse") or "", e.get("distributor") or "")

    chosen = None
    if eb and not es:
        chosen = body_po
    elif es and not eb:
        chosen = subj_po
    elif eb and es:
        mb, ms = _wh_ok(eb), _wh_ok(es)
        if ms and not mb:
            chosen = subj_po
        elif mb and not ms:
            chosen = body_po
    if chosen:
        record["po_number"] = chosen
        record["po_resolved"] = {"body": body_po, "subject": subj_po, "chosen": chosen}
    else:
        record["po_number"] = body_po
        record["po_conflict"] = {"body": body_po, "subject": subj_po}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _recv_dt(r: dict) -> datetime:
    s = (r.get("received_at") or "").strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _identity(r: dict):
    wh = r.get("warehouse") or ""
    po = _norm(r.get("po_number") or "")
    d = r.get("production_date") or ""
    if not (wh and po and d and r.get("lines")):
        return None
    return (wh, po, d)


def _overlaps(a: dict, b: dict) -> bool:
    """Do two sheets for the same PO/date describe the same bake -- i.e.
    share a variety or a lot? A second sheet for a different variety set is
    a separate bake and is kept."""
    va = {L.get("variety") for L in a.get("lines") or [] if L.get("variety")}
    vb = {L.get("variety") for L in b.get("lines") or [] if L.get("variety")}
    if va & vb:
        return True
    la = {L.get("lot_number") for L in a.get("lines") or [] if L.get("lot_number")}
    lb = {L.get("lot_number") for L in b.get("lines") or [] if L.get("lot_number")}
    return bool(la & lb)


def ingest_production_record(records: list, record: dict, *, refresh: bool = False,
                             tombstones: dict | None = None):
    """Add one parsed record to ``records`` (mutated in place).

    Returns (status, stored_record) with status one of:
      tombstoned  -- the email's record was deleted; never re-ingested
      duplicate   -- this message id is already stored (or was replaced)
      refreshed   -- refresh=True re-parse of a stored message id
      replaced    -- a newer email for the same (warehouse, PO, date) with
                     overlapping lines replaced the stored record
      superseded  -- an OLDER email for a record already replaced by a newer
                     one; remembered so it isn't fetched again
      parse_error -- stub stored for a sheet that couldn't be read
      ingested    -- appended
    """
    mid = record.get("source_message_id") or ""
    if mid and mid in (tombstones or {}):
        return "tombstoned", None
    for i, r in enumerate(records):
        if mid and r.get("source_message_id") == mid:
            if refresh and record.get("lines"):
                record["received_at"] = r.get("received_at") or record.get("received_at")
                if r.get("replaced"):
                    record["replaced"] = list(r["replaced"])
                records[i] = record
                return "refreshed", record
            return "duplicate", r
        if mid and mid in (r.get("replaced") or []):
            return "duplicate", r
    if not record.get("lines"):
        records.append(record)
        return "parse_error", record
    ident = _identity(record)
    if ident:
        for i, r in enumerate(records):
            if _identity(r) != ident or not _overlaps(r, record):
                continue
            if _recv_dt(record) >= _recv_dt(r):
                prior = [m for m in (r.get("replaced") or []) if m]
                if r.get("source_message_id"):
                    prior.append(r["source_message_id"])
                record["replaced"] = prior
                records[i] = record
                return "replaced", record
            r.setdefault("replaced", [])
            if mid and mid not in r["replaced"]:
                r["replaced"].append(mid)
            return "superseded", r
    records.append(record)
    return "ingested", record


def store_parsed(items: list, *, refresh: bool = False, ledger_index: dict | None = None):
    """Persist a batch of parsed records under data_lock (no network inside).

    ``items`` is a list of (record, body_po, subject_po). Returns a list of
    (status, record) in the same order.
    """
    it = _it()
    if ledger_index is None:
        ledger_index = ledger_po_index()   # outside the lock: reads, no network
    out = []
    with it.data_lock():
        records = it.load_production()
        tombstones = load_tombstones()
        changed = False
        for record, body_po, subj_po in items:
            if record.get("lines"):
                if not (body_po or subj_po):
                    # caller passed no PO sources: use what the record carries
                    body_po = record.get("po_body") or record.get("po_number") or ""
                    subj_po = record.get("po_subject") or ""
                resolve_po(record, body_po, subj_po, ledger_index)
            status, stored = ingest_production_record(
                records, record, refresh=refresh, tombstones=tombstones)
            if status in ("refreshed", "replaced", "superseded", "parse_error", "ingested"):
                changed = True
            out.append((status, stored))
        if changed:
            it.save_production(records)
    return out


# ---------------------------------------------------------------------------
# Canceled POs and per-PO baked totals
# ---------------------------------------------------------------------------

def canceled_po_keys() -> set:
    """Normalized PO numbers that are canceled by any of the ledger's three
    signals: canceled_pos.json, a "canceled" status override, or a canceled
    Chefs Warehouse record."""
    it = _it()
    keys = set()
    try:
        keys.update(_norm(k) for k in (it.load_canceled_pos() or {}))
    except Exception:  # noqa: BLE001
        pass
    try:
        for k, v in (it.load_status_overrides() or {}).items():
            if str(v).strip().lower() in ("canceled", "cancelled"):
                keys.add(_norm(k))
    except Exception:  # noqa: BLE001
        pass
    try:
        for cw in it.load_chefs_warehouse_pos() or []:
            if cw.get("canceled"):
                keys.add(_norm(cw.get("po_number") or ""))
    except Exception:  # noqa: BLE001
        pass
    keys.discard("")
    return keys


def baked_by_po(records: list, canceled: set | None = None) -> dict:
    """{normalized PO: {baked_cs, by_variety, dates, last_date, lots}} from
    production records, skipping parse stubs and canceled POs."""
    canceled = canceled or set()
    out: dict = {}
    for r in records or []:
        if r.get("parse_error") or not r.get("lines"):
            continue
        k = _norm(r.get("po_number") or "")
        if not k or k in canceled:
            continue
        b = out.setdefault(k, {"baked_cs": 0.0, "by_variety": {}, "dates": set(),
                               "lots": []})
        for L in r.get("lines") or []:
            cs = float(L.get("cs_count") or 0)
            v = L.get("variety") or ""
            b["baked_cs"] += cs
            if v:
                b["by_variety"][v] = b["by_variety"].get(v, 0.0) + cs
            lot = (L.get("lot_number") or "").strip()
            if lot and lot not in b["lots"]:
                b["lots"].append(lot)
        if r.get("production_date"):
            b["dates"].add(r["production_date"])
    for b in out.values():
        b["dates"] = sorted(b["dates"])
        b["last_date"] = b["dates"][-1] if b["dates"] else ""
        b["baked_cs"] = round(b["baked_cs"], 2)
    return out


def baked_for_ledger() -> dict:
    """baked_by_po over the live production.json, excluding canceled POs."""
    return baked_by_po(_it().load_production(), canceled_po_keys())


# ---------------------------------------------------------------------------
# Repair of stored records
# ---------------------------------------------------------------------------

def _snapshot(r: dict) -> dict:
    return {k: r.get(k) for k in ("warehouse", "distributor", "po_number",
                                  "production_date", "total_cases")}


def repair_production_records(records: list, ledger_index: dict) -> tuple:
    """Re-judge stored records with today's rules. Pure: returns
    (new_records, changes, removed, summary) and never mutates ``records``.

    Per record (parse-error stubs are left alone):
      * K2  warehouse from the subject's DC when the stored one is blank,
            unmatched, or In-House while the subject names a real DC;
      * C3  PDF-header PO vs subject PO resolved against the ledger (and a
            stored PO with no digit, e.g. "daily", is dropped);
      * C1  production date = most common plausible lot date, else the
            stored date if plausible, else the received date.
    Then C2: records sharing (warehouse, PO, date) with overlapping lines
    collapse to the newest email; the rest are remembered in replaced[].
    """
    work = copy.deepcopy(records or [])
    changes = []
    counts = {"reclassified": 0, "redated": 0, "relinked": 0, "po_conflicts": 0}
    for r in work:
        if r.get("parse_error") or not r.get("lines"):
            continue
        before = _snapshot(r)
        what = []
        # K2 -----------------------------------------------------------------
        old_wh, old_dist = r.get("warehouse") or "", r.get("distributor") or ""
        new_wh, new_dist = classify_warehouse_with_subject(
            r.get("warehouse_raw") or "", r.get("source_subject") or "")
        if (new_dist and new_wh != "In-House Inventory"
                and (not old_dist or old_wh == "In-House Inventory")
                and (new_wh, new_dist) != (old_wh, old_dist)):
            r["warehouse"], r["distributor"] = new_wh, new_dist
            counts["reclassified"] += 1
            what.append("warehouse")
        # C3 -----------------------------------------------------------------
        had_conflict = r.get("po_conflict")
        prior = r.get("po_resolved") or r.get("po_conflict") or {}
        body = prior.get("body") if prior else (r.get("po_body") or r.get("po_number") or "")
        if not _has_digit(body):
            body = ""
        subj = subject_po(r.get("source_subject") or "")
        old_po = r.get("po_number") or ""
        resolve_po(r, body, subj, ledger_index)
        if (r.get("po_number") or "") != old_po:
            counts["relinked"] += 1
            what.append("po_number")
        if r.get("po_conflict"):
            counts["po_conflicts"] += 1
            if r["po_conflict"] != had_conflict:
                what.append("po_conflict")      # newly flagged for review
        # C1 -----------------------------------------------------------------
        old_date = r.get("production_date") or ""
        lot_dates = [lot_date_iso(L.get("lot_number") or "") for L in r.get("lines") or []]
        new_date = choose_production_date(lot_dates, [old_date], r.get("received_at") or "",
                                          default=old_date)
        if new_date and new_date != old_date:
            r["production_date"] = new_date
            counts["redated"] += 1
            what.append("production_date")
        if what:
            changes.append({"source_message_id": r.get("source_message_id") or "",
                            "subject": (r.get("source_subject") or "")[:90],
                            "changed": what, "before": before, "after": _snapshot(r),
                            **({"po_conflict": r["po_conflict"]} if r.get("po_conflict") else {})})
    # C2 ---------------------------------------------------------------------
    kept: list = []
    removed = []
    for r in sorted(work, key=_recv_dt):
        ident = _identity(r)
        hit = None
        if ident:
            for j, k in enumerate(kept):
                if _identity(k) == ident and _overlaps(k, r):
                    hit = j
                    break
        if hit is None:
            kept.append(r)
            continue
        older = kept[hit]
        prior = [m for m in (older.get("replaced") or []) if m]
        if older.get("source_message_id"):
            prior.append(older["source_message_id"])
        r["replaced"] = sorted(set(prior) | set(r.get("replaced") or []))
        kept[hit] = r
        removed.append({"source_message_id": older.get("source_message_id") or "",
                        "kept": r.get("source_message_id") or "",
                        "warehouse": ident[0], "po_number": r.get("po_number"),
                        "production_date": ident[2],
                        "cases_removed": older.get("total_cases") or 0})
    # Preserve the file's original order for the survivors.
    keep_ids = {id(r) for r in kept}
    new_records = [r for r in work if id(r) in keep_ids]
    summary = {
        "records_scanned": len(work),
        "records_changed": len(changes),
        **counts,
        "duplicates_removed": len(removed),
        "cases_removed": sum(float(x["cases_removed"] or 0) for x in removed),
        "records_after": len(new_records),
    }
    return new_records, changes, removed, summary
