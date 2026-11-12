"""Cheney invoice lines (EDI 810) as an INDEPENDENT check on reported usage.

What an 810 is -- and is not
----------------------------
Cheney's daily 810s (integrations/edi_810.py) are the invoices Cheney sends
to each H&H STORE it serves: ``N1*SF*OCALA*9*3005`` (the Cheney DC the order
shipped from) -> ``N1*ST*H&H BAGELS MANDARIN*1*60446046`` (the store). Every
H&H bagel line on one (Cheney items 10153018..49 -> hh_mfg_codes) is cases
that LEFT that Cheney DC -- the same movement the rep's weekly usage figure
is supposed to describe, measured by Cheney's billing system instead of by a
person filling in a worksheet.

So, per Cheney warehouse + variety:

    invoiced cases / week   (sum over the last 4 full Mon-Sun weeks / 4)
        vs.
    item["weekly_usage"]    (what the rep reported)

Limits (read before trusting a divergence):
  * The feed only carries invoices to H&H's own store accounts. If a Cheney
    DC also sells H&H bagels to anyone else, invoiced < real usage. So
    "invoiced well ABOVE reported" is hard evidence the reported usage is
    too low; "invoiced well BELOW reported" may only mean other customers
    (or store accounts the feed does not include yet -- cheney_dcs lists
    three accounts never seen on an 810).
  * Statesville (DC 3012, Chapel Hill) is not a tracked warehouse; its lines
    are stored but never compared.
  * A week with no invoices at all for a warehouse is treated as "no feed",
    not as zero usage: a warehouse needs data in >= 3 of the 4 weeks before
    anything is flagged.
  * Credit memos (returns) are netted out (edi_810 ``sign``).

Storage: data/cheney_810.json  {"lines": {"<invoice>|<line>": {...}},
"files": {...}, "updated_at"} -- H&H bagel lines only, idempotent by
invoice + line number, pruned to 400 days. Never touches inventory.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KEEP_DAYS = 400
WEEKS = 4
DIVERGE_PCT = 0.35
DIVERGE_MIN_CS = 5.0
MIN_WEEKS_WITH_DATA = 3
DIVERGE_VARIETIES_FOR_ISSUE = 3


def _it():
    import inventory_tracker
    return inventory_tracker


def _path():
    return _it().DATA_DIR / "cheney_810.json"


def _load() -> dict:
    rec = _it()._read_json(_path(), {})
    if not isinstance(rec, dict):
        rec = {}
    rec.setdefault("lines", {})
    rec.setdefault("files", {})
    return rec


def load_lines() -> list:
    return list(_load()["lines"].values())


def lines_from_invoices(invoices: list) -> tuple:
    """Parsed 810 invoices -> (bagel line records, notes). Pure."""
    from integrations.cheney_dcs import (normalize_dc_code, store_from_account,
                                         warehouse_from_dc_code)
    from integrations.hh_mfg_codes import (CHENEY_ITEM_NO_TO_MFG,
                                           HH_MFG_CODE_TO_VARIETY)
    out, notes = [], []
    for v in invoices or []:
        inv_no = str(v.get("invoice_number") or "").strip()
        day = str(v.get("invoice_date") or "")[:10]
        if not inv_no or len(day) != 10:
            notes.append(f"invoice {inv_no or '?'}: no invoice number / date -- skipped")
            continue
        sign = -1 if v.get("is_credit") else 1
        dc = normalize_dc_code(str(v.get("ship_from_code") or ""))
        wh = warehouse_from_dc_code(dc)
        for l in v.get("lines") or []:
            item_no = str(l.get("item_no") or "").strip()
            mfg = CHENEY_ITEM_NO_TO_MFG.get(item_no)
            if not mfg:
                if "BAGEL" in str(l.get("description") or "").upper() \
                        and str(l.get("brand") or "").upper().replace(" ", "") in ("H&H", "HH"):
                    notes.append(f"invoice {inv_no}: H&H bagel item {item_no} "
                                 f"({l.get('description')}) is not in "
                                 "CHENEY_ITEM_NO_TO_MFG -- not counted")
                continue
            if l.get("cases") is None:
                notes.append(f"invoice {inv_no} line {l.get('line_no')}: "
                             f"item {item_no} has no case count -- skipped")
                continue
            out.append({
                "key": f"{inv_no}|{l.get('line_no') or item_no}",
                "invoice_number": inv_no, "invoice_date": day,
                "is_credit": bool(v.get("is_credit")),
                "dc_code": dc, "dc_name": str(v.get("ship_from") or ""),
                "warehouse": wh,
                "ship_to_account": str(v.get("ship_to_account") or ""),
                "store": (store_from_account(str(v.get("ship_to_account") or ""))
                          or str(v.get("ship_to") or "")),
                "item_no": item_no, "mfg_code": mfg,
                "variety": HH_MFG_CODE_TO_VARIETY.get(mfg, ""),
                "cases": round(sign * abs(float(l.get("cases") or 0)), 3),
                "po_number": str(v.get("po_number") or ""),
            })
    return out, notes


def ingest_invoices(invoices: list, *, filename: str = "", dry_run: bool = False,
                    today: "date | None" = None) -> dict:
    """Persist the H&H bagel lines of parsed 810 invoices. Idempotent: a
    line already stored (same invoice + line) is replaced, not added."""
    it = _it()
    lines, notes = lines_from_invoices(invoices)
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    with it.data_lock():
        rec = _load()
        added = replaced = 0
        for l in lines:
            if l["invoice_date"] < cutoff:
                continue
            if l["key"] in rec["lines"]:
                replaced += 1
            else:
                added += 1
            rec["lines"][l["key"]] = {k: v for k, v in l.items() if k != "key"}
        before = len(rec["lines"])
        rec["lines"] = {k: v for k, v in rec["lines"].items()
                        if (v.get("invoice_date") or "") >= cutoff}
        pruned = before - len(rec["lines"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if filename:
            rec["files"][filename] = {"ingested_at": now, "invoices": len(invoices or []),
                                      "bagel_lines": len(lines)}
            if len(rec["files"]) > 600:
                for k, _ in sorted(rec["files"].items(),
                                   key=lambda kv: kv[1].get("ingested_at", ""))[:-600]:
                    rec["files"].pop(k, None)
        rec["updated_at"] = now
        if not dry_run and (lines or pruned or filename):
            it._write_json(_path(), rec)
    return {"invoices": len(invoices or []), "bagel_lines": len(lines),
            "added": added, "replaced": replaced, "pruned": pruned,
            "stored_lines": len(rec["lines"]), "notes": notes[:40],
            "untracked_dc_lines": sum(1 for l in lines if not l["warehouse"])}


def _full_weeks(today: date, n: int = WEEKS) -> list:
    """The last ``n`` complete Mon-Sun weeks before ``today``'s week."""
    this_monday = today - timedelta(days=today.weekday())
    return [(this_monday - timedelta(days=7 * (i + 1)),
             this_monday - timedelta(days=7 * i + 1)) for i in range(n)][::-1]


def crosscheck(inv: dict, lines: "list | None" = None,
               today: "date | None" = None) -> dict:
    """Invoiced cases/week vs the rep-reported weekly_usage, per Cheney
    warehouse + variety. Pure given ``inv`` / ``lines`` / ``today``."""
    lines = load_lines() if lines is None else lines
    today = today or datetime.now(timezone.utc).date()
    weeks = _full_weeks(today)
    start, end = weeks[0][0].isoformat(), weeks[-1][1].isoformat()
    per: dict = {}                  # (wh, variety) -> [cases per week]
    wh_weeks: dict = {}             # wh -> set(week index with any line)
    for l in lines:
        wh, var, day = l.get("warehouse") or "", l.get("variety") or "", l.get("invoice_date") or ""
        if not wh or not var or not (start <= day <= end):
            continue
        for i, (a, b) in enumerate(weeks):
            if a.isoformat() <= day <= b.isoformat():
                per.setdefault((wh, var), [0.0] * len(weeks))[i] += float(l.get("cases") or 0)
                wh_weeks.setdefault(wh, set()).add(i)
                break
    reported: dict = {}
    for item in (inv or {}).values():
        if "cheney" not in str(item.get("distributor") or "").lower():
            continue
        wh = item.get("warehouse") or ""
        var = (item.get("name") or "").split(" Bagel")[0].strip()
        if wh and var:
            reported[(wh, var)] = item.get("weekly_usage")
    rows = []
    for (wh, var) in sorted(set(per) | set(reported)):
        by_week = per.get((wh, var), [0.0] * len(weeks))
        n_data = len(wh_weeks.get(wh, ()))
        invoiced = round(sum(by_week) / len(weeks), 2)
        rep = reported.get((wh, var))
        rep_f = float(rep) if rep is not None else None
        enough = n_data >= MIN_WEEKS_WITH_DATA
        diff = (invoiced - rep_f) if rep_f is not None else None
        base = max(invoiced, rep_f or 0.0)
        pct = (abs(diff) / base) if (diff is not None and base > 0) else None
        diverges = bool(enough and diff is not None and pct is not None
                        and pct > DIVERGE_PCT and abs(diff) > DIVERGE_MIN_CS)
        rows.append({
            "warehouse": wh, "variety": var,
            "invoiced_cs_per_week": invoiced,
            "invoiced_by_week": [round(x, 2) for x in by_week],
            "reported_weekly_usage": rep_f,
            "difference_cs_per_week": round(diff, 2) if diff is not None else None,
            "difference_pct": round(pct, 3) if pct is not None else None,
            "direction": ("" if diff is None or abs(diff) < 1e-9 else
                          "invoiced_above_reported" if diff > 0
                          else "invoiced_below_reported"),
            "weeks_with_invoices": n_data,
            "insufficient_data": not enough,
            "diverges": diverges,
        })
    by_wh: dict = {}
    for r in rows:
        w = by_wh.setdefault(r["warehouse"], {
            "warehouse": r["warehouse"], "varieties": 0, "diverging": [],
            "invoiced_cs_per_week": 0.0, "reported_weekly_usage": 0.0,
            "weeks_with_invoices": r["weeks_with_invoices"]})
        w["varieties"] += 1
        w["invoiced_cs_per_week"] = round(w["invoiced_cs_per_week"] + r["invoiced_cs_per_week"], 2)
        w["reported_weekly_usage"] = round(
            w["reported_weekly_usage"] + (r["reported_weekly_usage"] or 0.0), 2)
        if r["diverges"]:
            w["diverging"].append(r["variety"])
    return {
        "weeks": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in weeks],
        "rows": rows, "warehouses": sorted(by_wh.values(), key=lambda w: w["warehouse"]),
        "thresholds": {"pct": DIVERGE_PCT, "min_cs_per_week": DIVERGE_MIN_CS,
                       "min_weeks_with_invoices": MIN_WEEKS_WITH_DATA,
                       "varieties_for_issue": DIVERGE_VARIETIES_FOR_ISSUE},
        "stored_lines": len(lines),
        "limits": ("Invoices cover Cheney's sales to H&H store accounts only: "
                   "invoiced ABOVE reported means the reported usage is too low; "
                   "invoiced BELOW reported can also mean the DC sells to other "
                   "customers or the feed is missing a store account."),
    }


__all__ = ["lines_from_invoices", "ingest_invoices", "load_lines", "crosscheck",
           "KEEP_DAYS"]
