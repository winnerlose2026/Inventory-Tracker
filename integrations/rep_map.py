"""Optional data-driven sender->warehouse overrides (roadmap #10).

Onboard a new distributor rep / warehouse WITHOUT a code deploy: add an entry
to data/rep_warehouse_map.json on the service disk and the next scan picks it
up. The hardcoded maps in usfoods_inventory_report.py and
bagel_inventory_worksheet.py stay as the baseline; this only ADDS / overrides.

JSON shape (email -> [distributor, warehouse]):
  { "newrep@usfoods.com": ["US Foods", "Somewhere, ST"] }

Fails safe to {} on any error, so a missing or malformed file can never break
the scan -- it just means no overrides.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_CACHE = {"key": None, "data": {}}


def sender_address(from_header) -> str:
    """The REAL address of a From: header, lowercased; "" if there is none.

    Sender -> warehouse/distributor resolution used to take the first
    ``x@y`` token anywhere in the header, so a display name won:
    ``"maria.hernandez@usfoods.com" <anyone@evil.example>`` resolved to
    Zebulon and its pasted table overwrote Zebulon's on-hand (review
    2026-10-22). getaddresses() returns the addr-spec, and in strict mode
    (3.13+) an ambiguous header parses to ('', '') -- unknown sender, which
    fails closed. Plain addresses (Graph's from.emailAddress.address) pass
    through unchanged.
    """
    from email.utils import getaddresses
    try:
        pairs = getaddresses([str(from_header or "")])
    except Exception:  # noqa: BLE001 -- a garbled header is an unknown sender
        return ""
    for _name, addr in pairs:
        addr = (addr or "").strip().lower()
        if addr.count("@") == 1 and not any(c.isspace() for c in addr) \
                and addr.split("@")[0] and "." in addr.split("@")[1]:
            return addr
    return ""


def _map_path() -> Path:
    return Path(os.environ.get("REP_MAP_FILE", "data/rep_warehouse_map.json"))


def sender_overrides() -> dict:
    """email(lowercased) -> (distributor, warehouse). {} if no/!bad file."""
    path = _map_path()
    try:
        st = path.stat()
    except OSError:
        _CACHE["key"] = None
        _CACHE["data"] = {}
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _CACHE["key"] == key:
        return _CACHE["data"]
    out = {}
    try:
        raw = json.loads(path.read_text())
        for k, v in (raw or {}).items():
            if isinstance(v, (list, tuple)) and len(v) == 2:
                out[str(k).strip().lower()] = (str(v[0]), str(v[1]))
    except Exception:  # noqa: BLE001 — never let a bad file break the scan
        out = {}
    _CACHE["key"] = key
    _CACHE["data"] = out
    return out
