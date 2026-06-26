#!/usr/bin/env python3
"""Tests for the planner reference config (Phase 1). pytest or standalone."""
import sys, os, json, tempfile
sys.path.insert(0, ".")
import integrations.planning_config as pc


def test_defaults_shape():
    c = pc.load_planning_config()
    assert len(c["warehouses"]) == 8
    assert len(c["varieties"]) == 12
    assert c["top4"] == ["Plain", "Everything", "Sesame", "Cinnamon Raisin"]
    assert c["freezer_pallet_cap"] == 110
    assert c["capacity"]["dependable_cs_per_day"] == 224
    assert c["capacity"]["max_cs_per_day"] == 280


def test_top4_helper():
    assert pc.is_top4("Plain") and pc.is_top4("Cinnamon Raisin")
    assert not pc.is_top4("Onion") and not pc.is_top4("")


def test_cheney_transfer_pool():
    # the modeling fact: Cheney FL warehouses pool/transfer
    assert pc.transfer_group_for("Punta Gorda, FL") == "cheney-fl"
    assert pc.transfer_group_for("Riviera Beach, FL") == "cheney-fl"
    assert pc.transfer_group_for("Ocala, FL") == "cheney-fl"
    assert pc.transfer_group_for("Zebulon, NC") is None          # USF DCs are standalone
    assert sorted(pc.pool_members("cheney-fl")) == ["Ocala, FL", "Punta Gorda, FL", "Riviera Beach, FL"]


def test_transit_days_default():
    assert pc.transit_days_for("Zebulon, NC") == 7
    assert pc.transit_days_for("Unknown Place") == 7


def test_disk_override_merges_and_failsafe():
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd)
    try:
        # deep-merge: override one nested key, keep siblings
        json.dump({"transit_default_days": 9,
                   "warehouse_buffer_days": {"top4_target": 21}}, open(path, "w"))
        os.environ["PLANNING_CONFIG_FILE"] = path
        pc._CACHE["key"] = None
        c = pc.load_planning_config()
        assert c["transit_default_days"] == 9
        assert c["warehouse_buffer_days"]["top4_target"] == 21       # overridden
        assert c["warehouse_buffer_days"]["top4_floor"] == 7         # sibling preserved
        # malformed file -> fail safe to DEFAULTS
        open(path, "w").write("{ not json")
        pc._CACHE["key"] = None
        c2 = pc.load_planning_config()
        assert c2["transit_default_days"] == 7
    finally:
        os.environ.pop("PLANNING_CONFIG_FILE", None)
        pc._CACHE["key"] = None
        os.unlink(path)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn(); print("ok:", fn.__name__)
        except Exception as e:  # noqa: BLE001
            bad += 1; print("FAIL:", fn.__name__, e)
    print(f"\n{len(fns)-bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
