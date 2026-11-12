"""The one rep registry: warehouse -> distributor -> covering reps.

Every list of "who covers which warehouse" in this project is derived from
``WAREHOUSES`` below:

  * the mailbox scanner's sender -> warehouse maps
    (``usfoods_inventory_report.REPORT_SENDER_TO_WAREHOUSE``,
    ``bagel_inventory_worksheet.REP_EMAIL_TO_WAREHOUSE``),
  * the scanner's sender-domain -> distributor map
    (``email_scanner.DOMAIN_TO_DISTRIBUTOR``),
  * the report-status page (``report_status.WAREHOUSE_REPS`` /
    ``DISTRIBUTOR_OF``),
  * ``GET /api/reps``, which the two weekly chaser tasks read instead of
    carrying their own copy.

Before 2026-11-12 these were four hand-maintained copies. A rep added to the
scanner but not to the status page read MISSING every week (the Manassas
customer-contact alias); a warehouse added to inventory but not to the status
page had no one to chase (Houston, for two months). tests/test_rep_registry.py
fails if any consumer stops matching this file.

Adding a rep or a warehouse: edit ``WAREHOUSES`` only. (A rep can also be
added WITHOUT a deploy through data/rep_warehouse_map.json -- see
integrations/rep_map.py; that only feeds the scanner, and ``/api/reps`` lists
those overrides separately.)

Rep fields
----------
name        display name
email       the address the rep sends from (lowercase)
aliases     other addresses of the same person / mailbox that should resolve
            to the same warehouse (lowercase). None today.
no_chase    True = keep reading their mail, never email them a reminder
parser      how the scanner reads their weekly numbers:
              "usf-report"      body table or .xlsx report  (usfoods_inventory_report)
              "worksheet"       CS OH / USE worksheet       (bagel_inventory_worksheet)
              "cheney-workbook" per-facility workbook; the warehouse comes
                                from the FILENAME, so the sender is not in a
                                sender -> warehouse map
            Houston's reps are "usf-report" senders whose numbers arrive as a
            screenshot (integrations/usf_screenshot); the sender map is the
            same one.

Warehouse fields
----------------
distributor     canonical distributor name (seed_bagels.py)
chase_variant   wording the chaser uses: "standard", or "houston-screenshot"
                (ask for the buyer-screen screenshot + typed weekly usage)
watch_only      True = the warehouse is watched but never chased (every rep
                is no_chase). A consistency check, not a second switch.
"""
from __future__ import annotations

from typing import Optional

US_FOODS = "US Foods"
CHENEY = "Cheney Brothers"
CHEFS_WAREHOUSE = "Chefs Warehouse"

# Sender-domain -> canonical distributor name.
DOMAIN_TO_DISTRIBUTOR: "dict[str, str]" = {
    "cheneybrothers.com": CHENEY,
    "cheney.com": CHENEY,
    "usfoods.com": US_FOODS,
    "usfood.com": US_FOODS,
    "chefswarehouse.com": CHEFS_WAREHOUSE,
}

CHASE_STANDARD = "standard"
CHASE_HOUSTON_SCREENSHOT = "houston-screenshot"


def _rep(name: str, email: str, parser: str = "usf-report", *,
         no_chase: bool = False, aliases: "tuple[str, ...]" = ()) -> dict:
    return {"name": name, "email": email.lower(), "parser": parser,
            "no_chase": bool(no_chase),
            "aliases": [a.lower() for a in aliases]}


_ROSS = ("Michael Ross", "mross@cheneybrothers.com", "cheney-workbook")

# Warehouse labels match seed_bagels.py exactly. Multiple reps can cover one
# warehouse; any one of them sending a genuine report clears it.
WAREHOUSES: "dict[str, dict]" = {
    "Manassas, VA": {
        "distributor": US_FOODS, "chase_variant": CHASE_STANDARD,
        "watch_only": False,
        "reps": [
            _rep("Thomas Paxson", "thomas.paxson@usfoods.com"),
            _rep("Jasmin Gomez", "jasmin.gomez@usfoods.com"),
            # Manassas (DC 5O) street-sales shared mailbox, in case the report
            # comes from the team alias rather than a named coordinator.
            _rep("USF Manassas coordination",
                 "5o-dl-streetsalescoordination@usfoods.com"),
            # The weekly "Product Usage Report" itself now comes from USF's
            # Salesforce customer-contact alias (Stephanie Warfle, Sales
            # Coordination Manager - AMA/MDC), and Thomas Paxson forwards it.
            # Never chased -- it's a no-reply system alias.
            _rep("USF Customer Contact (Sales Coordination)",
                 "usfcustomercontact@usfoods.com", no_chase=True),
        ],
    },
    "Zebulon, NC": {
        "distributor": US_FOODS, "chase_variant": CHASE_STANDARD,
        "watch_only": False,
        "reps": [
            _rep("Maria Hernandez", "maria.hernandez@usfoods.com"),
            _rep("Kathleen Thompson", "kathleen.thompson@usfoods.com"),
        ],
    },
    "La Mirada, CA": {
        "distributor": US_FOODS, "chase_variant": CHASE_STANDARD,
        "watch_only": False,
        "reps": [
            _rep("Sam Travlos", "sam.travlos@usfoods.com"),
            _rep("Ozzy Corut", "ozzy.corut@usfoods.com"),
        ],
    },
    "Chicago, IL": {
        "distributor": US_FOODS, "chase_variant": CHASE_STANDARD,
        "watch_only": False,
        "reps": [
            _rep("Michael Via", "michael.via@usfoods.com", "worksheet"),
        ],
    },
    # Alcoa is watch-only. Knoxville market (DC 6H, 2270) -- delivered as the
    # USF "Assortment Management Tool" .xlsx export. Kim asked on 2026-08-03
    # to stop receiving the automated chasers -- she has her own Monday
    # reminder to send the report -- so we keep reading her mail but never
    # nudge her. Christy Dunn (Volunteer State area buyer) was never a
    # covering rep (Kim owns Alcoa, per her 2026-07-07 note); she stays listed
    # only so a report forwarded by her still clears the warehouse.
    "Alcoa, TN": {
        "distributor": US_FOODS, "chase_variant": CHASE_STANDARD,
        "watch_only": True,
        "reps": [
            _rep("Kim Cobb", "kimberly.cobb@usfoods.com", no_chase=True),
            _rep("Christy Dunn", "christy.dunn@usfoods.com", no_chase=True),
        ],
    },
    # NW Houston, DC B2 (13400 Hollister Rd). Foley is the area buyer who cuts
    # the POs and holds the DC on-hand -- he is the ONLY Houston rep we chase,
    # and he answers with a screenshot of his buyer screen plus typed weekly
    # usage. JD took Youngblood (major account exec) off Houston
    # correspondence on 2026-09-30 ("just correspond with Tom Foley for
    # now"); he stays listed no_chase only so anything he forwards still
    # lands on Houston instead of erroring as unknown.
    "Houston, TX": {
        "distributor": US_FOODS, "chase_variant": CHASE_HOUSTON_SCREENSHOT,
        "watch_only": False,
        "reps": [
            _rep("Tom Foley", "tom.foley@usfoods.com"),
            _rep("Charles Youngblood", "charles.youngblood@usfoods.com",
                 no_chase=True),
        ],
    },
    # Cheney: Michael Ross sends ONE report (per-facility workbooks) covering
    # all three FL facilities, so his address is listed against each.
    "Riviera Beach, FL": {
        "distributor": CHENEY, "chase_variant": CHASE_STANDARD,
        "watch_only": False, "reps": [_rep(*_ROSS)],
    },
    "Ocala, FL": {
        "distributor": CHENEY, "chase_variant": CHASE_STANDARD,
        "watch_only": False, "reps": [_rep(*_ROSS)],
    },
    "Punta Gorda, FL": {
        "distributor": CHENEY, "chase_variant": CHASE_STANDARD,
        "watch_only": False, "reps": [_rep(*_ROSS)],
    },
}

# Addresses that must never receive an automated email, whatever a task's own
# list says. Every no_chase rep is on it by construction (never_email());
# list here only addresses that are NOT reps in the table above.
NEVER_EMAIL_EXTRA: "tuple[str, ...]" = ()


# --------------------------------------------------------------------------- #
# Derived views -- the shapes the consumers had before the registry existed
# --------------------------------------------------------------------------- #

def sender_map(parser: str) -> "dict[str, tuple[str, str]]":
    """email (lowercased) -> (distributor, warehouse) for reps read by
    ``parser`` ("usf-report" / "worksheet"). Aliases are included."""
    out: "dict[str, tuple[str, str]]" = {}
    for wh, rec in WAREHOUSES.items():
        for rep in rec["reps"]:
            if rep["parser"] != parser:
                continue
            for addr in [rep["email"], *rep["aliases"]]:
                out[addr] = (rec["distributor"], wh)
    return out


def warehouse_reps() -> "dict[str, list[dict]]":
    """warehouse -> [{name, email[, no_chase: True]}] -- the report-status
    page's WAREHOUSE_REPS shape (no_chase present only when true)."""
    out: "dict[str, list[dict]]" = {}
    for wh, rec in WAREHOUSES.items():
        reps = []
        for rep in rec["reps"]:
            r = {"name": rep["name"], "email": rep["email"]}
            if rep["no_chase"]:
                r["no_chase"] = True
            reps.append(r)
        out[wh] = reps
    return out


def distributor_of() -> "dict[str, str]":
    return {wh: rec["distributor"] for wh, rec in WAREHOUSES.items()}


def never_email() -> "list[str]":
    """Every address an automated task must not email: all no_chase reps (and
    their aliases) plus NEVER_EMAIL_EXTRA. Sorted, lowercased."""
    out = {a.lower() for a in NEVER_EMAIL_EXTRA}
    for rec in WAREHOUSES.values():
        for rep in rec["reps"]:
            if rep["no_chase"]:
                out.add(rep["email"])
                out.update(rep["aliases"])
    return sorted(out)


def chase_recipients(warehouse: str) -> "list[str]":
    """Addresses the chaser may email for ``warehouse`` (empty = watch-only)."""
    rec = WAREHOUSES.get(warehouse) or {}
    blocked = set(never_email())
    return [r["email"] for r in rec.get("reps", [])
            if not r["no_chase"] and r["email"] not in blocked]


def known_sender_addresses() -> "set[str]":
    """Every rep address (and alias) in the registry, lowercased."""
    out: "set[str]" = set()
    for rec in WAREHOUSES.values():
        for rep in rec["reps"]:
            out.add(rep["email"])
            out.update(rep["aliases"])
    return out


def distributor_for_domain(domain: str) -> Optional[str]:
    d = (domain or "").strip().lower()
    for known, name in DOMAIN_TO_DISTRIBUTOR.items():
        if d == known or d.endswith("." + known):
            return name
    return None


def is_known_sender(address: str) -> bool:
    """A registry rep / alias, a data-file override (rep_map), or any address
    on a distributor domain."""
    addr = (address or "").strip().lower()
    if "@" not in addr:
        return False
    if addr in known_sender_addresses():
        return True
    try:
        from integrations.rep_map import sender_overrides
        if addr in sender_overrides():
            return True
    except Exception:  # noqa: BLE001 -- overrides are optional
        pass
    return distributor_for_domain(addr.rsplit("@", 1)[1]) is not None


def as_dict() -> dict:
    """The registry as plain JSON (GET /api/reps)."""
    warehouses = []
    for wh, rec in WAREHOUSES.items():
        warehouses.append({
            "warehouse": wh,
            "distributor": rec["distributor"],
            "watch_only": bool(rec["watch_only"]),
            "chase_variant": rec["chase_variant"],
            "reps": [dict(r, aliases=list(r["aliases"])) for r in rec["reps"]],
            "chase_to": chase_recipients(wh),
        })
    return {
        "warehouses": warehouses,
        "never_email": never_email(),
        "domains": dict(DOMAIN_TO_DISTRIBUTOR),
        "chase_variants": [CHASE_STANDARD, CHASE_HOUSTON_SCREENSHOT],
    }


__all__ = [
    "WAREHOUSES", "DOMAIN_TO_DISTRIBUTOR", "NEVER_EMAIL_EXTRA",
    "CHASE_STANDARD", "CHASE_HOUSTON_SCREENSHOT",
    "sender_map", "warehouse_reps", "distributor_of", "never_email",
    "chase_recipients", "known_sender_addresses", "distributor_for_domain",
    "is_known_sender", "as_dict",
]
