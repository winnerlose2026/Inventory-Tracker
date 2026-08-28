#!/usr/bin/env python3
"""Production sheet parsing: label resolution, footer rows, assorted splits.

Before 2026-08-28, 6,324 cases sat in the "In-House Inventory" catch-all.
Causes, in descending size:
  * a "TOTAL" footer row parsed as a line item (1,112 cs of pure double-count)
  * assorted cases with no mapping at all (1,374 cs)
  * hand-keyed typos of Everything, and Cinnamon Raisin written six ways
  * Pumpernickel (mfg 1154, the October LTO) never aliased
  * a lot code glued to the variety: "PARB-EVERYTHING 1158040226"
"""
import sys
sys.path.insert(0, ".")

from integrations.production_pdf_parser import (
    ASSORTED_SPLIT, _normalize_variety, assorted_kind, is_non_item_label,
    parse_production_text, split_assorted,
)


def canon(raw):
    return _normalize_variety(raw)[0]


def recognized(raw):
    return _normalize_variety(raw)[1]


# --- footer rows ----------------------------------------------------------

def test_total_row_is_not_a_line_item():
    assert is_non_item_label("TOTAL")
    assert is_non_item_label(" totals ")
    assert is_non_item_label("GRAND TOTAL")
    assert not is_non_item_label("PLAIN")


def test_total_row_is_dropped_from_parsed_lines():
    sheet = parse_production_text(
        "40 Total Cases RIVIERA BEACH.PO.0011\n04/12/2026\n"
        "24 CS PLAIN\n16 CS SESAME\n40 CS TOTAL\n")
    assert [L.variety for L in sheet.lines] == ["Plain", "Sesame"]
    assert sum(L.cs_count for L in sheet.lines) == 40


# --- the varieties that were falling through ------------------------------

def test_pumpernickel_resolves():
    for raw in ("PUMPERNICKEL", "PARB-PUMPERNICKEL", "PRNKL"):
        assert canon(raw) == "Pumpernickel", raw


def test_cinnamon_variants_all_reach_cinnamon_raisin():
    for raw in ("CINN RAISIN", "CINNAMON", "PARB-CINNAMON",
                "PARB-CINN RAISIN", "PARB- CINNAMON", "CIN RAISIN"):
        assert canon(raw) == "Cinnamon Raisin", raw


def test_everything_typos_resolve_by_fuzzy_match():
    for raw in ("PARB-EVRYTHING", "PARB-EVEYRTHING", "PARB-EVRERYTHING",
                "PARB-EVERTYTHING", "EVERYTTHING"):
        assert canon(raw) == "Everything", raw


def test_fuzzy_never_confuses_distinct_varieties():
    """The cutoff has to be tight enough that nothing drifts into a neighbour."""
    assert canon("ASIAGO") == "Asiago"
    assert canon("SESAME") == "Sesame"
    assert canon("ONION") == "Onion"
    # short/unknown junk must stay unmapped rather than snap to something
    assert not recognized("XQ")
    assert not recognized("ZZZZZZZZ")


def test_lot_code_glued_to_the_variety_is_stripped():
    assert canon("PARB-EVERYTHING 1158040226") == "Everything"
    assert canon("PARB-WW-ET 1157020626") == "Whole Wheat Everything"


def test_hyphenated_jalapeno():
    assert canon("PARB-JALAPENO-CHEDDAR") == "Jalapeno Cheddar"


# --- sliced is a form, not a variety --------------------------------------

def test_sliced_folds_into_the_base_variety():
    assert canon("PLAIN SLICED") == "Plain"
    assert canon("SLICED PLAIN") == "Plain"
    assert canon("EVERYTHING SLICED") == "Everything"
    assert canon("SLICED EVERYTHING") == "Everything"


# --- assorted -------------------------------------------------------------

def test_assorted_labels_are_recognised():
    for raw in ("ASSORTED", "ASST", "ASST SLICED", "ASSORTED SLICED"):
        assert assorted_kind(raw) == "assorted", raw
        assert canon(raw) == "Assorted", raw


def test_mini_assorted_stays_its_own_bucket():
    """A mini case is not a full-size case; folding it into the four
    full-size varieties would overstate them."""
    assert assorted_kind("MINI ASST") == "mini"
    assert canon("MINI ASST") == "Mini Assorted"
    assert canon("MINI ASSORTED") == "Mini Assorted"


def test_split_is_even_across_the_four_varieties():
    parts = dict(split_assorted(480))
    assert set(parts) == set(ASSORTED_SPLIT)
    assert all(v == 120 for v in parts.values())


def test_split_always_preserves_the_total():
    """A rounded split that loses a case would quietly corrupt the sheet."""
    for n in range(0, 200):
        assert sum(c for _, c in split_assorted(n)) == n


def test_assorted_line_expands_in_place():
    sheet = parse_production_text(
        "34 Total Cases OCALA.PO.0022\n05/02/2026\n"
        "24 CS ASSORTED\n10 CS PARB-PUMPERNICKEL\n")
    by_variety = {}
    for L in sheet.lines:
        by_variety[L.variety] = by_variety.get(L.variety, 0) + L.cs_count
    assert by_variety == {"Plain": 6, "Sesame": 6, "Everything": 6,
                          "Cinnamon Raisin": 6, "Pumpernickel": 10}
    assert sum(L.cs_count for L in sheet.lines) == 34


def test_expanded_lines_keep_their_provenance():
    """The split is H&H's known case makeup, not something the sheet said, so
    it has to stay visible and reversible."""
    sheet = parse_production_text(
        "24 Total Cases OCALA.PO.0022\n05/02/2026\n24 CS ASSORTED\n")
    assert all(L.derived_from == "ASSORTED" for L in sheet.lines)
    assert all(L.raw_variety == "ASSORTED" for L in sheet.lines)


def test_a_real_sheet_leaves_nothing_unmapped():
    """Every label that was in the production catch-all on 2026-08-28."""
    raws = ["PARB-WHOLEWHEAT", "PARB-WW-ET", "ASSORTED", "PARB-WWET",
            "PARB-WW", "PARB-PUMPERNICKEL", "WHOLEWHEAT", "CINNAMON RAISIN",
            "CINN RAISIN", "EVERYTHING SLICED", "PLAIN SLICED",
            "PARB- EVERYTHING", "ASST SLICED", "ASST", "PARB-CINNAMON",
            "PARB-CINN RAISIN", "PARB-JALAPENO-CHEDDAR", "SLICED EVERYTHING",
            "PARB- PLAIN", "SLICED PLAIN", "MINI ASST", "CINNAMON",
            "PARB- ONION", "ASSORTED SLICED", "PARB-EVERYTHING 1158040226",
            "PARB-EVRYTHING", "PARB-EVEYRTHING", "PARB-EVRERYTHING",
            "PARB-EVERTYTHING", "PARB-WW-ET 1157020626"]
    unmapped = [r for r in raws if not recognized(r)]
    assert unmapped == [], unmapped


# --- hand-keyed prefix / word-order / mini variants ------------------------
# Everything below was still landing in the catch-all after the first pass.
# Adding an alias per misspelling doesn't scale, so these are handled by the
# par-baked prefix family, a token-set index and a MINI rule.

def test_parbake_prefix_family():
    for raw in ("PARB-EVERYTHING", "PAARB-EVERYTHING", "PARABEKED EVERYTHING",
                "PARBAKED EVERYTHING", "PRB-EVERYTHING", "PAR-EVERYTHING"):
        assert canon(raw) == "Everything", raw
    assert canon("PAR-PUMPERNICKEL") == "Pumpernickel"
    assert canon("PRB-CINN RAISIN") == "Cinnamon Raisin"


def test_word_order_and_filler_words_do_not_matter():
    assert canon("PARB- CHEDDAR JALAPENO") == "Jalapeno Cheddar"
    assert canon("ASIAGO CHEESE") == "Asiago"
    assert canon("PLAIN BAGEL") == "Plain"


def test_sl_is_a_sliced_abbreviation():
    assert canon("EVERYTHING SL") == "Everything"


def test_mini_of_a_known_variety_keeps_its_own_bucket():
    """A mini case is not a full-size case, so it must never merge into the
    full-size variety total."""
    assert canon("MINI EVERYTHING") == "Mini Everything"
    assert canon("MINI PLAIN") == "Mini Plain"
    assert canon("ASST MINIS") == "Mini Assorted"
    assert canon("MINI ASST") == "Mini Assorted"


def test_mini_is_never_split_like_a_full_size_assorted():
    assert assorted_kind("MINI EVERYTHING") == ""
    assert assorted_kind("ASST MINIS") == "mini"


def test_prefix_family_cannot_swallow_a_real_variety():
    for raw in ("PLAIN", "POPPY SEED", "PUMPERNICKEL", "PARB-PLAIN"):
        assert recognized(raw), raw
    assert canon("PLAIN") == "Plain"
    assert canon("PUMPERNICKEL") == "Pumpernickel"


def test_every_label_seen_in_production_now_resolves():
    """The full residual set from the live data after the first renormalize."""
    raws = ["PAARB-EVERYTHING", "PRB-CINN RAISIN", "PARABEKED EVERYTHING",
            "ASST MINIS", "MINI EVERYTHING", "PARB- CHEDDAR JALAPENO",
            "ASIAGO CHEESE", "PAR-PUMPERNICKEL", "EVERYTHING SL",
            "PARB-WHOLEWHEAT", "PARB-WW-ET", "PARB-WWET", "PARB-WW",
            "CINNAMON RAISIN", "PARB- EVERYTHING", "PARB- PLAIN",
            "PARB-EVERYTTHING", "WW-ET", "PARB- ONION"]
    assert [r for r in raws if not recognized(r)] == []
