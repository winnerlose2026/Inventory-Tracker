#!/usr/bin/env python3
"""PDF-header PO vs subject PO, the subject PO regex, and warehouse mapping.

* The CWC 7/1 sheet's header printed CWFL's PO 1135526 (template not
  updated); the subject said 1135525. The header won silently, booking
  Chicago's bake to the Florida PO. 3 of 4 disagreements since April were
  header typos.
* The subject regex missed "PO#2562955G" and read "DELIBAG PO. daily
  production" as PO "daily".
* US FOODS AURORA / KNOXVILLE / "CHENEY BROTHERS OCALA" / "RIVIERA B" etc.
  were unmatched raw labels, and a blank header ignored the DC in the subject
  (Houston 393072B2 stayed "US FOODS HOUSTON", so Houston showed no lots).
* Chefs Warehouse production names ("Chefs Warehouse, NY") never matched the
  ledger's ship-to cities ("Bronx, NY").
"""
import sys
sys.path.insert(0, ".")

import pytest

from integrations.production_pdf_parser import (
    _classify_warehouse, classify_warehouse_with_subject, ledger_warehouse_for,
    parse_production_text, subject_po, warehouse_matches_ledger,
)
from integrations.production_store import resolve_po

LEDGER = {
    "1135526": {"po_number": "1135526", "warehouse": "Opa Locka, FL", "distributor": "Chefs Warehouse"},
    "1135525": {"po_number": "1135525", "warehouse": "Chicago, IL", "distributor": "Chefs Warehouse"},
    "1127191": {"po_number": "1127191", "warehouse": "Bronx, NY", "distributor": "Chefs Warehouse"},
    "014511688792": {"po_number": "014511688792", "warehouse": "Riviera Beach, FL",
                     "distributor": "Cheney Brothers"},
}


@pytest.mark.parametrize("subject,po", [
    ("Daily Production US FOODS ZEBULON PO#2562955G", "2562955G"),
    ("Daily Production PO #4511763291", "4511763291"),
    ("Daily Production P.O. 1234567", "1234567"),
    ("Daily Production CW PO-1226111", "1226111"),
    ("Daily Production CWC.PO.1135525", "1135525"),
    ("Daily Production US FOODS MANASSAS. PO. 8868475O", "8868475O"),
    ("DELIBAG PO. daily production", ""),
    ("Daily Production PO.US FOODS MANASSAS 8750515O", "8750515O"),
    ("Daily Production Sheet Template.xlsx", ""),
])
def test_subject_po(subject, po):
    assert subject_po(subject) == po


def _sheet_record(text, subject):
    s = parse_production_text(text, subject=subject, received_at="2026-07-01T19:42:05Z")
    rec = {"warehouse": s.warehouse, "distributor": s.distributor, "po_number": s.po_number}
    return s, rec


def test_cwc_sheet_with_cwfl_header_po_moves_to_its_subject_po():
    s, rec = _sheet_record("CS Count SKU\n56 CS PLAIN\n56 CS EVERYTHING\n"
                           "112 Total CasesCWC.PO.1135526\n", "Daily Production CWC.PO.1135525")
    assert (s.po_body, s.po_subject) == ("1135526", "1135525")
    resolve_po(rec, s.po_body, s.po_subject, LEDGER)
    assert rec["po_number"] == "1135525"
    assert rec["po_resolved"]["chosen"] == "1135525" and "po_conflict" not in rec


def test_only_one_in_the_ledger_wins():
    rec = {"warehouse": "Chefs Warehouse, NY", "distributor": "Chefs Warehouse"}
    resolve_po(rec, "1127197", "1127191", LEDGER)
    assert rec["po_number"] == "1127191"
    rec = {"warehouse": "Riviera Beach, FL", "distributor": "Cheney Brothers"}
    resolve_po(rec, "014511688792", "041511688792", LEDGER)     # body right here
    assert rec["po_number"] == "014511688792"


def test_neither_in_the_ledger_keeps_body_and_flags():
    rec = {"warehouse": "Chefs Warehouse, NY", "distributor": "Chefs Warehouse"}
    resolve_po(rec, "909963", "909969", LEDGER)
    assert rec["po_number"] == "909963"
    assert rec["po_conflict"] == {"body": "909963", "subject": "909969"}


def test_both_in_ledger_with_no_warehouse_tiebreak_keeps_body_and_flags():
    rec = {"warehouse": "", "distributor": ""}
    resolve_po(rec, "1135526", "1135525", LEDGER)
    assert rec["po_number"] == "1135526" and rec["po_conflict"]


@pytest.mark.parametrize("raw,want", [
    ("US FOODS AURORA", ("Chicago, IL", "US Foods")),
    ("US.FOODS.KNOXVILLE", ("Alcoa, TN", "US Foods")),
    ("US FOODS HOUSTON", ("Houston, TX", "US Foods")),
    ("US FOODS LA  MIRADA", ("La Mirada, CA", "US Foods")),
    ("US FOODOS LA MIRADA", ("La Mirada, CA", "US Foods")),
    ("US FOODS LA MIRANDA", ("La Mirada, CA", "US Foods")),
    ("CHENEY BROTHEHRS OCALA", ("Ocala, FL", "Cheney Brothers")),
    ("CHENEY BROTHERS PUNTA GORDA", ("Punta Gorda, FL", "Cheney Brothers")),
    ("PUNTA.GORDA", ("Punta Gorda, FL", "Cheney Brothers")),
    ("RIVIERA B", ("Riviera Beach, FL", "Cheney Brothers")),
    ("CWMID-ATL", ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse")),
    ("CW-MID-AT", ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse")),
    ("CHEFS WAREHOUSE MID ATL", ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse")),
    ("ACE ENDICO", ("ACE ENDICO", "")),              # unknown stays unknown
    ("CW", ("CW", "")),                               # too short to guess
])
def test_classifier(raw, want):
    assert _classify_warehouse(raw) == want


def test_subject_dc_used_when_body_label_blank_or_unmatched():
    assert classify_warehouse_with_subject(
        "", "Daily Production US FOODS HOUSTON PO.393072B2") == ("Houston, TX", "US Foods")
    assert classify_warehouse_with_subject(
        "", "Daily ProductionUS FOODS . LA MIRANDA .PO.6814644C") == ("La Mirada, CA", "US Foods")
    assert classify_warehouse_with_subject(
        "US FOOS", "Daily Production US FOOS.PO.KNOXVILLE 8764076H") == ("Alcoa, TN", "US Foods")
    assert classify_warehouse_with_subject(
        "", "DELIBAG PO. daily production") == ("DeliBag, South Korea", "DeliBag")
    # blank with nothing usable in the subject: In-House, as before
    assert classify_warehouse_with_subject(
        "", "Daily Production Sheet") == ("In-House Inventory", "H&H Bagels")
    # a matched body label is never overridden by the subject
    assert classify_warehouse_with_subject(
        "OCALA", "Daily Production RIVIERA BEACH PO.1") == ("Ocala, FL", "Cheney Brothers")


def test_parser_upgrades_an_unmatched_body_label_from_the_subject():
    s = parse_production_text("CS Count SKU\n56 CS PLAIN\n56 Total CasesUS FOOS.PO.8764076H\n",
                              subject="Daily Production US FOODS KNOXVILLE PO.8764076H")
    assert (s.warehouse, s.distributor) == ("Alcoa, TN", "US Foods")


def test_chefs_warehouse_names_map_to_ledger_names_without_merging_chicago():
    assert ledger_warehouse_for("Chefs Warehouse, NY", "Chefs Warehouse") == "Bronx, NY"
    assert ledger_warehouse_for("Chefs Warehouse, FL", "Chefs Warehouse") == "Opa Locka, FL"
    assert ledger_warehouse_for("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse") == "Hanover, MD"
    assert warehouse_matches_ledger("Chefs Warehouse, Chicago", "Chefs Warehouse",
                                    "Chicago, IL", "Chefs Warehouse")
    assert not warehouse_matches_ledger("Chefs Warehouse, Chicago", "Chefs Warehouse",
                                        "Chicago, IL", "US Foods")
    assert not warehouse_matches_ledger("Chicago, IL", "US Foods",
                                        "Chicago, IL", "Chefs Warehouse")
