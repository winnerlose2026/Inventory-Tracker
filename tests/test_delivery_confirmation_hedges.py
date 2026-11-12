"""The server-side confirmation reader does not take a partial delivery, a
hand-off to a terminal / carrier, or "out for delivery" as a full delivery.
A wrong "delivered" releases a whole PO into on-hand; a missed one only waits
for a person."""
import sys

sys.path.insert(0, ".")

import pytest

from integrations.po_delivery import confirmation_lines

PLAIN = [
    "This shows delivered 0900AM/22nd",
    "Yes this shows delivered 0800AM/19th",
    "This load delivered 11/2",
    "7236706H-delivered",
    "Delivered 0752AM/15th",
    "This shows it delivered 10/21.",
    "Both shows delivered today",
]

NOT_PLAIN = [
    "This was delivered to our terminal in Dallas 11/2",
    "Delivered to the cross-dock this morning",
    "Delivered to the crossdock 0900AM/22nd",
    "It was delivered to the carrier yesterday",
    "Delivered to a driver 11/3",
    "The truck is out for delivery",
    "Delivered 18 of 20 pallets 0900AM/22nd",
    "Only 18 pallets delivered 11/2",
    "This shows delivered 11/2 but the receiver signed for 18 pallets",
    "Delivered 11/2. 2 pallets remaining on the trailer",
    "Delivered 11/2. Two pallets were left on the dock at origin",
    "Delivered 11/2, short 2 pallets",
    "Partial delivery - delivered 11/2",
    "Delivered 11/2 to the wrong DC",
]


@pytest.mark.parametrize("text", PLAIN)
def test_plain_confirmations_still_read(text):
    assert [k for _l, k in confirmation_lines(text)] == ["delivered"], text


@pytest.mark.parametrize("text", NOT_PLAIN)
def test_hedged_or_partial_lines_are_left_to_a_person(text):
    assert confirmation_lines(text) == [], text


def test_out_for_delivery_holds_back_the_rest_unless_each_line_names_its_po():
    assert confirmation_lines("1220440 shows delivered 11/2. The other one is out "
                              "for delivery") == []
    got = confirmation_lines("064511780513-shows delivered\n1220440-shows delivered\n"
                             "9314683Y-out for delivery")
    assert [l for l, _k in got] == ["064511780513-shows delivered",
                                    "1220440-shows delivered"]
    got = confirmation_lines("064511780513-shows delivered\n"
                             "9314683Y-delivered to our terminal, will deliver Monday")
    assert [l for l, _k in got] == ["064511780513-shows delivered"]
