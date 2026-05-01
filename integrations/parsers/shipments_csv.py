"""Parse a weekly shipment history CSV from a distributor.

Each line item becomes a ``restock`` EmailEvent tagged with the
shipment's PO number, so it flows through the existing PO
revision-replace pipeline (idempotent if the same shipment file is
re-ingested). The receiving end will park each line in
``on_order`` with an ETA = order_date + PO_LEAD_DAYS, then auto-roll
into ``quantity`` when the ETA passes.

If we see a ``po_revision`` column we honor it; otherwise we set
``po_revision = ""`` and the server does line-add (not revision
supersede) semantics.

For aggregation: shipment lines are sometimes split across multiple
rows (e.g. partial fills). We treat every row as its own event so the
on_order subtotal accumulates correctly, and the PO number alone keys
revision-replace if a corrected file is sent later.
"""

from ._common import (
    canonical_variety,
    canonical_warehouse,
    iter_rows,
    normalize_to_cases,
    opt_float,
    opt_int,
    _resolve,
)


DISTRIBUTOR_TAG = {"Cheney Brothers": "CB", "US Foods": "USF"}


def _short(warehouse: str) -> str:
    return warehouse.split(",", 1)[0].strip() if warehouse else ""


def _build_name(distributor: str, variety: str, warehouse: str) -> str:
    tag = DISTRIBUTOR_TAG.get(distributor, distributor)
    return f"{variety} Bagel 4oz [{tag} - {_short(warehouse)}]"


def parse_shipments_csv(distributor: str, filename: str,
                        content: bytes) -> tuple[list[dict], list[str]]:
    events: list[dict] = []
    errors: list[str] = []
    for idx, row in enumerate(iter_rows(content), start=1):
        variety_raw = _resolve(row, "variety")
        wh_raw      = _resolve(row, "warehouse")
        wh_code     = _resolve(row, "warehouse_code")
        qty_raw     = _resolve(row, "quantity")
        po_number   = _resolve(row, "po_number")
        po_revision = _resolve(row, "po_revision")

        variety = canonical_variety(variety_raw)
        warehouse = canonical_warehouse(distributor, wh_raw, wh_code)
        qty = opt_float(qty_raw)

        if not variety or not warehouse or qty is None or qty <= 0:
            if not (variety and warehouse and qty is not None):
                errors.append(
                    f"row {idx}: missing required fields "
                    f"(variety={variety_raw!r}, warehouse={wh_raw!r}|{wh_code!r}, "
                    f"quantity={qty_raw!r}) — skipped"
                )
            continue

        cs = opt_int(_resolve(row, "case_size"))
        unit_raw = _resolve(row, "unit") or "cs"
        qty_norm, unit_norm = normalize_to_cases(qty, unit_raw, cs)

        item: dict = {
            "quantity": qty_norm,
            "distributor": distributor,
            "name": _build_name(distributor, variety, warehouse),
            "variety": variety,
            "warehouse": warehouse,
            "unit": unit_norm,
        }
        if cs is not None:
            item["case_size"] = cs
        cc = opt_float(_resolve(row, "case_cost"))
        if cc is not None:
            item["case_cost"] = cc
        sku = _resolve(row, "distributor_sku")
        if sku:
            item["distributor_sku"] = sku

        events.append({
            "event_type": "restock",
            "item": item,
            "source_message_id": f"sftp:{filename}#{idx}",
            "source_subject": f"SFTP shipment history: {filename}",
            "po_number": po_number or f"SFTP-{filename}-{idx}",
            "po_revision": po_revision or "",
        })

    return events, errors
