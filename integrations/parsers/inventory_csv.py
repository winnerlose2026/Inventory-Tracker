"""Parse a daily on-hand inventory CSV from a distributor.

Output: list of EmailEvent payload dicts ready to POST to
/api/email/ingest-events. Each row of CSV becomes one ``on_hand``
event for a specific (variety, warehouse) SKU at the given
distributor.

Required columns (after alias resolution): variety, warehouse (or
warehouse_code), quantity. Everything else is optional and refines
the SKU (case_size, case_cost, distributor_sku, weekly_usage).
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


def parse_inventory_csv(distributor: str, filename: str,
                        content: bytes) -> tuple[list[dict], list[str]]:
    events: list[dict] = []
    errors: list[str] = []
    for idx, row in enumerate(iter_rows(content), start=1):
        variety_raw = _resolve(row, "variety")
        wh_raw      = _resolve(row, "warehouse")
        wh_code     = _resolve(row, "warehouse_code")
        qty_raw     = _resolve(row, "quantity")

        variety = canonical_variety(variety_raw)
        warehouse = canonical_warehouse(distributor, wh_raw, wh_code)
        qty = opt_float(qty_raw)

        if not variety or not warehouse or qty is None:
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
            item["price"] = cc      # mirror so reports stay consistent
        else:
            pr = opt_float(_resolve(row, "price"))
            if pr is not None:
                item["price"] = pr
        sku = _resolve(row, "distributor_sku")
        if sku:
            item["distributor_sku"] = sku
        wu = opt_float(_resolve(row, "weekly_usage"))
        if wu is not None:
            item["weekly_usage"] = wu

        events.append({
            "event_type": "on_hand",
            "item": item,
            "source_message_id": f"sftp:{filename}#{idx}",
            "source_subject": f"SFTP inventory snapshot: {filename}",
            "po_number": "",
            "po_revision": "",
        })

    return events, errors
