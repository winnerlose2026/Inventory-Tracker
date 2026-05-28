#!/usr/bin/env python3
"""One-shot ingester for a folder of Lineage Freight .msg files.

Use this to backfill freight_invoices.json from .msg files you've
already downloaded (e.g. the batch JD attached when first standing up
the Freight Costs tab). The normal path is the 6h mailbox cron --
this is just for the initial backfill.

Usage:
    python scripts/ingest_lineage_msg_folder.py <folder> \\
        --app-url https://bagel-inventory.onrender.com \\
        --api-token $INVENTORY_API_TOKEN \\
        [--dry-run] [--verbose]

Requires extract-msg (`pip install extract-msg`). The Lineage email
attachment is a .zip containing one or more PDFs; each PDF becomes
one freight_invoices.json record.
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from integrations.lineage_freight_parser import parse_freight_pdf  # noqa: E402

try:
    import extract_msg  # type: ignore
except ImportError:
    print("ERROR: extract-msg is required. Install with: "
          "pip install extract-msg", file=sys.stderr)
    sys.exit(2)


def _walk_msg(folder: Path) -> list[dict]:
    """Walk every .msg file in ``folder`` and return a list of parsed
    FreightInvoice dicts."""
    records: list[dict] = []
    msg_paths = sorted(glob.glob(str(folder / "*.msg")) +
                       glob.glob(str(folder / "**/*.msg"), recursive=True))
    for path in msg_paths:
        try:
            m = extract_msg.openMsg(path)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {path}: openMsg failed: {exc}", file=sys.stderr)
            continue
        subj = (m.subject or "").upper()
        if "LINEAGE FREIGHT" not in subj:
            continue
        msg_id = (m.messageId or "") or path
        for att in (m.attachments or []):
            name = (att.longFilename or att.shortFilename or "")
            data = att.data or b""
            if not data:
                continue
            pdfs: list[tuple[str, bytes]] = []
            if name.lower().endswith(".zip"):
                try:
                    zf = zipfile.ZipFile(io.BytesIO(data))
                    for info in zf.infolist():
                        if info.filename.lower().endswith(".pdf"):
                            pdfs.append((info.filename, zf.read(info.filename)))
                except zipfile.BadZipFile as exc:
                    print(f"  bad zip in {path}/{name}: {exc}", file=sys.stderr)
                    continue
            elif name.lower().endswith(".pdf"):
                pdfs.append((name, data))
            for fname, pb in pdfs:
                try:
                    inv = parse_freight_pdf(
                        pb, pdf_filename=fname,
                        source_message_id=msg_id,
                        source_subject=m.subject or "",
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  parse failed {path}/{fname}: {exc}",
                          file=sys.stderr)
                    continue
                if inv is None:
                    print(f"  not a freight invoice: {path}/{fname}",
                          file=sys.stderr)
                    continue
                records.append(asdict(inv))
    return records


def _post(app_url: str, token: str, records: list[dict],
          *, dry_run: bool = False) -> dict:
    body = json.dumps({
        "dry_run": dry_run,
        "source": "lineage-msg-backfill",
        "invoices": records,
    }).encode("utf-8")
    url = app_url.rstrip("/") + "/api/freight/ingest"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={
                                     "Content-Type": "application/json",
                                     "X-Inventory-Token": token,
                                     "Authorization": f"Bearer {token}",
                                 })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", help="Folder containing Lineage .msg files")
    ap.add_argument("--app-url",
                    default=os.environ.get("APP_URL", ""),
                    help="Base URL of the Render web service")
    ap.add_argument("--api-token",
                    default=os.environ.get("INVENTORY_API_TOKEN", ""),
                    help="INVENTORY_API_TOKEN for the service")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse only — do not POST")
    ap.add_argument("--print-records", action="store_true",
                    help="Print parsed records to stdout instead of POSTing")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        print(f"ERROR: {folder} is not a directory", file=sys.stderr)
        return 2

    records = _walk_msg(folder)
    print(f"Parsed {len(records)} freight invoices from {folder}", file=sys.stderr)
    if not records:
        return 0

    if args.print_records:
        print(json.dumps(records, indent=2))
        return 0

    if not args.app_url or not args.api_token:
        print("ERROR: --app-url and --api-token are required (or set "
              "APP_URL / INVENTORY_API_TOKEN).", file=sys.stderr)
        return 2

    result = _post(args.app_url, args.api_token, records,
                   dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
