"""Shared H&H internal bagel mfg-code map.

Every distributor who carries H&H bagels stores H&H's own internal SKU
code alongside their own catalog number:

    US Foods       -> USF "item #" column (e.g. 1150, 1152, 1158)
    Cheney Bros    -> Cheney "Mfg#" field on each line of the PO PDF

Both point to the same canonical H&H variety. This module is the single
source of truth. Extend as new codes appear on POs.

Coverage today (12 codes, 2026-05-13):
    1150  Plain                   (USF POs)
    1151  Onion                   (USF POs - from "BAGEL, ONION 4.25 Z UNSL PARBK")
    1152  Poppy Seed              (USF + Cheney POs)
    1153  Sesame                  (USF POs)
    1155  Cinnamon Raisin         (Cheney POs)
    1156  Whole Wheat             (USF + Cheney POs - "BAGEL, WHL WHEAT 4.25 Z UNSL")
    1157  Whole Wheat Everything  (USF POs - "BAGEL, EVTHG WHL WHEAT 4.06 Z")
    1158  Everything              (USF + Cheney POs)
    1159  Asiago                  (USF POs - from "ASIGO CHS WHEAT")
    1171  Blueberry               (Cheney POs)
    1184  Egg                     (USF POs)
    1189  Jalapeno Cheddar        (USF POs - from "JLP CHEDR CHS WHEAT")
"""

HH_MFG_CODE_TO_VARIETY: dict[str, str] = {
    "1150": "Plain",
    "1151": "Onion",
    "1152": "Poppy Seed",
    "1153": "Sesame",
    "1155": "Cinnamon Raisin",
    "1156": "Whole Wheat",
    "1157": "Whole Wheat Everything",
    "1158": "Everything",
    "1159": "Asiago",
    "1171": "Blueberry",
    "1184": "Egg",
    "1189": "Jalapeno Cheddar",
}

# Cheney Brothers catalog item # -> H&H mfg code. Cheney's on-hand stock
# export ("Item # / Description / ... / Stock") has no H&H mfg column, so this
# crosswalk resolves variety from Cheney's own catalog number. Derived from
# Cheney's case-movement export, which lists Dist Item # and Mfq.Product Code
# side by side. Extend as new items appear.
CHENEY_ITEM_NO_TO_MFG: dict[str, str] = {
    "10153018": "1150",  # Plain
    "10153034": "1151",  # Onion
    "10153019": "1152",  # Poppy Seed
    "10153041": "1153",  # Sesame
    "10153046": "1155",  # Cinnamon Raisin
    "10153042": "1156",  # Whole Wheat
    "10153049": "1157",  # Whole Wheat Everything
    "10153048": "1158",  # Everything
    "10153043": "1159",  # Asiago
    "10153044": "1171",  # Blueberry
    "10153047": "1184",  # Egg
    "10153045": "1189",  # Jalapeno Cheddar
}

__all__ = ["HH_MFG_CODE_TO_VARIETY", "CHENEY_ITEM_NO_TO_MFG"]
