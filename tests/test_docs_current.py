"""The operating docs exist, stay short, and keep up with the code."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_operations_doc_is_short_and_covers_jobs_env_alerts_restore():
    text = _read("docs/OPERATIONS.md")
    assert len(text.splitlines()) <= 150
    from integrations import watchdog
    for feed in watchdog.FEED_MAX_AGE_HOURS:
        if feed != "bakery-workbook":
            assert feed in text, feed
    for var in ("INVENTORY_API_TOKEN", "INVENTORY_API_TOKENS", "INVENTORY_ADMINS",
                "FLASK_SECRET_KEY", "SESSION_REQUIRE_PWV", "ALERT_EMAIL_TO",
                "ALERTS_ENABLED", "FORECAST_BACKFILL_DAYS", "INPROCESS_SCHEDULER",
                "DELIVERY_API_TOKEN", "MAX_UPLOAD_MB"):
        assert var in text, var
    for key in ("feed-stale", "books:", "unparsed:", "backup-failed",
                "data-corrupt", "save-refused", "audit-write-failed"):
        assert key in text, key
    assert "/restore" in text and "pre-restore" in text


def test_claude_md_states_the_dev_flow():
    text = _read("CLAUDE.md")
    for needle in ("fresh clone", "Dropbox", "python -m pytest -q",
                   "python scripts/safe_push.py", "One concern per commit",
                   "data/", "/api/feeds/health", "/restore", "72 characters"):
        assert needle in text, needle


def test_stale_handoff_points_at_the_operations_doc():
    text = _read("docs/handoffs/render_scan_endpoint.md")
    assert "OPERATIONS.md" in text and len(text.splitlines()) < 20


def test_shopify_readme_no_longer_says_reads_are_open_or_to_paste_the_master_key():
    text = _read("shopify/README.md")
    assert "are open to anyone" not in text
    assert "paste the value of `INVENTORY_API_TOKEN`" not in text
    assert "Never paste" in text and "reads included" in text


def test_env_example_lists_the_real_variables_with_no_values():
    text = _read(".env.example")
    for var in ("INVENTORY_API_TOKEN", "INVENTORY_API_TOKENS", "INVENTORY_USERNAMES",
                "INVENTORY_PASSWORD", "FLASK_SECRET_KEY", "INVENTORY_ADMINS",
                "DELIVERY_API_TOKEN", "ALERT_EMAIL_TO", "MS365_SEND_AS",
                "TOAST_CLIENT_ID", "SESSION_REQUIRE_PWV"):
        assert var in text, var
    for line in text.splitlines():
        m = re.match(r"^(INVENTORY_API_TOKEN|INVENTORY_PASSWORD|FLASK_SECRET_KEY|"
                     r"MS365_CLIENT_SECRET)=(.*)$", line)
        if m:
            assert m.group(2).strip() == "", line      # placeholders only
