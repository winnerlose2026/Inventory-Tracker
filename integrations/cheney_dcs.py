"""Cheney Brothers DC + ship-to-account crosswalks.

Derived 2026-08-04 from Cheney's first real feed drop
(``vendor_feeds/cheney/2026-08-04_sample_drop/``), where the EDI 810s state
both sides explicitly and the order-guide CSVs carry only the short codes:

    810:  N1*SF*OCALA*9*3005        N1*ST*H&H BAGELS MANDARIN*1*60446046
    CSV:  <item>,<brand>,05,0,<pack>,<cost>,<timestamp>

So the CSV's DC column is the **last two digits** of Cheney's 3-digit DC
number, and the CSV filename's trailing number is the ship-to account:

    OrderGuide-20260803-113325_0060446046.csv  ->  DC 05 (3005 Ocala),
                                                   account 60446046 (Mandarin)

Only Riviera Beach, Ocala and Punta Gorda are H&H inventory-tracker
warehouses. Cheney also serves H&H Chapel Hill out of Statesville, NC, which
the tracker does not model -- ``in_scope`` marks that difference so callers
report an out-of-scope DC instead of inventing a warehouse for it.
"""
from __future__ import annotations

# Cheney 3-digit DC number -> (Cheney's own DC name, tracker warehouse label).
# The tracker label is "" for DCs we don't track inventory at.
DC_BY_NUMBER: dict[str, tuple[str, str]] = {
    "3001": ("RIVIERA", "Riviera Beach, FL"),
    "3005": ("OCALA", "Ocala, FL"),
    "3006": ("PUNTA GORDA", "Punta Gorda, FL"),
    "3012": ("STATESVILLE", ""),          # serves H&H Chapel Hill, NC -- untracked
}

# Cheney ship-to account # -> H&H store name, as Cheney spells it on the 810.
ACCOUNT_TO_STORE: dict[str, str] = {
    "60398352": "H&H Jacksonville",
    "60402085": "H&H Bagels Tampa Bay",
    "60414676": "H&H Bagels Miami",
    "60415887": "H&H Bagels Chapel Hill",
    "60446046": "H&H Bagels Mandarin",
    "60458212": "H&H Bagels Altamonte Springs",
    # Seen in the 2026-08-04 order-guide drop but not yet on any sample 810;
    # DC is known from the CSV, the store name is not. Fill in when an 810
    # for these accounts arrives.
    "60372848": "",                        # DC 3001 Riviera
    "60413269": "",                        # DC 3001 Riviera
    "60416092": "",                        # DC 3012 Statesville
}


def normalize_dc_code(code: str) -> str:
    """Cheney DC code -> the 4-digit form used as the key here.

    '05', '5', '003005', ' 3005 ', '3005' -> '3005'. Always returns either 4
    digits or ''.

    The CSV feed's DC column is the last two digits of the 3-digit DC number,
    so a 1-2 digit code is expanded with the '30' prefix every current Cheney
    DC shares. Anything that isn't 1-2 significant digits or a full 4-digit
    code is refused rather than guessed at -- callers should surface an unknown
    DC, not silently attach rows to the wrong warehouse.
    """
    s = "".join(ch for ch in (code or "") if ch.isdigit())
    if not s:
        return ""
    trimmed = s.lstrip("0")
    if not trimmed:            # all zeros
        return ""
    if len(trimmed) <= 2:
        return "30" + trimmed.zfill(2)
    if len(trimmed) == 4:
        return trimmed
    return ""


def dc_name(code: str) -> str:
    """Cheney's own DC name for a DC code, or ''."""
    return DC_BY_NUMBER.get(normalize_dc_code(code), ("", ""))[0]


def warehouse_from_dc_code(code: str) -> str:
    """Tracker warehouse label for a Cheney DC code.

    Returns '' both for unknown codes and for known-but-untracked DCs
    (Statesville). Use ``is_known_dc`` to tell those apart.
    """
    return DC_BY_NUMBER.get(normalize_dc_code(code), ("", ""))[1]


def is_known_dc(code: str) -> bool:
    return normalize_dc_code(code) in DC_BY_NUMBER


def store_from_account(account: str) -> str:
    """H&H store name for a Cheney ship-to account #, or ''."""
    s = (account or "").strip().lstrip("0")
    return ACCOUNT_TO_STORE.get(s, "")


__all__ = [
    "DC_BY_NUMBER", "ACCOUNT_TO_STORE", "normalize_dc_code", "dc_name",
    "warehouse_from_dc_code", "is_known_dc", "store_from_account",
]
