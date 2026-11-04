"""Put the repo root on sys.path so tests can import app, core, blueprints,
integrations and scripts the same way they did when they lived at the root."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
