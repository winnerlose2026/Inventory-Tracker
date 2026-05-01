#!/usr/bin/env python3
"""Cron entry point: pull distributor CSVs from the FTPS inbox and
post them to the live web service.

Designed to run on a Render Cron Job (every 15 minutes is fine; the
cursor at data/sftp_cursor.json prevents re-ingesting files we've
already processed). Exit code 0 if nothing failed, 1 if any account
hit a connect/parse/post error.

Required env vars (web service & cron share them):
  SFTP_HOST, SFTP_USERNAME_USFOODS, SFTP_PASSWORD_USFOODS,
  SFTP_USERNAME_CHENEY, SFTP_PASSWORD_CHENEY,
  APP_URL, INVENTORY_API_TOKEN.
Optional:
  SFTP_PORT (default 21), SFTP_INCOMING_DIR (default 'incoming'),
  SFTP_PROCESSED_DIR (default 'processed'),
  SFTP_CURSOR_PATH (default data/sftp_cursor.json),
  SFTP_DRY_RUN ("1" to skip writes & cursor updates).
"""

import json
import os
import sys


def main() -> int:
    # Make the package importable when running via `python scripts/...`
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(here))

    from integrations.sftp_inbox import pull_all  # noqa: E402

    dry_run = os.environ.get("SFTP_DRY_RUN", "").strip() == "1"
    reports = pull_all(dry_run=dry_run)

    failed = 0
    for r in reports:
        summary = {
            "account": r.account,
            "distributor": r.distributor,
            "files_seen": r.files_seen,
            "files_skipped": r.files_skipped,
            "files_ingested": r.files_ingested,
            "files_failed": r.files_failed,
            "errors": r.errors,
            "server_reports": r.server_reports,
        }
        print(json.dumps(summary, indent=2, default=str))
        if r.files_failed or r.errors:
            failed += 1

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
