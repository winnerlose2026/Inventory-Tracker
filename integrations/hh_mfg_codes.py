"""Shared H&H internal bagel mfg-code map.

Every distributor who carries H&H bagels stores H&H's own internal SKU
code alongside their own catalog number:

    US Foods       -> USF "item #" column (e.g. 1150, 1152, 1158)
    Cheney Bros    -> Cheney "Mfg#" field on each line of the PO PDF

Both point to the same canonical H&H variety. This module is the single
source of truth. Extend as new codes appear on POs.

Coverage today (9 codes, 2026-04-23):
    1150  Plain             (USF POs)
    1152  Poppy Seed        (USF + Cheney POs)
    1153  Sesame            (USF POs)
    1155  Cinnamon Raisin   (Cheney POs)
    1158  Everything        (USF + Cheney POs)
    1159  Asiago            (USF POs - from "ASIGO CHS WHEAT")
    1171  Blueberry         (Cheney POs)
    1184  Egg               (USF POs)
    1189  Jalapeno Cheddar  (USF POs - from "JLP CHEDR CHS WHEAT")
"""

HH_MFG_CODE_TO_VARIETY: dict[str, str] = {
    "1150": "Plain",
    "1152": "Poppy Seed",
    "1153": "Sesame",
    "1155": "Cinnamon Raisin",
    "1158": "Everything",
    "1159": "Asiago",
    "1171": "Blueberry",
    "1184": "Egg",
    "1189": "Jalapeno Cheddar",
}

__all__ = ["HH_MFG_CODE_TO_VARIETY"]
