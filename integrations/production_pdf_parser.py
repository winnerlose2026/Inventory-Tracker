"""Parser for H&H Bagels "Daily Production Sheet" PDFs.

These PDFs are sent internally (typically from gabo@hhbagels.com to
isaiah@hhbagels.com) with a subject like

    Daily Production RIVIERA BEACH.PO.014511697480

The attachment is one page of mostly tabular text rendered from a
template. Layout, in order of appearance (extracted via pypdf):

    Production Date
    CS Count SKU
    <qty> CS <variety>            (one row per line item)
    ...
    <total> Total Cases<WAREHOUSE>.PO.<po_number>
    Lot#
    <lot_1>
    <lot_2>
    ...<production_date as MM/DD/YYYY>     (date is concatenated to one of the lots)
    <lot_N>

The header `<WAREHOUSE>.PO.<po_number>` is concatenated to the total-
cases line. The production date is concatenated to whichever lot row
landed at the right spot on the page. Both are recovered with regex
so layout drift doesn't break ingestion.

Special cases handled:

  * CWNY layout omits the "PARB-" prefix on variety names.
  * Subject format on US Foods Manassas was observed as
    "Daily production US FOODS MANASSAS PO.4363705O" — lowercase 'p'
    and an extra 'US FOODS' prefix on the warehouse. The parser pulls
    the warehouse + PO from the PDF body, not the subject, so the
    subject is informational only.
  * Some emails attach an OCR'd scan (image-only PDF). pypdf returns
    no text in that case; ``parse_production_pdf`` returns a partial
    result with ``error`` populated and an empty ``lines`` list so the
    operator can flag the message and re-request a text PDF.

Dependencies: ``pypdf>=4.0`` (already in requirements.txt for the USF
and Cheney PO parsers).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Optional


# Variety normalization. The PDFs use the production team's internal
# variety codes (PARB-... or bare names on CWNY). Map to our canonical
# SKU variety so downstream cross-referencing with on_order works.
_VARIETY_ALIASES: dict[str, str] = {
    "PARB-PLAIN":                "Plain",
    "PARB-EVERYTHING":           "Everything",
    "PARB-SESAME":               "Sesame",
    "PARB-POPPY":                "Poppy Seed",
    "PARB-POPPY SEED":           "Poppy Seed",
    "PARB-ONION":                "Onion",
    "PARB-WHOLE WHEAT":          "Whole Wheat",
    "PARB-WHOLE WHEAT EVERYTHING": "Whole Wheat Everything",
    "PARB-WHEAT EVERYTHING":     "Whole Wheat Everything",
    "PARB-EGG":                  "Egg",
    "PARB-BLUEBERRY":            "Blueberry",
    "PARB-ASIAGO":               "Asiago",
    "PARB-JALAPENO":             "Jalapeno Cheddar",
    "PARB-JALAPENO CHEDDAR":     "Jalapeno Cheddar",
    "PARB-CINN-RAISIN":          "Cinnamon Raisin",
    "PARB-CINNAMON RAISIN":      "Cinnamon Raisin",
    # CWNY bare names (no PARB- prefix)
    "PLAIN":                     "Plain",
    "EVERYTHING":                "Everything",
    "SESAME":                    "Sesame",
    "POPPY":                     "Poppy Seed",
    "POPPY SEED":                "Poppy Seed",
    "ONION":                     "Onion",
    "WHOLE WHEAT":               "Whole Wheat",
    "WHOLEWHEAT":                "Whole Wheat",
    "EGG":                       "Egg",
    "BLUEBERRY":                 "Blueberry",
    "ASIAGO":                    "Asiago",
    "JALAPENO":                  "Jalapeno Cheddar",
    "JALAPENO CHEDDAR":          "Jalapeno Cheddar",
    "CINN-RAISIN":               "Cinnamon Raisin",
    "CINNAMON RAISIN":           "Cinnamon Raisin",
    # Short-hand abbreviations seen on hand-keyed production sheets.
    "WW":                        "Whole Wheat",
    "WWET":                      "Whole Wheat Everything",
    "WW EVERYTHING":             "Whole Wheat Everything",
    "WHOLE WHEAT EVERYTHING":    "Whole Wheat Everything",
    "WHOLEWHEAT EVERYTHING":     "Whole Wheat Everything",
    # PARB- prefix variants of the abbreviations.
    "PARB-WW":                   "Whole Wheat",
    "PARB-WWET":                 "Whole Wheat Everything",
    "PARB-WW EVERYTHING":        "Whole Wheat Everything",
    "PARB-WHOLEWHEAT":           "Whole Wheat",
    "PARB-WHOLEWHEAT EVERYTHING":"Whole Wheat Everything",
    "PARB-WW-ET":                "Whole Wheat Everything",
    "PARB-WW ET":                "Whole Wheat Everything",
    "WW-ET":                     "Whole Wheat Everything",
    "WW ET":                     "Whole Wheat Everything",
    # Common typos
    "EVERYTTHING":               "Everything",
    "EVERYTING":                 "Everything",
    "EVERYTHIING":               "Everything",
    "BLUBERRY":                  "Blueberry",
    # Other compact tags spotted in the wild.
    "PARB-PLN":                  "Plain",
    "PARB-EVT":                  "Everything",
    "PARB-EVTHG":                "Everything",
    "PARB-SES":                  "Sesame",
    "PARB-PPY":                  "Poppy Seed",
    "PARB-ON":                   "Onion",
    "PARB-CINN":                 "Cinnamon Raisin",
    "PARB-BB":                   "Blueberry",
    "PARB-JC":                   "Jalapeno Cheddar",
    "PARB-JLP":                  "Jalapeno Cheddar",
    "PARB-AS":                   "Asiago",
    # Pumpernickel — H&H mfg 1154, the October LTO. Every DC cut a promo PO
    # for it; the production sheets were bucketing it into the catch-all.
    "PUMPERNICKEL":              "Pumpernickel",
    "PARB-PUMPERNICKEL":         "Pumpernickel",
    "PRNKL":                     "Pumpernickel",
    "PUMP":                      "Pumpernickel",
    # Cinnamon Raisin — 1155 is the only cinnamon SKU H&H makes, so a bare
    # "CINNAMON" on a sheet can only mean Cinnamon Raisin.
    "CINN RAISIN":               "Cinnamon Raisin",
    "CINNAMON":                  "Cinnamon Raisin",
    "CINN":                      "Cinnamon Raisin",
    "CIN RAISIN":                "Cinnamon Raisin",
    "RAISIN":                    "Cinnamon Raisin",
    # Punctuation variants of the two-word names.
    "JALAPENO-CHEDDAR":          "Jalapeno Cheddar",
    "JALAPENO CHEDDER":          "Jalapeno Cheddar",
    "POPPYSEED":                 "Poppy Seed",
    "WHOLE-WHEAT":               "Whole Wheat",
}

#: Assorted cases hold a mix the sheet never itemises. JD's call (2026-08-28):
#: an H&H assorted case is an even split of these four.
ASSORTED_SPLIT: tuple[str, ...] = (
    "Plain", "Sesame", "Everything", "Cinnamon Raisin",
)

#: Labels that mean "assorted case" and get the split above.
_ASSORTED_LABELS = frozenset({
    "ASSORTED", "ASST", "ASSORTED SLICED", "ASST SLICED", "SLICED ASSORTED",
    "ASSORTMENT", "MIXED", "VARIETY",
})

#: Mini assorted stays its OWN bucket -- a mini case is not a full-size case,
#: so folding it into the four full-size varieties would overstate them.
_MINI_ASSORTED_LABELS = frozenset({
    "MINI ASST", "MINI ASSORTED", "ASST MINI", "MINI",
})

#: Footer / rollup rows that are not line items. "TOTAL" alone accounted for
#: 1,112 phantom cases before this existed.
_NON_ITEM_LABELS = frozenset({
    "TOTAL", "TOTALS", "GRAND TOTAL", "SUBTOTAL", "SUB TOTAL", "SUM",
    "TOTAL CASES", "TOTAL CS", "ALL", "OTHER",
})

# Map of compact abbreviations applied AFTER stripping a "PARB-" prefix.
# Lets us treat the bare form and the parbaked form the same way without
# bloating _VARIETY_ALIASES with every cross-product.
_VARIETY_SHORTHAND: dict[str, str] = {
    "WW":     "Whole Wheat",
    "WWET":   "Whole Wheat Everything",
    "WW-ET":  "Whole Wheat Everything",
    "WW ET":  "Whole Wheat Everything",
    "PLN":    "Plain",
    "EVT":    "Everything",
    "EVTHG":  "Everything",
    "SES":    "Sesame",
    "PPY":    "Poppy Seed",
    "ON":     "Onion",
    "CINN":   "Cinnamon Raisin",
    "BB":     "Blueberry",
    "JC":     "Jalapeno Cheddar",
    "JLP":    "Jalapeno Cheddar",
    "AS":     "Asiago",
}


#: A lot code sometimes lands in the same text run as the variety, e.g.
#: "PARB-EVERYTHING 1158040226" (mfg 1158 + MMDDYY). Strip it before matching.
_TRAILING_LOT_RE = re.compile(r"\s+\d{6,14}$")

#: The par-baked prefix, however it was typed. These sheets are hand-keyed and
#: the prefix alone has arrived as PARB-, PAARB-, PRB-, PAR-, PARABEKED and
#: PARBAKED. Matching the family beats adding an alias per misspelling.
_PARBAKE_PREFIX_RE = re.compile(r"^(?:PA{0,2}RA?B[A-Z]*|PRB[A-Z]*|PAR)[-.\s]+")

#: Words that describe the product but not the variety.
_STOPWORDS = frozenset({"CHEESE", "BAGEL", "BAGELS", "BGL", "BGLS",
                        "CASE", "CASES", "CS", "EA"})

#: Mini bagels are a different product size, so they never merge into the
#: full-size variety totals -- they get their own "Mini <X>" bucket.
_MINI_TOKENS = frozenset({"MINI", "MINIS"})

#: "SLICED" is a form, not a variety -- H&H's 13 mfg codes have no sliced SKU,
#: so "PLAIN SLICED" and "SLICED PLAIN" are both Plain. (JD, 2026-08-28.)
_SLICED_RE = re.compile(r"\b(SLICED|SLCD|SLC|SL)\b")


def is_non_item_label(raw: str) -> bool:
    """Is this a footer / rollup row rather than a production line?

    The sheet's "TOTAL" row parses as "<n> CS TOTAL" exactly like a real line,
    so it was being stored as a line item and double-counting the whole sheet
    -- 1,112 phantom cases across three sheets before this check existed.
    """
    key = _TRAILING_LOT_RE.sub("", (raw or "").strip().upper()).strip()
    return key in _NON_ITEM_LABELS


def assorted_kind(raw: str) -> str:
    """'' | 'assorted' | 'mini' for a raw variety label."""
    key = _TRAILING_LOT_RE.sub("", (raw or "").strip().upper()).strip()
    key = _PARBAKE_PREFIX_RE.sub("", key).strip()
    toks = _tokens(key)
    if toks & _MINI_TOKENS:
        rest = toks - _MINI_TOKENS
        if not rest or rest & {"ASST", "ASSORTED", "ASSORTMENT", "MIXED"}:
            return "mini"
        return ""          # "MINI EVERYTHING" is a mini of a known variety
    if key in _ASSORTED_LABELS or toks in ({"ASST"}, {"ASSORTED"}):
        return "assorted"
    return ""


def _fuzzy_variety(key: str) -> str:
    """Last-resort close match against the known labels.

    The sheets are hand-keyed and Everything alone arrived as EVRYTHING,
    EVEYRTHING, EVRERYTHING and EVERTYTHING. The cutoff is deliberately high
    and only single-token labels are considered, so ASSORTED can never drift
    into ASIAGO.
    """
    import difflib
    if len(key) < 5 or " " in key:
        return ""
    hits = difflib.get_close_matches(key, _FUZZY_CANDIDATES, n=1, cutoff=0.86)
    return _VARIETY_ALIASES[hits[0]] if hits else ""


#: Single-token alias keys eligible for fuzzy matching (see _fuzzy_variety).
_FUZZY_CANDIDATES = [k for k in _VARIETY_ALIASES
                     if " " not in k and "-" not in k and len(k) >= 5]


def _tokens(key: str) -> frozenset:
    """Significant word tokens of a label, minus filler."""
    parts = re.split(r"[\s\-_.]+", key)
    return frozenset(t for t in parts if t and t not in _STOPWORDS)


#: frozenset-of-tokens -> canonical, so "CHEDDAR JALAPENO" resolves the same
#: as "JALAPENO CHEDDAR" and "ASIAGO CHEESE" the same as "ASIAGO", without an
#: alias entry for every permutation.
_TOKEN_INDEX: dict = {}
for _k, _v in _VARIETY_ALIASES.items():
    _TOKEN_INDEX.setdefault(_tokens(_PARBAKE_PREFIX_RE.sub("", _k)), _v)
for _k, _v in _VARIETY_SHORTHAND.items():
    _TOKEN_INDEX.setdefault(_tokens(_k), _v)


def split_assorted(cs_count: int) -> list:
    """Split an assorted case count evenly across ASSORTED_SPLIT.

    A sheet says "24 CS ASSORTED" and never itemises the mix, so the split is
    H&H's known case makeup (JD, 2026-08-28) rather than anything the sheet
    reported. Integer cases with the remainder handed to the earliest
    varieties, so the parts always sum EXACTLY to the original -- a rounded
    split that loses a case would quietly corrupt the sheet total.

    Returns [(variety, cs), ...].
    """
    n = len(ASSORTED_SPLIT)
    total = int(cs_count or 0)
    base, rem = divmod(abs(total), n)
    sign = -1 if total < 0 else 1
    out = []
    for i, v in enumerate(ASSORTED_SPLIT):
        out.append((v, sign * (base + (1 if i < rem else 0))))
    return out


def _normalize_variety(raw: str) -> tuple[str, bool]:
    """Resolve a raw variety string from a production sheet to a
    canonical variety name.

    Returns ``(canonical, recognized)``. If the raw value can't be
    mapped to anything we know about, the canonical is set to
    ``"In-House Inventory"`` so the ranking aggregates the noise
    into a single bucket instead of fragmenting it into one-off
    entries per typo.

    Resolution order:
      1. Exact match against ``_VARIETY_ALIASES`` (upper-cased).
      2. Strip a leading ``PARB``/``PARB-``/``PARB ``/``PRBKD`` prefix
         and re-try ``_VARIETY_ALIASES``.
      3. Look up the stripped form in ``_VARIETY_SHORTHAND``.
      4. Fall back to ``"In-House Inventory"``.
    """
    if not raw:
        return ("In-House Inventory", False)
    key = raw.strip().upper()
    # A lot code glued onto the variety ("PARB-EVERYTHING 1158040226") is not
    # part of the name.
    key = _TRAILING_LOT_RE.sub("", key).strip()
    if key in _VARIETY_ALIASES:
        return (_VARIETY_ALIASES[key], True)

    kind = assorted_kind(key)
    if kind == "mini":
        return ("Mini Assorted", True)
    if kind == "assorted":
        # Expanded into ASSORTED_SPLIT by expand_assorted_lines(); this label
        # is what a line carries until that runs.
        return ("Assorted", True)

    # Strip the par-baked prefix in whatever spelling it arrived in.
    stripped = _PARBAKE_PREFIX_RE.sub("", key).strip()
    if stripped != key and stripped in _VARIETY_ALIASES:
        return (_VARIETY_ALIASES[stripped], True)
    if stripped in _VARIETY_SHORTHAND:
        return (_VARIETY_SHORTHAND[stripped], True)

    # A mini of a known variety keeps its own bucket, so mini cases never
    # inflate the full-size counts.
    mini_toks = _tokens(stripped)
    if mini_toks & _MINI_TOKENS:
        base, base_ok = _normalize_variety(
            " ".join(sorted(mini_toks - _MINI_TOKENS)))
        if base_ok and base != "Assorted":
            return (f"Mini {base}", True)
        return ("Mini Assorted", True)

    # "SLICED" is a form, not a variety -- drop the token and re-match.
    if _SLICED_RE.search(stripped):
        unsliced = _SLICED_RE.sub("", stripped).replace("  ", " ").strip(" -")
        if unsliced in _VARIETY_ALIASES:
            return (_VARIETY_ALIASES[unsliced], True)
        if unsliced in _VARIETY_SHORTHAND:
            return (_VARIETY_SHORTHAND[unsliced], True)
        if assorted_kind(unsliced) == "assorted":
            return ("Assorted", True)

    # Word order and filler words shouldn't matter: "CHEDDAR JALAPENO" and
    # "ASIAGO CHEESE" are the same products as their canonical spellings.
    hit = _TOKEN_INDEX.get(_tokens(stripped))
    if hit:
        return (hit, True)

    fuzzy = _fuzzy_variety(stripped)
    if fuzzy:
        return (fuzzy, True)
    # Single stray token that fuzzy-matches (e.g. "PAARB-EVERYTHING" once the
    # prefix family has been stripped down to a lone misspelling).
    toks = [t for t in _tokens(stripped) if len(t) >= 5]
    if len(toks) == 1:
        fuzzy = _fuzzy_variety(toks[0])
        if fuzzy:
            return (fuzzy, True)
    return ("In-House Inventory", False)

# Warehouse normalization. The production sheet uses an UPPERCASE
# city-only label; map to the "<City>, <ST>" form we already use on
# on_order entries so cross-referencing works. CWNY is a separate
# distributor we hadn't tracked before this — Chefs Warehouse NY.
_WAREHOUSE_TO_CANONICAL: dict[str, tuple[str, str]] = {
    # warehouse_label_in_pdf -> (canonical_warehouse, distributor)
    "RIVIERA BEACH":         ("Riviera Beach, FL",      "Cheney Brothers"),
    "OCALA":                 ("Ocala, FL",              "Cheney Brothers"),
    "PUNTA GORDA":           ("Punta Gorda, FL",        "Cheney Brothers"),
    "MANASSAS":              ("Manassas, VA",           "US Foods"),
    "US FOODS MANASSAS":     ("Manassas, VA",           "US Foods"),
    "ZEBULON":               ("Zebulon, NC",            "US Foods"),
    "LA MIRADA":             ("La Mirada, CA",          "US Foods"),
    "CHICAGO":               ("Chicago, IL",            "US Foods"),
    "ALCOA":                 ("Alcoa, TN",              "US Foods"),
    "HOUSTON":               ("Houston, TX",            "US Foods"),
    "CWNY":                  ("Chefs Warehouse, NY",    "Chefs Warehouse"),
    "CWFL":                  ("Chefs Warehouse, FL",    "Chefs Warehouse"),
    "CWF":                   ("Chefs Warehouse, FL",    "Chefs Warehouse"),
    "CWC":                   ("Chefs Warehouse, Chicago",      "Chefs Warehouse"),
    "CWMID":                 ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    # Chefs Warehouse PDF header sometimes prints hyphenated codes.
    # Subject format is "CW-MID-ATLANTIC", but pypdf occasionally extracts
    # a truncated "CW-MID-ATL" or "CW-MID" from the header — alias them
    # all to the same canonical destination so warehouse + distributor
    # populate correctly regardless of which form the layout produced.
    "CW-MID-ATLANTIC":       ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    "CW-MID-ATL":            ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    "CW-MID":                ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    "CW-NY":                 ("Chefs Warehouse, NY",            "Chefs Warehouse"),
    "CW-FL":                 ("Chefs Warehouse, FL",            "Chefs Warehouse"),
    "CW-C":                  ("Chefs Warehouse, Chicago",       "Chefs Warehouse"),
    # H&H's production sheet uses "JACKSON VILLE" (two words) for the
    # production destined to Cheney's Ocala DC — per JD, route it there.
    "JACKSON VILLE":         ("Ocala, FL",              "Cheney Brothers"),
    "JACKSONVILLE":          ("Ocala, FL",              "Cheney Brothers"),
    "JACK-VILLE":            ("Ocala, FL",              "Cheney Brothers"),
    # Common typos seen on hand-keyed sheets — route them like the
    # correctly-spelled originals so the operator doesn't have to.
    "MNASSAS":               ("Manassas, VA",           "US Foods"),
    "CWFLL":                 ("Chefs Warehouse, FL",    "Chefs Warehouse"),
    # Customers / additional distributors that surfaced in the historical
    # data and JD has tagged with a canonical mapping.
    # DeliBag is a South Korean account — full-container loads (1120 cs /
    # 20 pallets at a time) ship out under their own distributor entry.
    "DELIBAG":               ("DeliBag, South Korea",   "DeliBag"),
    "CARMELA FOODS":         ("Carmela Foods",          "Carmela Foods"),
    # Bare "CHENEY" without a city defaults to Riviera Beach per JD.
    "CHENEY":                ("Riviera Beach, FL",      "Cheney Brothers"),
    # Spelled-out forms of the same city-less Cheney label.
    "CHENEY BROTHERS":       ("Riviera Beach, FL",      "Cheney Brothers"),
    "CHENEY BROS":           ("Riviera Beach, FL",      "Cheney Brothers"),
    # US Foods' Chicago-area DC moved from Bensenville to AURORA; the PO
    # parser already maps AURORA -> "Chicago, IL" (usfoods_po_parser), but
    # production sheets headed "US FOODS AURORA" (5177033T, 2026-09-14) fell
    # through to a raw label and never reached the Chicago lots.
    "AURORA":                ("Chicago, IL",            "US Foods"),
    # USF's Alcoa DC is the Knoxville metro ("US.FOODS.KNOXVILLE.PO.1...").
    "KNOXVILLE":             ("Alcoa, TN",              "US Foods"),
    # "LA MIRANDA" is the recurring misspelling of La Mirada (7324474C,
    # 5959034C).
    "LA MIRANDA":            ("La Mirada, CA",          "US Foods"),
    "CW-CHICAGO":            ("Chefs Warehouse, Chicago", "Chefs Warehouse"),
    # Sheets baked for H&H's own freezer stock say "INVENTORY" in the subject.
    "INVENTORY":             ("In-House Inventory",     "H&H Bagels"),
    "INVENTORI":             ("In-House Inventory",     "H&H Bagels"),
    "IN HOUSE":              ("In-House Inventory",     "H&H Bagels"),
    "IN-HOUSE":              ("In-House Inventory",     "H&H Bagels"),
    "IN HOUSE INVENTORY":    ("In-House Inventory",     "H&H Bagels"),
    "IN-HOUSE INVENTORY":    ("In-House Inventory",     "H&H Bagels"),
}

#: Chefs Warehouse production sheets name the DC by region ("Chefs Warehouse,
#: NY"); the PO ledger names the same DC by its ship-to city ("Bronx, NY").
#: Stored records keep their production name -- this maps it at LINK time so
#: a warehouse join against the ledger works. Keyed by (warehouse,
#: distributor) so CW Chicago can never be confused with US Foods "Chicago, IL".
CW_PRODUCTION_TO_LEDGER_WAREHOUSE: dict[str, str] = {
    "Chefs Warehouse, NY":           "Bronx, NY",
    "Chefs Warehouse, FL":           "Opa Locka, FL",
    "Chefs Warehouse, Chicago":      "Chicago, IL",
    "Chefs Warehouse, Mid-Atlantic": "Hanover, MD",
}


def ledger_warehouse_for(warehouse: str, distributor: str) -> str:
    """The PO-ledger warehouse name for a production record's warehouse.

    Identity for everything except the four Chefs Warehouse DCs, whose
    production names differ from the ledger's ship-to cities."""
    if (distributor or "") == "Chefs Warehouse":
        return CW_PRODUCTION_TO_LEDGER_WAREHOUSE.get(warehouse or "", warehouse or "")
    return warehouse or ""


def warehouse_matches_ledger(warehouse: str, distributor: str,
                             ledger_warehouse: str, ledger_distributor: str) -> bool:
    """Does a production record's (warehouse, distributor) name the same DC
    as a ledger PO's (warehouse, distributor)? Distributor must agree too, so
    Chefs Warehouse Chicago never matches US Foods Chicago."""
    if not warehouse or not ledger_warehouse:
        return False
    if distributor and ledger_distributor and distributor != ledger_distributor:
        return False
    return ledger_warehouse_for(warehouse, distributor) == ledger_warehouse


@dataclass
class ProductionLine:
    variety: str          # canonical variety name
    raw_variety: str      # exactly as it appeared on the sheet
    cs_count: int
    lot_number: str = ""
    #: Set when this line was DERIVED rather than read off the sheet -- today
    #: only by the assorted split, which turns one "24 CS ASSORTED" row into
    #: four. Keeps the estimate visible and reversible: raw_variety still holds
    #: the original label, so re-splitting on a different mix is a re-run.
    derived_from: str = ""


@dataclass
class ProductionSheet:
    production_date: str = ""         # ISO YYYY-MM-DD if parseable, else MM/DD/YYYY raw
    warehouse: str = ""               # canonical "City, ST"
    warehouse_raw: str = ""           # exactly as printed on the sheet
    distributor: str = ""             # inferred from warehouse
    po_number: str = ""
    #: The PO as printed in the PDF header, and as written in the subject.
    #: Kept apart so a disagreement (a template whose header PO wasn't
    #: updated -- CWC 7/1 sheet printed CWFL's 1135526) can be resolved
    #: against the PO ledger instead of the body silently winning.
    po_body: str = ""
    po_subject: str = ""
    lines: list = field(default_factory=list)
    total_cases: int = 0
    unmapped_varieties: list = field(default_factory=list)
    raw_text: str = ""
    error: str = ""


# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

# "<qty> CS <variety>"  — variety may include hyphens and spaces, stops at
# end of line / before next number.
_LINE_RE = re.compile(
    r"^\s*(\d+)\s+CS\s+([A-Z][A-Z0-9\- ]+?)\s*$",
    re.MULTILINE,
)

# "<total> Total Cases<WAREHOUSE>.PO.<po_number>"
_HEADER_RE = re.compile(
    # "<total> Total Cases [<lot_digits>] <WAREHOUSE>.PO.<po_number>"
    # La Mirada renders an extra lot # between "Cases" and the warehouse
    # because of how pypdf merges adjacent text runs; absorb optional
    # leading digits before the warehouse name. Warehouse may contain
    # hyphens (e.g. "CW-MID-ATL", "CW-NY") — Chefs Warehouse routes use
    # them on the PDF header even though the subject uses other forms.
    r"(\d+)\s+Total\s+Cases\s*\d*\s*([A-Z][A-Z 0-9\-]*?)\s*\.\s*PO\s*\.\s*([A-Z0-9]+)",
    re.IGNORECASE,
)

# Fallback: extract the warehouse from a Daily Production subject line.
# Subjects observed in the wild:
#   "Daily Production OCALA.PO.054511694374"
#   "Daily Production US FOODS ZEBULON.PO.5210265G"
#   "Daily Production CW-MID-ATLANTIC PO.1095389"        (space, no dot)
#   "Daily production US FOODS MANASSAS PO.4363705O"     (lowercase 'p')
# Captures everything between "Daily Production " and the trailing
# "[. ]PO." token. The PDF header is still the source of truth when
# present; this fallback only fires when the body header didn't yield a
# warehouse.
_SUBJECT_WAREHOUSE_RE = re.compile(
    r"daily\s{1,3}production\s{1,3}(.{1,80}?)\s{0,3}[.\s]\s{0,3}PO\s{0,3}[.\s]\s{0,3}[A-Z0-9]+",
    re.IGNORECASE,
)

# Date inline in the lot column, e.g. "11890430264/30/2026" -> "4/30/2026"
_DATE_RE = re.compile(r"(?<!\d)((?:0?[1-9]|1[0-2])/\d{1,2}/\d{4})(?!\d)|(?:^|\D)((?:0?[1-9]|1[0-2])/\d{1,2}/\d{4})(?!\d)")


# --------------------------------------------------------------------------
# Warehouse classification
# --------------------------------------------------------------------------

# Aliases for partial "US FOODS <abbr>" — JD's production team uses
# short city abbreviations on the sheet that don't match the canonical
# city in _WAREHOUSE_TO_CANONICAL. Add new entries here as they surface.
_USF_CITY_ALIASES: dict[str, str] = {
    "LA": "LA MIRADA",     # observed in "US FOODS LA"
}


def _classify_warehouse(raw: str) -> "tuple[str, str]":
    """Resolve a raw warehouse label to (canonical_warehouse, distributor).

    Order of precedence:
      1. Empty / missing -> ("In-House Inventory", "H&H Bagels") —
         production sheets without a header pointed at a specific
         distributor PO; per JD these are baked for general inventory.
      2. Exact match in _WAREHOUSE_TO_CANONICAL
      3. "CW<XX>" 2-letter state code   -> Chefs Warehouse, <XX>
      4. "US FOODS <CITY-OR-ABBR>" prefix -> look up city (with alias
         table for shorthand like LA -> LA MIRADA)
      5. Otherwise, pass through the raw label with empty distributor

    Always returns a 2-tuple.
    """
    if not raw or not raw.strip():
        return "In-House Inventory", "H&H Bagels"
    key = raw.strip().upper()
    canonical = _WAREHOUSE_TO_CANONICAL.get(key)
    if canonical:
        return canonical

    # 2-letter CW<STATE> codes (CWDC, CWTX, etc.). Multi-letter region
    # codes (CWC, CWMID) are handled via explicit entries above.
    cw_match = re.match(r"^CW([A-Z]{2})$", key)
    if cw_match:
        return (f"Chefs Warehouse, {cw_match.group(1)}", "Chefs Warehouse")

    # "US FOODS <CITY>" — strip the prefix and look up the city alone.
    # Apply alias table (LA -> LA MIRADA) so shorthand variants resolve.
    if key.startswith("US FOODS "):
        city_key = key[len("US FOODS "):].strip()
        city_key = _USF_CITY_ALIASES.get(city_key, city_key)
        cm = _WAREHOUSE_TO_CANONICAL.get(city_key)
        if cm:
            return cm

    loose = _classify_loose(key)
    if loose:
        return loose

    return (raw, "")


def _squash(s: str) -> str:
    """Upper-case and drop everything but letters/digits, so "US FOODS LA
    MIRADA" (double space), "PUNTA.GORDA" and "CWMID-ATL" compare equal to
    their canonical keys."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


#: Distributor words typed in front of the city, most specific first. The
#: hand-keyed sheets misspell these too ("US FOODOS", "CHENEY BROTHEHRS").
_DIST_PREFIXES = (
    "USFOOODS", "USFOODOS", "USFOODS", "USF",
    "CHENEYBROTHEHRS", "CHENEYBROTHERS", "CHENEYBROTHER", "CHENEYBROS",
    "CHENEYBRO", "CHENEY",
)

_SQUASHED_CANONICAL: dict = {}
for _k, _v in _WAREHOUSE_TO_CANONICAL.items():
    _SQUASHED_CANONICAL.setdefault(_squash(_k), _v)


def _classify_loose(key: str):
    """Last-resort warehouse match for hand-keyed labels.

    The exact-key table missed 64 sheets / 14,285 cs whose label was only a
    spacing, punctuation, prefix or truncation away from a known DC
    ("US FOODS LA  MIRADA", "CHENEY BROTHEHRS OCALA", "RIVIERA B",
    "CW-MID-AT"). Returns (warehouse, distributor) or None.
    """
    sq = _squash(key)
    if not sq:
        return None
    hit = _SQUASHED_CANONICAL.get(sq)
    if hit:
        return hit
    # "CHEFS WAREHOUSE MID ATL" -> "CWMIDATL"
    if sq.startswith("CHEFSWAREHOUSE") and len(sq) > len("CHEFSWAREHOUSE"):
        sq = "CW" + sq[len("CHEFSWAREHOUSE"):]
        hit = _SQUASHED_CANONICAL.get(sq)
        if hit:
            return hit
    for pre in _DIST_PREFIXES:
        if sq.startswith(pre) and len(sq) > len(pre):
            rest = sq[len(pre):]
            rest = _squash(_USF_CITY_ALIASES.get(rest, rest)) if pre.startswith("USF") else rest
            hit = _SQUASHED_CANONICAL.get(rest)
            if hit:
                return hit
            sq = rest
            break
    # Truncated label ("RIVIERA B", "CW-MID-AT"): accept a prefix only when
    # it is long enough to mean something and names exactly one DC.
    if len(sq) >= 6:
        hits = {v for k, v in _SQUASHED_CANONICAL.items() if k.startswith(sq)}
        if len(hits) == 1:
            return hits.pop()
    return None


#: Daily Production subjects end in the PO: "PO.123", "PO#123", "PO #123",
#: "P.O. 123", "PO-123", "CWC.PO.1135525". The number must contain a digit,
#: so "DELIBAG PO. daily production" no longer yields the PO "daily".
_SUBJECT_PO_RE = re.compile(
    r"(?<![A-Z0-9])P\.?O\.?(?:\s*(?:#|NO\.?|NUM(?:BER)?\.?))?"
    r"(?:[\s.:#\-]+|(?=\d))([A-Z0-9]*\d[A-Z0-9]*)",
    re.IGNORECASE,
)
#: Where the PO part of a subject starts (with or without a number), so the
#: DC label in front of it can be cut out.
_SUBJECT_PO_START_RE = re.compile(r"(?<![A-Z0-9])P\.?O(?![A-Z])", re.IGNORECASE)


def subject_po(subject: str) -> str:
    """The PO number written in a Daily Production subject, or ''."""
    m = _SUBJECT_PO_RE.search(subject or "")
    if m:
        return m.group(1).strip().upper()
    # "PO.US FOODS MANASSAS 8750515O" / "US FOOS.PO.KNOXVILLE 8764076H": the
    # DC sits between the PO marker and the number. Take the last
    # number-bearing token (5+ chars, so a date's 10/13 never qualifies).
    pm = _SUBJECT_PO_START_RE.search(subject or "")
    if pm:
        toks = [t for t in re.split(r"[^A-Z0-9]+", subject[pm.end():].upper())
                if len(t) >= 5 and any(c.isdigit() for c in t)]
        if toks:
            return toks[-1]
    return ""


def _subject_rest(subject: str) -> str:
    s = re.sub(r"^\s*(?:(?:FW|FWD|RE)\s*:\s*)+", "", subject or "", flags=re.IGNORECASE)
    # No \b after "production": "Daily ProductionUS FOODS.MANASSAS.PO.." is real.
    m = re.search(r"daily\s*production(?:\s+sheet\b)?(.*)$", s, re.IGNORECASE)
    if not m:
        return ""
    if not m.group(1).strip(" .-_#,:\t") and m.start() > 0:
        # "DELIBAG PO. daily production": the label comes first.
        return s[:m.start()]
    return m.group(1)


def subject_warehouse_labels(subject: str) -> list:
    """Candidate DC labels in a Daily Production subject, best first: the
    text before the PO marker ("US FOODS HOUSTON" in "... US FOODS HOUSTON
    PO.393072B2"), then the text after it minus the PO number ("US FOODS
    MANASSAS" in "... PO.US FOODS MANASSAS 8750515O")."""
    rest = _subject_rest(subject)
    if not rest:
        return []
    out = []
    pm = _SUBJECT_PO_START_RE.search(rest)
    before = rest[:pm.start()] if pm else rest
    before = before.strip(" .-_#,:\t").strip()
    if before:
        out.append(before)
    if pm:
        po = subject_po(subject)
        after = rest[pm.end():]
        if po:
            after = re.sub(re.escape(po), " ", after, flags=re.IGNORECASE)
        after = re.sub(r"^[\s.:#\-]*(?:#|NO\.?)?", "", after).strip(" .-_#,:\t").strip()
        if after and after not in out:
            out.append(after)
    return out


def subject_warehouse_label(subject: str) -> str:
    """The DC label a Daily Production subject names, e.g. "US FOODS HOUSTON"
    from "Daily Production US FOODS HOUSTON PO.393072B2". '' when absent."""
    labels = subject_warehouse_labels(subject)
    for lab in labels:
        if _classify_warehouse(lab)[1]:
            return lab
    return labels[0] if labels else ""


def classify_warehouse_with_subject(warehouse_raw: str, subject: str = "") -> "tuple[str, str]":
    """_classify_warehouse, falling back to the DC named in the SUBJECT when
    the body label is blank or unmatched.

    Records ingested before a label was known (Houston 393072B2 stored as
    "US FOODS HOUSTON") or whose PDF header didn't parse (64 sheets / 14,285
    cs stored with no warehouse) still carry the DC in their subject line.
    """
    wh, dist = _classify_warehouse(warehouse_raw)
    if dist and (warehouse_raw or "").strip():
        return wh, dist
    for label in subject_warehouse_labels(subject):
        swh, sdist = _classify_warehouse(label)
        if sdist:
            return swh, sdist
    return wh, dist


# --------------------------------------------------------------------------
# Production date
# --------------------------------------------------------------------------

#: A sheet is emailed within days of the bake. A lot or typed date more than
#: this far before the email -- or after it -- is a typo, not a bake date.
PLAUSIBLE_PRODUCTION_WINDOW_DAYS = 21
_LOCAL_TZ = "America/New_York"   # the bakery's calendar


def received_local_date(received_at: str):
    """The bakery-local calendar date of an email's received_at, or None.

    Graph stamps receivedDateTime in UTC ("...Z"); a naive value is treated as
    UTC too (the server's datetime.now() on Render). A sheet received at
    8 pm ET is still that day's sheet."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    s = (received_at or "").strip()
    if not s:
        return None
    try:
        dt = _dt.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = _dt.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(_LOCAL_TZ)).date()
    except Exception:  # noqa: BLE001 -- no tz database: fixed EST offset
        return (dt.astimezone(_tz.utc) - _td(hours=5)).date()


def lot_date_iso(lot: str) -> str:
    """The production date encoded in an H&H lot code, as ISO, or ''.

    <4-digit item><MMDDYYYY> (12 digits) or legacy <item><MMDDYY> (10)."""
    from datetime import date as _date
    d = re.sub(r"\D", "", lot or "")
    try:
        if len(d) == 12:
            return _date(int(d[8:12]), int(d[4:6]), int(d[6:8])).isoformat()
        if len(d) == 10:
            return _date(2000 + int(d[8:10]), int(d[4:6]), int(d[6:8])).isoformat()
    except ValueError:
        return ""
    return ""


def is_plausible_production_date(iso: str, received_at: str) -> bool:
    """Within [received - 21 days, received] on the bakery's calendar."""
    from datetime import date as _date, timedelta as _td
    recv = received_local_date(received_at)
    if recv is None or not iso:
        return False
    try:
        d = _date.fromisoformat(str(iso)[:10])
    except ValueError:
        return False
    return recv - _td(days=PLAUSIBLE_PRODUCTION_WINDOW_DAYS) <= d <= recv


def choose_production_date(lot_dates, slash_dates, received_at: str,
                           default: str = "") -> str:
    """The sheet's production date.

    Was: the first typed date, else the EARLIEST lot date -- so one mistyped
    lot (1189031026 for 091026 on Riviera 014511751616) back-dated the whole
    sheet six months and FIFO drained it first. Now: the most common
    PLAUSIBLE lot date (ties -> earliest), else a plausible typed date, else
    the received date. Without a received_at nothing can be judged, so the
    caller's ``default`` (the old rule) stands.
    """
    from collections import Counter as _Counter
    recv = received_local_date(received_at)
    if recv is None:
        return default
    lots = [d for d in (lot_dates or []) if d and is_plausible_production_date(d, received_at)]
    if lots:
        counts = _Counter(lots)
        top = max(counts.values())
        return min(d for d, n in counts.items() if n == top)
    for d in (slash_dates or []):
        if d and is_plausible_production_date(d, received_at):
            return str(d)[:10]
    return recv.isoformat()


# --------------------------------------------------------------------------
# Parse entrypoints
# --------------------------------------------------------------------------

def parse_production_pdf(pdf_bytes: bytes, subject: str = "",
                         received_at: str = "") -> ProductionSheet:
    try:
        text = _extract_text(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        return ProductionSheet(error=f"PDF text extraction failed: {exc}")
    return parse_production_text(text, subject=subject, received_at=received_at)


def parse_production_text(text: str, subject: str = "",
                          received_at: str = "") -> ProductionSheet:
    sheet = ProductionSheet(raw_text=text)
    if not text or not text.strip():
        sheet.error = "PDF contained no extractable text (image-only scan?)"
        return sheet

    # Header — warehouse + PO number from "<total> Total Cases<WAREHOUSE>.PO.<po#>"
    m = _HEADER_RE.search(text)
    if m:
        sheet.total_cases = int(m.group(1))
        sheet.warehouse_raw = m.group(2).strip()
        sheet.po_number = m.group(3).strip()
        sheet.po_body = sheet.po_number
        sheet.warehouse, sheet.distributor = _classify_warehouse(sheet.warehouse_raw)
    sheet.po_subject = subject_po(subject)

    # Production date — legacy rule first (a slash-format MM/DD/YYYY anywhere
    # in the document, else the earliest lot date). It only stands when there
    # is no received_at to judge plausibility against; see
    # choose_production_date below.
    slash_dates = []
    for md in _DATE_RE.finditer(text):
        raw = md.group(1) or md.group(2)
        try:
            mm, dd, yyyy = raw.split("/")
            slash_dates.append(f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}")
        except ValueError:
            slash_dates.append(raw)
    if slash_dates:
        # Only a malformed date string (bad split / int) falls back to the raw
        # value; anything else is a real bug and should surface.
        sheet.production_date = slash_dates[0]
    else:
        dates_from_lots = sorted(_dates_from_lot_numbers(text))
        if dates_from_lots:
            sheet.production_date = dates_from_lots[0]

    # Line items — "<qty> CS <variety>"
    seen_unknown = set()
    for match in _LINE_RE.finditer(text):
        cs = int(match.group(1))
        raw_v = match.group(2).strip()
        # A "TOTAL" footer row matches the line regex exactly like a real
        # item. Storing it double-counts the sheet.
        if is_non_item_label(raw_v):
            continue
        canonical, recognized = _normalize_variety(raw_v)
        if not recognized:
            seen_unknown.add(raw_v)
        if canonical == "Assorted":
            for v, part in split_assorted(cs):
                if part:
                    sheet.lines.append(ProductionLine(
                        variety=v, raw_variety=raw_v, cs_count=part,
                        derived_from=raw_v.strip().upper(),
                    ))
            continue
        sheet.lines.append(ProductionLine(
            variety=canonical, raw_variety=raw_v, cs_count=cs,
        ))
    sheet.unmapped_varieties = sorted(seen_unknown)

    # Pair each line with its lot for traceability.
    # Lots are queued per variety (item-code -> variety via
    # HH_MFG_CODE_TO_VARIETY) and assigned to each matching line in
    # text-position order. This handles:
    #   - The single-day case (one lot per line, same variety)
    #   - Multi-day production (same variety appears on multiple lines
    #     with different lots — each line gets its own)
    #   - PDF quirks where one lot lands above the "Lot#" header in
    #     text-flow (we sort by source text offset, not column position)
    try:
        from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY
    except ImportError:  # standalone test invocation
        from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY  # type: ignore
    lots_by_variety: dict = {}
    sheet_lot_dates = []
    for lot in _extract_lots(text):
        canonical = HH_MFG_CODE_TO_VARIETY.get(lot["item_code"])
        if not canonical:
            continue
        lots_by_variety.setdefault(canonical, []).append(lot["lot"])
        sheet_lot_dates.append(lot["date_iso"])
    # Lines read off the sheet claim their lots BEFORE lines derived from an
    # assorted split. In sheet order, "24 CS ASSORTED" above "56 CS
    # PARB-PLAIN" handed the only Plain lot to the 6-cs derived Plain share,
    # leaving the real 56-cs line lot-less and out of FIFO.
    for derived_pass in (False, True):
        for line in sheet.lines:
            if bool(line.derived_from) != derived_pass:
                continue
            queue = lots_by_variety.get(line.variety)
            if queue:
                line.lot_number = queue.pop(0)

    # The date the sheet is filed under. With a received_at, a mistyped lot
    # or typed date can no longer drag the sheet months back (Riviera
    # 014511751616 filed under 2026-03-10; La Mirada 5579094C under 03-23).
    sheet.production_date = choose_production_date(
        sheet_lot_dates, slash_dates, received_at, default=sheet.production_date)

    # If the header total didn't parse but we got lines, compute it.
    if not sheet.total_cases and sheet.lines:
        sheet.total_cases = sum(L.cs_count for L in sheet.lines)

    # Subject fallback — pull PO and/or warehouse out of the subject when
    # the PDF body header didn't yield them. Common cases: image-only
    # scan PDFs (no extractable text) and layouts where pypdf splits the
    # header line in a way the regex above misses. A body label that IS
    # present but unmatched ("US FOODS HOUSTON" before Houston was mapped)
    # also falls back to the DC the subject names.
    if not sheet.po_number and sheet.po_subject:
        sheet.po_number = sheet.po_subject
    if subject and (not sheet.warehouse_raw or not sheet.distributor):
        label = subject_warehouse_label(subject)
        if label:
            swh, sdist = _classify_warehouse(label)
            # An unrecognised label is kept only when the subject has the
            # "<DC> PO.<n>" shape (as before), so "Daily Production Sheet
            # Template.xlsx" doesn't become a warehouse called Template.xlsx.
            if not sheet.warehouse_raw and (sdist or _SUBJECT_PO_START_RE.search(subject)):
                sheet.warehouse_raw = label
                sheet.warehouse, sheet.distributor = swh, sdist
            elif sdist:
                sheet.warehouse, sheet.distributor = swh, sdist

    if not sheet.lines:
        sheet.error = (sheet.error or "") + " no line items detected".strip()

    return sheet


_DIGIT_RUN_RE = re.compile(r"\d{10,}")


def _extract_lots(text: str) -> list:
    """Walk every digit run in the text and return the lots we can
    confidently parse.

    Each lot is {lot: str, item_code: str, date_iso: str}. Preserves
    order of appearance in the source text so callers that need to
    pair lots with line items positionally can do so.

    H&H lot codes are <4-digit item><MMDDYYYY> (current 12-digit) or
    <4-digit item><MMDDYY> (legacy 10-digit). We prefer the 12-digit
    interpretation per run and fall back to 10-digit when nothing
    plausible parsed at 12.
    """
    out = []
    seen_starts = set()
    for m in _DIGIT_RUN_RE.finditer(text):
        run = m.group(0)
        run_offset = m.start()
        run_hits: list = []
        # 12-digit pass
        i = 0
        while i <= len(run) - 12:
            window = run[i:i+12]
            mm, dd, yyyy = window[4:6], window[6:8], window[8:12]
            try:
                mm_i, dd_i, yyyy_i = int(mm), int(dd), int(yyyy)
            except ValueError:
                i += 1
                continue
            if (1 <= mm_i <= 12 and 1 <= dd_i <= 31
                    and 1900 <= yyyy_i <= 2099):
                run_hits.append({
                    "lot":       window,
                    "item_code": window[:4],
                    "date_iso":  f"{yyyy_i:04d}-{mm_i:02d}-{dd_i:02d}",
                    "_offset":   run_offset + i,
                })
                i += 12     # consume this window so it isn't re-matched
                continue
            i += 1
        # 10-digit fallback only if 12-digit found nothing
        if not run_hits:
            i = 0
            while i <= len(run) - 10:
                window = run[i:i+10]
                mm, dd, yy = window[4:6], window[6:8], window[8:10]
                try:
                    mm_i, dd_i, yy_i = int(mm), int(dd), int(yy)
                except ValueError:
                    i += 1
                    continue
                if 1 <= mm_i <= 12 and 1 <= dd_i <= 31:
                    year = 2000 + yy_i if yy_i < 70 else 1900 + yy_i
                    run_hits.append({
                        "lot":       window,
                        "item_code": window[:4],
                        "date_iso":  f"{year:04d}-{mm_i:02d}-{dd_i:02d}",
                        "_offset":   run_offset + i,
                    })
                    i += 10
                    continue
                i += 1
        out.extend(run_hits)
    # Final sort: by source text offset so positional pairing works
    out.sort(key=lambda x: x["_offset"])
    return out


def _dates_from_lot_numbers(text: str) -> set:
    """Convenience wrapper: return the distinct ISO dates encoded in lot
    numbers. Use _extract_lots when you need the full lot tuples
    (e.g. for per-line traceability)."""
    return {lot["date_iso"] for lot in _extract_lots(text)}


def _extract_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


__all__ = [
    "ProductionLine", "ProductionSheet",
    "parse_production_pdf", "parse_production_text",
]
