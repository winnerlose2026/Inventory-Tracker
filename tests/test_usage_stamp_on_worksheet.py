"""A worksheet count that carries weekly usage stamps last_usage_report_at.

Suggested Orders judges usage age from that stamp alone (a fresh count used
to mask a months-old usage figure). The stamp was only written by pure
usage_rate events, so every DC whose usage arrives on its inventory worksheet
would have read as "no usage report on file"."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_onhand_guards import _setup_temp_inventory, _seed, KEY_PLAIN_OCALA  # noqa: E402


def _evt(qty, wu, count_date, source_id):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=float(qty), distributor="Cheney Brothers", variety="Plain",
                      warehouse="Ocala, FL", unit="cs", weekly_usage=wu),
        source_message_id=source_id, source_subject="Cheney inventory & usage",
        count_date=count_date, source_received_at="")


def test_worksheet_with_usage_stamps_the_usage_date_and_one_without_does_not():
    with TemporaryDirectory() as td:
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        sync._apply_events([_evt(40, 21.0, "2026-10-05", "ws:wk40#1")], dry_run=False)
        item = it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]
        assert item["weekly_usage"] == 21.0
        assert item["last_usage_report_at"] == "2026-10-05"
        # next week's worksheet lost its usage column: the count moves on,
        # the usage date does not -- which is what makes it show as old.
        sync._apply_events([_evt(33, None, "2026-10-12", "ws:wk41#1")], dry_run=False)
        item = it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]
        assert item["quantity"] == 33 and item["last_count_at"][:10] == "2026-10-12"
        assert item["last_usage_report_at"] == "2026-10-05" and item["weekly_usage"] == 21.0
