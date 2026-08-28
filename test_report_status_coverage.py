"""Every seeded warehouse must have a covering rep on the status page.

Houston, TX went live in inventory on 2026-08-03 (PO 305202B2 created the
SKUs) and was seeded in seed_bagels.WAREHOUSES, but nobody added it to
report_status.WAREHOUSE_REPS. A warehouse with no reps can never clear a
week -- there is no address to look for a report from -- so it sat
permanently stale on /report-status and in /api/scan/health, training
everyone to ignore the stale list. Assert the two maps agree.
"""

from integrations.report_status import (
    DISTRIBUTOR_OF, WAREHOUSE_REPS,
)
from seed_bagels import WAREHOUSES


def _seeded():
    return {label for whs in WAREHOUSES.values() for label, _tag, _m in whs}


def test_every_seeded_warehouse_has_at_least_one_rep():
    missing = sorted(_seeded() - set(WAREHOUSE_REPS))
    assert not missing, f"seeded but no rep mapped: {missing}"


def test_no_rep_mapping_for_an_unseeded_warehouse():
    extra = sorted(set(WAREHOUSE_REPS) - _seeded())
    assert not extra, f"reps mapped for a warehouse nobody seeds: {extra}"


def test_distributor_of_covers_every_warehouse():
    assert set(DISTRIBUTOR_OF) == set(WAREHOUSE_REPS)
    for label, whs in WAREHOUSES.items():
        for warehouse, _tag, _mult in whs:
            assert DISTRIBUTOR_OF[warehouse] == label, warehouse


def test_every_rep_has_a_usable_address():
    for warehouse, reps in WAREHOUSE_REPS.items():
        assert reps, warehouse
        for rep in reps:
            assert "@" in rep["email"], (warehouse, rep)
            assert rep["email"] == rep["email"].lower(), (warehouse, rep)
            assert rep["name"].strip(), (warehouse, rep)
