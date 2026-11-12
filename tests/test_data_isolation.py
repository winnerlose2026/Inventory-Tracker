"""The autouse fixture in conftest.py keeps pytest out of the checkout's data/."""
from pathlib import Path

import inventory_tracker as it


def test_data_paths_point_at_a_temp_dir(_isolated_data_dir):
    assert it.DATA_DIR == _isolated_data_dir
    assert it.DATA_DIR != Path("data")
    for name, val in vars(it).items():
        if name.endswith("_FILE") and isinstance(val, Path):
            assert _isolated_data_dir in val.parents, name


def test_a_write_lands_in_the_temp_dir(_isolated_data_dir):
    it.append_inventory_audit([{"ts": "x", "name": "t"}])
    assert (_isolated_data_dir / "inventory_audit.json").exists()
    assert not (Path(__file__).resolve().parent.parent / "data"
                / "inventory_audit.json").exists()
