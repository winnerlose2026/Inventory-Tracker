"""Put the repo root on sys.path so tests can import app, core, blueprints,
integrations and scripts the same way they did when they lived at the root."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch):
    """Point every data path at a per-test temp directory.

    inventory_tracker computes DATA_DIR = Path("data") and a dozen *_FILE
    paths from it at import, so any test that forgot to patch them wrote into
    the checkout's own data/ (inventory_audit.json was the usual casualty).
    This repoints DATA_DIR, every module-level *_FILE Path that lives under
    it, and the few modules that keep their own copy of the path. A test that
    patches DATA_DIR / a *_FILE itself still wins: its monkeypatch runs after
    this one.

    Modules that resolve "data/..." relative to the working directory at call
    time (integrations/rep_map.py, planning_config.py, sftp_inbox.py) are
    covered by their own env vars / arguments in the tests that use them.
    """
    import inventory_tracker as it
    from pathlib import Path

    new = tmp_path_factory.mktemp("data")
    old = it.DATA_DIR
    for name, val in list(vars(it).items()):
        if name.endswith("_FILE") and isinstance(val, Path):
            try:
                rel = val.relative_to(old)
            except ValueError:
                continue
            monkeypatch.setattr(it, name, new / rel)
    monkeypatch.setattr(it, "DATA_DIR", new)
    it._FILE_CACHE.clear()
    if hasattr(it, "_reset_boot_state"):
        it._reset_boot_state()

    import sys
    fh = sys.modules.get("core.feed_health")
    if fh is None:
        try:
            import core.feed_health as fh
        except Exception:  # noqa: BLE001
            fh = None
    if fh is not None:
        monkeypatch.setattr(fh, "FEED_HEALTH_FILE", new / "feed_health.json")
    try:
        import integrations.graph_subscriptions as gs
        monkeypatch.setattr(gs, "_DATA_DIR", new)
        monkeypatch.setattr(gs, "_STATE_FILE", new / "graph_subscriptions.json")
    except Exception:  # noqa: BLE001
        pass
    yield new
    it._FILE_CACHE.clear()
