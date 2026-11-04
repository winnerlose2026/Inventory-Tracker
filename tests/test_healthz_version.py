"""/healthz reports the release version from VERSION."""
from pathlib import Path

from flask import Flask

from blueprints.health import health_bp

ROOT = Path(__file__).resolve().parent.parent


def test_healthz_reports_version():
    app = Flask(__name__)
    app.register_blueprint(health_bp)
    body = app.test_client().get("/healthz").get_json()
    assert body["ok"] is True
    assert body["version"] == (ROOT / "VERSION").read_text().strip()
    assert "commit" in body
