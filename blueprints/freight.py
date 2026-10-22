"""Freight blueprint — Lineage freight invoices: list, ingest, the MS365 scan,
the PO->ship-date index, and lead-time metrics. Extracted from app.py
(refactor — see REFACTOR_PLAN.md). Shared helpers come from core/.
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, make_response, request

from core.cache import _AGG_CACHE, _data_sig
from core.util import rollover_row_live
from core.errors import _log_exc, _safe_err
from core.http import _TRUSTED_OUTBOUND_HOSTS
from core.util import _norm_po_key

freight_bp = Blueprint("freight", __name__)


@freight_bp.route("/api/freight/invoices")
def api_freight_invoices():
    """Return all Lineage freight invoices, sorted by ship_date desc.

    Query params:
        ``dc``        filter to a single destination DC, e.g. "Manassas, VA"
        ``distributor`` filter to "US Foods" | "Cheney Brothers" | "Chefs Warehouse"
        ``since``     ISO date (YYYY-MM-DD); only invoices on or after
        ``until``     ISO date; only invoices on or before
    """
    from inventory_tracker import load_freight_invoices
    records = load_freight_invoices()
    dc = (request.args.get("dc") or "").strip()
    dist = (request.args.get("distributor") or "").strip()
    since = (request.args.get("since") or "").strip()
    until = (request.args.get("until") or "").strip()
    out = []
    for r in records:
        if dc and r.get("dest_dc") != dc:
            continue
        if dist and r.get("distributor") != dist:
            continue
        sd = r.get("ship_date") or ""
        if since and sd and sd < since:
            continue
        if until and sd and sd > until:
            continue
        out.append(r)
    out.sort(key=lambda x: (x.get("ship_date") or "", x.get("invoice_number") or ""),
             reverse=True)
    # Also derive a few summary stats so the dashboard doesn't have to
    # recompute them client-side.
    total_cost = sum(float(r.get("total_due") or 0) for r in out)
    total_pallets = sum(int(r.get("pallets") or 0) for r in out)
    total_cases = sum(int(r.get("cases") or 0) for r in out)
    # Aggregate by line-item category (Basis Item / Fuel Surcharge /
    # Lumper / Detention / ...). Each Lineage invoice breaks the total
    # into one or more line items, and operators want to see the
    # categories rolled up across the visible filter window.
    by_category: dict = {}
    by_dc: dict = {}
    for r in out:
        dc = r.get("dest_dc") or "Unknown"
        bd = by_dc.setdefault(dc, {"count": 0, "cost": 0.0,
                                   "pallets": 0, "cases": 0})
        bd["count"]   += 1
        bd["cost"]    += float(r.get("total_due") or 0)
        bd["pallets"] += int(r.get("pallets") or 0)
        bd["cases"]   += int(r.get("cases") or 0)
        for li in (r.get("line_items") or []):
            desc = (li.get("description") or "Other").strip()
            bc = by_category.setdefault(desc, {"count": 0, "total": 0.0})
            bc["count"] += 1
            bc["total"] += float(li.get("total") or 0)
    # Round for clean transit
    for v in by_category.values():
        v["total"] = round(v["total"], 2)
    for v in by_dc.values():
        v["cost"] = round(v["cost"], 2)
        v["per_pallet"] = round(v["cost"] / v["pallets"], 2) if v["pallets"] else 0
        v["per_case"]   = round(v["cost"] / v["cases"], 4)   if v["cases"]   else 0

    summary = {
        "count":          len(out),
        "total_cost":     round(total_cost, 2),
        "total_pallets":  total_pallets,
        "total_cases":    total_cases,
        "avg_cost_per_pallet": round(total_cost / total_pallets, 2) if total_pallets else 0,
        "avg_cost_per_case":   round(total_cost / total_cases, 4) if total_cases else 0,
        "by_category":    by_category,
        "by_dc":          by_dc,
    }
    return jsonify({"invoices": out, "summary": summary})


@freight_bp.route("/api/freight/ingest", methods=["POST"])
def api_freight_ingest():
    """Accept externally-parsed Lineage freight invoice records.

    Called by the 6h cron mailbox scan after it has fetched
    Lineage emails, unzipped the attachment, and parsed each PDF.

    Request body:
        {
          "dry_run": false,
          "source":  "cowork-routine/lineage",
          "invoices": [ <FreightInvoice as dict>, ... ]
        }

    Dedup is by invoice_number — re-ingesting the same invoice replaces
    the prior record. Records without invoice_number are rejected.
    """
    import traceback as _tb
    from datetime import datetime
    try:
        from inventory_tracker import (
            load_freight_invoices, save_freight_invoices,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False,
                        "error": _safe_err(exc, "import")}), 500
    body = request.json or {}
    dry_run = bool(body.get("dry_run", False))
    source = str(body.get("source") or "external").strip() or "external"
    raw = body.get("invoices") or []
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "invoices must be a list"}), 400

    existing = load_freight_invoices()
    by_inv = {str(r.get("invoice_number") or ""): r for r in existing if r.get("invoice_number")}

    added = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for idx, rec in enumerate(raw):
        if not isinstance(rec, dict):
            errors.append(f"invoices[{idx}]: not an object")
            skipped += 1
            continue
        inv_no = str(rec.get("invoice_number") or "").strip()
        if not inv_no:
            errors.append(f"invoices[{idx}]: missing invoice_number")
            skipped += 1
            continue
        # Re-derive cost_per_pallet / cost_per_case server-side so we
        # never trust client math. Clients may omit them entirely.
        try:
            total = float(rec.get("total_due") or 0)
            pallets = int(rec.get("pallets") or 0)
            cases = int(rec.get("cases") or 0)
        except (TypeError, ValueError):
            total = pallets = cases = 0
        rec["cost_per_pallet"] = round(total / pallets, 2) if pallets else 0
        rec["cost_per_case"]   = round(total / cases, 4) if cases else 0
        # Stamp ingest source + time
        rec.setdefault("source", source)
        rec["ingested_at"] = rec.get("ingested_at") or now

        if inv_no in by_inv:
            by_inv[inv_no] = rec
            updated += 1
        else:
            by_inv[inv_no] = rec
            added += 1

    if not dry_run:
        merged = sorted(by_inv.values(),
                        key=lambda x: (x.get("ship_date") or "",
                                       x.get("invoice_number") or ""))
        try:
            save_freight_invoices(merged)
        except Exception as exc:  # noqa: BLE001
            return jsonify({
                "ok": False,
                "error": _safe_err(exc, "save"),
                "traceback": "",
            }), 500

    ship_dates = {}
    if not dry_run and (added or updated):
        try:
            ship_dates = apply_freight_ship_dates()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"apply ship dates: {_safe_err(exc)}")

    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "report": {
            "source": source,
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "total_after": len(by_inv),
            "errors": errors,
            "ship_dates_stamped": ship_dates.get("stamped", 0),
        },
    })


@freight_bp.route("/api/freight/scan", methods=["POST"])
def api_freight_scan():
    """Deep-sweep the configured MS365 mailboxes for Lineage Freight invoices.

    Same Graph credentials the legacy /api/email/scan path uses (web
    service has MS365_TENANT_ID / MS365_CLIENT_ID / MS365_CLIENT_SECRET
    / MS365_USER) but with a Lineage-only sender filter so we can sweep
    a very wide window without burning budget on every PO PDF in the
    inbox.

    Request body (all optional):
        {
          "dry_run":        false,
          "lookback_days":  365,    # default 730 (~2 years)
          "mailboxes":      "JD@ms.hhbagels.com,info@ms.hhbagels.com",
                                    # default MS365_USER env var
          "max_messages":   500     # default 500, hard cap 5000
        }

    Returns:
        {"ok": true, "report": {"messages_seen": N, "invoices_added": A,
         "invoices_updated": U, "errors": [...]}}
    """
    import io
    import json as _json
    import os as _os
    import re as _re
    import sys as _sys
    import traceback as _tb
    import urllib.error
    import urllib.parse
    import urllib.request
    import zipfile
    from dataclasses import asdict
    from datetime import datetime, timezone, timedelta

    body = request.json or {}
    dry_run = bool(body.get("dry_run", False))
    # Optional explicit ISO date window (YYYY-MM-DD). When set, supersedes
    # lookback_days. Lets the caller batch a multi-year backfill into
    # bite-sized 6-month windows so each request fits within gunicorn's
    # 180s timeout.
    since_date = (body.get("since_date") or "").strip()
    until_date = (body.get("until_date") or "").strip()
    try:
        lookback_days = int(body.get("lookback_days") or 730)
    except (TypeError, ValueError):
        lookback_days = 730
    lookback_days = max(1, min(lookback_days, 3650))
    try:
        max_messages = int(body.get("max_messages") or 500)
    except (TypeError, ValueError):
        max_messages = 500
    max_messages = max(1, min(max_messages, 5000))
    mailboxes_raw = (body.get("mailboxes")
                     or _os.environ.get("MS365_USER", "")).strip()
    mailboxes = [m.strip() for m in mailboxes_raw.split(",") if m.strip()]

    tenant = _os.environ.get("MS365_TENANT_ID")
    client_id = _os.environ.get("MS365_CLIENT_ID")
    client_secret = _os.environ.get("MS365_CLIENT_SECRET")
    if not all([tenant, client_id, client_secret, mailboxes]):
        return jsonify({
            "ok": False,
            "error": "MS365 credentials or mailboxes not configured",
        }), 200

    try:
        from integrations.lineage_freight_parser import parse_freight_pdf
        from inventory_tracker import (
            load_freight_invoices, save_freight_invoices,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False,
                        "error": _safe_err(exc, "import")}), 500

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def _token():
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        return payload["access_token"]

    import time as _time

    def _graph_get(token, path):
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        if (urllib.parse.urlparse(url).hostname or "").lower() not in _TRUSTED_OUTBOUND_HOSTS:
            raise ValueError("refusing outbound request to untrusted host")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        # Graph answers 429 / 5xx under load (a 504 Gateway Timeout failed the
        # 10/8 18:00 cron run). Three tries, honouring Retry-After up to 20 s,
        # keeps a transient blip from costing a whole 6-hour slot while
        # staying well inside gunicorn's 180 s budget.
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return _json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                try:
                    wait = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                _time.sleep(min(max(wait, 3.0 * (attempt + 1)), 20.0))
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                _time.sleep(3.0 * (attempt + 1))

    def _graph_get_bytes(token, path):
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        if (urllib.parse.urlparse(url).hostname or "").lower() not in _TRUSTED_OUTBOUND_HOSTS:
            raise ValueError("refusing outbound request to untrusted host")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    errors: list[str] = []
    seen = 0
    parsed_invoices: list[dict] = []
    diag_atts_seen = 0
    lineage_attachment_samples: list = []
    diag_zips = 0
    diag_pdfs_in_zip = 0
    diag_parse_none = 0
    try:
        token = _token()
        # Two ways to specify the window:
        #   - lookback_days (relative): now() - N days .. now()
        #   - since_date / until_date (absolute): explicit YYYY-MM-DD pair,
        #     useful for batching a multi-year backfill into 6-month chunks
        if since_date or until_date:
            try:
                since = datetime.fromisoformat(since_date) if since_date else (
                    datetime.now(timezone.utc) - timedelta(days=3650))
                since = since.replace(tzinfo=timezone.utc)
            except ValueError:
                return jsonify({"ok": False,
                                "error": f"bad since_date {since_date!r}"}), 200
            try:
                until = (datetime.fromisoformat(until_date)
                         if until_date else datetime.now(timezone.utc))
                until = until.replace(tzinfo=timezone.utc)
            except ValueError:
                return jsonify({"ok": False,
                                "error": f"bad until_date {until_date!r}"}), 200
            since_iso = since.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            until_iso = until.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            flt = (f"receivedDateTime ge {since_iso} "
                   f"and receivedDateTime lt {until_iso}")
        else:
            since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            since_iso = since.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            flt = f"receivedDateTime ge {since_iso}"
        LINEAGE_DOMAINS = ("tms.blujaysolutions.net", "blujaysolutions.net",
                           "tms.e2open.com", "e2open.com",
                           "lineagelogistics.com", "onelineage.com")

        # Track sender domains we see vs match, for diagnostics
        domain_seen: dict = {}
        lineage_subjects: list = []
        for mb in mailboxes:
            user = urllib.parse.quote(mb)
            # ALL_MAIL search ALL folders (not just Inbox) because Lineage
            # invoices may have been auto-filed via Outlook rules or
            # manually moved. Use AllItems via the standard "messages"
            # endpoint (no /mailFolders/Inbox prefix).
            # Filter on receivedDateTime ONLY (+ matching $orderby) and test
            # hasAttachments here: `hasAttachments eq true` in $filter makes
            # Graph scan every folder of the mailbox and it 504s on JD@
            # (10/8). receivedDateTime is indexed -- the same query shape
            # the mail scan and the old cron's Lineage sweep always used.
            list_url = (f"{GRAPH_BASE}/users/{user}/messages"
                        f"?$top=250&$filter={urllib.parse.quote(flt)}"
                        f"&$orderby={urllib.parse.quote('receivedDateTime desc')}"
                        f"&$select=id,subject,from,hasAttachments")
            fetched = 0
            while list_url and fetched < max_messages:
                page = _graph_get(token, list_url)
                msgs = page.get("value", [])
                if not msgs:
                    break
                for m in msgs:
                    seen += 1
                    if fetched >= max_messages:
                        break
                    if not m.get("hasAttachments"):
                        continue
                    # Post-filter by sender domain or subject keyword
                    sender = (((m.get("from") or {}).get("emailAddress") or {})
                              .get("address") or "").lower()
                    subj_check = (m.get("subject") or "").upper()
                    dom = sender.split("@", 1)[-1] if "@" in sender else ""
                    domain_seen[dom] = domain_seen.get(dom, 0) + 1
                    looks_lineage = (
                        any(dom == d or dom.endswith("." + d) for d in LINEAGE_DOMAINS)
                        or ("LINEAGE FREIGHT" in subj_check
                            and "BILLABLE INVOICE" in subj_check)
                    )
                    if not looks_lineage:
                        continue
                    fetched += 1
                    if len(lineage_subjects) < 5:
                        lineage_subjects.append((sender, m.get("subject") or ""))
                    mid = m.get("id") or ""
                    subj = m.get("subject") or ""
                    # List + fetch zip attachments
                    try:
                        atts_resp = _graph_get(token,
                            f"/users/{user}/messages/{urllib.parse.quote(mid)}/attachments"
                            "?$select=id,name,contentType,size")
                    except urllib.error.HTTPError as exc:
                        errors.append(f"{mid[:12]}.. list-att failed")
                        continue
                    for a in (atts_resp.get("value") or []):
                        diag_atts_seen += 1
                        aname_raw = a.get("name") or ""
                        aname = aname_raw.lower()
                        actype = (a.get("contentType") or "").lower()
                        if len(lineage_attachment_samples) < 10:
                            lineage_attachment_samples.append({
                                "name": aname_raw,
                                "ctype": actype,
                                "size": a.get("size"),
                            })
                        if not (aname.endswith(".zip") or aname.endswith(".pdf")
                                or actype in ("application/zip",
                                              "application/x-zip-compressed",
                                              "application/pdf")):
                            if len(errors) < 5:
                                errors.append(f"skip att type={actype!r} name={aname!r}")
                            continue
                        try:
                            ab = _graph_get_bytes(token,
                                f"/users/{user}/messages/{urllib.parse.quote(mid)}"
                                f"/attachments/{urllib.parse.quote(a.get('id') or '')}/$value")
                        except urllib.error.HTTPError as exc:
                            errors.append(f"{mid[:12]}.. fetch-att failed")
                            continue
                        pdfs: list[tuple[str, bytes]] = []
                        if aname.endswith(".zip") or actype in ("application/zip",
                                                                "application/x-zip-compressed"):
                            diag_zips += 1
                            try:
                                zf = zipfile.ZipFile(io.BytesIO(ab))
                                for info in zf.infolist():
                                    if info.filename.lower().endswith(".pdf"):
                                        pdfs.append((info.filename, zf.read(info.filename)))
                                        diag_pdfs_in_zip += 1
                            except zipfile.BadZipFile as exc:
                                errors.append(f"{mid[:12]}.. bad-zip")
                                continue
                        else:
                            pdfs = [(a.get("name") or "invoice.pdf", ab)]
                        for fname, pb in pdfs:
                            try:
                                inv = parse_freight_pdf(
                                    pb, pdf_filename=fname,
                                    source_message_id=mid, source_subject=subj)
                            except Exception as exc:  # noqa: BLE001
                                errors.append(f"{mid[:12]}.. parse[{fname}]")
                                continue
                            if inv is None:
                                diag_parse_none += 1
                                if len(errors) < 10:
                                    # Capture the first chars of extracted
                                    # PDF text so we can see what pypdf is
                                    # actually returning on the server.
                                    try:
                                        from pypdf import PdfReader as _PR
                                        import io as _io2
                                        _r = _PR(_io2.BytesIO(pb))
                                        _t = (_r.pages[0].extract_text() or "")[:160]
                                    except Exception as _e:
                                        _log_exc(_e, "freight pdf text extract")
                                        _t = "<text extract failed>"
                                    errors.append(
                                        f"{mid[:12]}.. parse[{fname}] None "
                                        f"({len(pb)}b) txt={_t!r}")
                                continue
                            parsed_invoices.append(asdict(inv))
                # Next page
                list_url = page.get("@odata.nextLink")
    except urllib.error.HTTPError as exc:
        _log_exc(exc, "freight graph")
        return jsonify({
            "ok": False,
            "error": "internal error (graph)",
            "errors": errors,
        }), 200
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "freight scan")
        return jsonify({
            "ok": False,
            "error": "internal error",
            "errors": errors,
        }), 200

    # Merge into freight_invoices.json
    existing = load_freight_invoices()
    by_inv = {str(r.get("invoice_number") or ""): r
              for r in existing if r.get("invoice_number")}
    added = updated = 0
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for rec in parsed_invoices:
        ino = str(rec.get("invoice_number") or "").strip()
        if not ino:
            continue
        try:
            t = float(rec.get("total_due") or 0)
            pl = int(rec.get("pallets") or 0)
            cs = int(rec.get("cases") or 0)
        except (TypeError, ValueError):
            t = pl = cs = 0
        rec["cost_per_pallet"] = round(t / pl, 2) if pl else 0
        rec["cost_per_case"]   = round(t / cs, 4) if cs else 0
        rec["ingested_at"]     = rec.get("ingested_at") or now
        if ino in by_inv:
            by_inv[ino] = rec; updated += 1
        else:
            by_inv[ino] = rec; added += 1
    if not dry_run:
        merged = sorted(by_inv.values(),
                        key=lambda x: (x.get("ship_date") or "",
                                       x.get("invoice_number") or ""))
        save_freight_invoices(merged)
        try:
            apply_freight_ship_dates()
        except Exception as exc:  # noqa: BLE001
            _log_exc(exc, "freight apply ship dates")

    # Trim domain_seen to top 25 for log readability
    top_domains = dict(sorted(domain_seen.items(), key=lambda x: -x[1])[:25])
    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "report": {
            "mailboxes":        mailboxes,
            "lookback_days":    lookback_days,
            "messages_seen":    seen,
            "invoices_parsed":  len(parsed_invoices),
            "invoices_added":   added,
            "invoices_updated": updated,
            "total_after":      len(by_inv),
            "errors":           errors[:50],
            "error_count":      len(errors),
            "top_sender_domains": top_domains,
            "sample_lineage_matches": lineage_subjects,
            "lineage_attachment_samples": lineage_attachment_samples,
            "diag": {
                "pypdf_version":        (lambda:
                    __import__("pypdf").__version__ if hasattr(__import__("pypdf"), "__version__") else "?")(),
                "attachments_seen":     diag_atts_seen,
                "zip_attachments":      diag_zips,
                "pdfs_extracted_from_zip": diag_pdfs_in_zip,
                "parse_returned_none":  diag_parse_none,
            },
        },
    })


def apply_freight_ship_dates(dry_run: bool = False) -> dict:
    """Stamp freight-verified ship dates onto pending on_order rows.

    A Lineage invoice is the strongest evidence we have that a PO left
    Woodside. Until now it was DISPLAY-ONLY: the ledger showed the ship date,
    the UI then treated ship+7 as arrived, but the server kept the PO pending
    until ordered_at + 30 and on-hand was not credited -- so the Pending tab,
    the ledger status and the Inventory tab disagreed for up to three weeks,
    and the operator's edits went to an endpoint that found nothing to edit.

    Same contract as an operator entering the ship date on the row:
    arrival = ship + 7 (the rollover trigger), unless an arrival is already
    set. Idempotent; rows that already carry a ship_date are left alone.
    """
    from inventory_tracker import load_inventory, save_inventory, data_lock
    from integrations import lead_times as LT
    from integrations.planning_config import transit_days_for
    idx = _freight_ship_date_index()
    stamped: list[dict] = []
    if not idx:
        return {"stamped": 0, "rows": stamped}
    try:
        model = current_lead_time_model()["model"]
    except Exception as exc:  # noqa: BLE001 -- fall back to the defaults
        _log_exc(exc, "lead-time model")
        model = {}
    with data_lock():
        inv = load_inventory()
        changed = False
        for key, item in inv.items():
            for entry in (item.get("on_order") or []):
                if (entry.get("ship_date") or "").strip():
                    continue
                po = (entry.get("po_number") or "").strip()
                sd = idx.get(_norm_po_key(po)) if po else None
                if not sd:
                    continue
                try:
                    ship_dt = datetime.fromisoformat(sd[:10])
                except ValueError:
                    continue
                arrival_iso, tdays, basis = LT.estimate_arrival(
                    sd, item.get("distributor") or "", item.get("warehouse") or "",
                    model, transit_days_for)
                stamped.append({"item_key": key, "po_number": po,
                                "ship_date": ship_dt.isoformat(),
                                "arrival_date": arrival_iso,
                                "transit_days": tdays, "transit_basis": basis})
                if dry_run:
                    continue
                entry["ship_date"] = ship_dt.isoformat()
                entry["ship_date_source"] = "freight"
                if not (entry.get("arrival_date") or "").strip():
                    entry["arrival_date"] = arrival_iso
                    entry["eta"] = arrival_iso
                    entry["arrival_source"] = "freight"
                    entry["transit_days"] = tdays
                    entry["transit_basis"] = basis
                changed = True
        if changed and not dry_run:
            save_inventory(inv)
    out = {"stamped": len(stamped), "rows": stamped}
    try:
        out["rederived"] = rederive_estimated_arrivals(dry_run=dry_run, model=model)
    except Exception as exc:  # noqa: BLE001 -- never fail the stamp over this
        _log_exc(exc, "rederive arrivals")
    return out


def current_lead_time_model() -> dict:
    """Learned transit per location from confirmed deliveries.

    {"model": {location_key: ...}, "samples": [...], "excluded": [...]}.
    PO warehouse / distributor / ship / order dates come from the PO ledger;
    ship dates prefer the Lineage freight invoice.
    """
    from blueprints.pos import build_po_ledger
    from integrations import lead_times as LT
    from integrations.planning_config import transit_days_for
    from integrations.po_delivery import norm_po, wall_time
    from inventory_tracker import load_po_delivery
    tracking = load_po_delivery() or {}
    po_meta = {norm_po(r.get("po_number") or ""): {
                   "distributor": r.get("distributor") or "",
                   "warehouse": r.get("warehouse") or "",
                   "ship_date": r.get("ship_date") or "",
                   "ordered_at": r.get("ordered_at") or ""}
               for r in build_po_ledger()}
    idx = {norm_po(k): v for k, v in _freight_ship_date_index().items()}
    samples, excluded = LT.collect_samples(tracking, po_meta, idx, norm_po, wall_time)
    return {"model": LT.build_model(samples, transit_days_for),
            "samples": samples, "excluded": excluded}


def rederive_estimated_arrivals(dry_run: bool = False, model: dict = None) -> dict:
    """Re-estimate every not-yet-arrived arrival that was derived from a ship
    date (ship + transit), using the current learned transit per location.

    Touches only estimates (lead_times.arrival_is_derived): a Lineage
    appointment, a confirmed delivery or an operator's arrival is never moved.
    Pending inventory rows and Chefs Warehouse POs whose arrival is still in
    the future. Moving an inventory row's arrival moves its rollover (when it
    is credited to on-hand) -- Lineage-tracked POs stay held until confirmed
    regardless.
    """
    from inventory_tracker import (data_lock, load_chefs_warehouse_pos,
                                   load_inventory, save_chefs_warehouse_pos,
                                   save_inventory)
    from integrations import lead_times as LT
    from integrations.planning_config import transit_days_for
    if model is None:
        model = current_lead_time_model()["model"]
    idx = _freight_ship_date_index()
    now = datetime.now()
    changes: list = []

    def _redo(entry, distributor, warehouse, where):
        if not LT.arrival_is_derived(entry):
            return False
        # Same order as the samples: a Lineage-reported pickup, then the
        # freight invoice, then whatever ship date the row carries.
        if (entry.get("ship_date_source") or "") == "lineage" and entry.get("ship_date"):
            ship = entry["ship_date"]
        else:
            ship = (idx.get(_norm_po_key(entry.get("po_number") or "")) or
                    entry.get("ship_date") or "")
        if not ship:
            return False
        cur = (entry.get("arrival_date") or "").strip()
        if cur and cur[:10] < now.date().isoformat() and where == "cw":
            return False          # a CW PO already past its date: history
        arr, days, basis = LT.estimate_arrival(ship, distributor, warehouse,
                                               model, transit_days_for)
        if not arr or (cur[:10] == arr[:10] and entry.get("transit_days") == days):
            return False
        changes.append({"po_number": entry.get("po_number") or "", "where": where,
                        "warehouse": warehouse, "ship_date": ship[:10],
                        "old_arrival": cur[:10], "new_arrival": arr[:10],
                        "transit_days": days, "basis": basis})
        if not dry_run:
            entry["arrival_date"] = arr
            entry["eta"] = arr
            if (entry.get("arrival_source") or "") not in ("freight",):
                entry["arrival_source"] = "ship+transit"
            entry["transit_days"] = days
            entry["transit_basis"] = basis
        return True

    with data_lock():
        inv = load_inventory()
        inv_changed = False
        for item in inv.values():
            for e in (item.get("on_order") or []):
                if _redo(e, item.get("distributor") or "", item.get("warehouse") or "",
                         "inventory"):
                    inv_changed = True
        cw = load_chefs_warehouse_pos()
        cw_changed = False
        for r in cw:
            if r.get("canceled"):
                continue
            if _redo(r, "Chefs Warehouse", r.get("warehouse") or "", "cw"):
                cw_changed = True
        if not dry_run:
            if inv_changed:
                save_inventory(inv)
            if cw_changed:
                save_chefs_warehouse_pos(cw)
    # One line per PO (an inventory PO has a row per SKU).
    seen, per_po = set(), []
    for c in changes:
        k = (c["po_number"], c["where"])
        if k not in seen:
            seen.add(k)
            per_po.append(c)
    return {"dry_run": dry_run, "pos_changed": len(per_po), "rows_changed": len(changes),
            "changes": per_po}


@freight_bp.route("/api/freight/apply-ship-dates", methods=["POST"])
def api_freight_apply_ship_dates():
    """Backfill freight ship dates onto pending POs (idempotent). Runs
    automatically after every freight ingest; this is the manual trigger."""
    body = request.json or {}
    try:
        return jsonify({"ok": True, **apply_freight_ship_dates(
            dry_run=bool(body.get("dry_run", False)))})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc)}), 500


def _freight_ship_date_index() -> dict:
    """Map normalized PO/order/shipper-ref key -> earliest freight ship_date.

    Built from data/freight_invoices.json. When several invoices reference
    the same PO (split shipments), the earliest non-empty ship_date wins —
    that's when the order actually left the origin DC. Freight is auxiliary,
    so any load failure yields an empty index rather than an error.
    """
    try:
        from inventory_tracker import load_freight_invoices
        invoices = load_freight_invoices()
    except Exception:  # noqa: BLE001
        return {}
    idx: dict = {}
    for inv in invoices or []:
        sd = (inv.get("ship_date") or "").strip()
        if not sd:
            continue
        for fld in ("po_number", "order_number", "shipper_ref"):
            key = _norm_po_key(inv.get(fld) or "")
            if not key:
                continue
            prev = idx.get(key)
            if prev is None or sd < prev:
                idx[key] = sd
    return idx


@freight_bp.route("/api/freight/ship-date-index")
def api_freight_ship_date_index():
    """Expose the freight ship-date index (normalized PO key -> ship_date).

    The Pending POs tab uses this to mark POs whose ship date is verified by
    an actual Lineage freight invoice, and to drive Ship/Arrival from it.
    """
    return jsonify({"ok": True, "index": _freight_ship_date_index()})


@freight_bp.route("/api/freight/lead-times")
def api_freight_lead_times():
    """Measured transit (ship -> CONFIRMED delivery) per location, for the
    Freight tab, and the transit the Pending POs tab uses for each location.

    Built from Lineage-confirmed deliveries (data/po_delivery.json) and the
    Lineage invoice ship dates -- see integrations/lead_times.py. Until
    2026-10-22 this endpoint measured ship -> the system's own ship + 7
    estimate, so every location read ~7 days.

    Returns:
      locations  one row per receiving location seen on a PO: n, trend_avg
                 (last TREND_WINDOW), median, min/max, used_days + basis
                 (what the Pending tab adds to a ship date), its samples
      deliveries every confirmed delivery used, newest first
      excluded   confirmed deliveries that couldn't be used, with why
      overall    transit + order->delivery stats across all samples
      (by_warehouse kept for older page builds)
    """
    sig = _data_sig("inventory.json", "usage.json", "freight_invoices.json",
                    "po_delivery.json", "chefs_warehouse_pos.json")
    _c = _AGG_CACHE.get("lead-times")
    if _c and _c[0] == sig:
        return jsonify(_c[1])
    from blueprints.pos import build_po_ledger
    from integrations import lead_times as LT
    from integrations.planning_config import transit_days_for
    try:
        m = current_lead_time_model()
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "lead-times")
        return jsonify({"ok": False, "error": _safe_err(exc)}), 200
    model, samples = m["model"], m["samples"]

    # Every receiving location on a live PO, so a lane with no confirmed
    # delivery yet still shows what the Pending tab assumes for it.
    seen = {}
    for r in build_po_ledger():
        if (r.get("status") or "") in ("canceled", "cancelled"):
            continue
        wh, dist = r.get("warehouse") or "", r.get("distributor") or ""
        if wh and dist in ("US Foods", "Cheney Brothers", "Chefs Warehouse"):
            seen[LT.location_key(dist, wh)] = (dist, wh)
    locations = []
    for key in sorted(set(seen) | set(model)):
        if key in model:
            locations.append(model[key])
            continue
        dist, wh = seen[key]
        days, basis = LT.transit_for(model, dist, wh, transit_days_for)
        locations.append({"distributor": dist, "warehouse": wh,
                          "location": LT.location_label(dist, wh),
                          "transit_n": 0, "trend_avg": None, "used_days": days,
                          "default_days": days, "learned": False, "basis": basis,
                          "samples": []})
    locations.sort(key=lambda x: (not x.get("learned"), -(x.get("transit_n") or 0),
                                  x["location"]))

    def _agg(vals):
        return LT._stats(vals)

    overall = {"transit": _agg([s["transit_days"] for s in samples]),
               "order_to_delivery": _agg([s.get("order_to_delivery_days")
                                          for s in samples])}
    # Legacy keys (older page builds read these).
    overall["ship_to_arrival"] = overall["transit"]
    overall["order_to_arrival"] = overall["order_to_delivery"]
    by_warehouse = [{"warehouse": l["location"],
                     "ship_to_arrival": _agg([x["transit_days"] for x in l["samples"]]),
                     "order_to_arrival": l.get("order_to_delivery") or _agg([])}
                    for l in locations if l.get("samples")]
    result = {"ok": True, "min_samples": LT.MIN_SAMPLES,
              "trend_window": LT.TREND_WINDOW,
              "locations": locations,
              "deliveries": list(reversed(samples)),
              "excluded": m["excluded"],
              "overall": overall, "by_warehouse": by_warehouse,
              "po_count": len(samples)}
    _AGG_CACHE["lead-times"] = (sig, result)
    return jsonify(result)


@freight_bp.route("/api/freight/lead-times/apply", methods=["POST"])
def api_freight_lead_times_apply():
    """Re-estimate pending arrivals from the learned transit. Body {dry_run}
    (default true). Only ship-date-derived estimates move -- never a Lineage
    appointment, a confirmed delivery or an operator's arrival."""
    body = request.get_json(silent=True) or {}
    dry_run = body.get("dry_run", True) is not False
    try:
        return jsonify({"ok": True, **rederive_estimated_arrivals(dry_run=dry_run)})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "lead-times apply")
        return jsonify({"ok": False, "error": _safe_err(exc)}), 200
