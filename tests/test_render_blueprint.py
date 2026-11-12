"""render.yaml documents what actually runs (it is not applied automatically,
so the test is what keeps it from drifting from the code)."""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def services():
    doc = yaml.safe_load((ROOT / "render.yaml").read_text())
    return {s["name"]: s for s in doc["services"]}


def _keys(svc):
    return {e["key"] for e in svc["envVars"]}


def test_web_service_health_check_and_deploy_mode(services):
    web = services["bagel-inventory"]
    assert web["healthCheckPath"] == "/healthz"
    assert web["autoDeploy"] is False
    assert web["startCommand"] == "gunicorn -c gunicorn.conf.py app:app"
    assert web["disk"]["mountPath"].endswith("/data")


def test_web_service_lists_every_secret_the_code_reads(services):
    web = services["bagel-inventory"]
    needed = {"INVENTORY_API_TOKEN", "INVENTORY_API_TOKENS", "INVENTORY_ADMINS",
              "INVENTORY_USERNAMES", "INVENTORY_PASSWORD", "FLASK_SECRET_KEY",
              "DELIVERY_API_TOKEN", "MS365_SEND_AS", "ALERT_EMAIL_TO",
              "TOAST_CLIENT_ID", "TOAST_CLIENT_SECRET", "TOAST_API_HOSTNAME",
              "TOAST_USER_ACCESS_TYPE"}
    assert needed <= _keys(web)
    secret = {e["key"] for e in web["envVars"] if e.get("sync") is False}
    assert needed <= secret                 # never a literal value in git


def test_lineage_followups_cron_uses_only_the_limited_key(services):
    cron = services["lineage-po-followups"]
    assert cron["type"] == "cron" and cron["schedule"] == "50 14,20 * * 1-5"
    assert cron["startCommand"] == "python scripts/lineage_followups.py"
    assert _keys(cron) == {"APP_URL", "DELIVERY_API_TOKEN"}


def test_header_names_every_in_process_job():
    pytest.importorskip("apscheduler")
    text = (ROOT / "render.yaml").read_text()
    for job in ("forecast-burn", "graph-renew", "production-scan",
                "delivery-sync", "toast-nightly", "backup", "watchdog",
                "heartbeat", "catch-up"):
        assert job in text, job
