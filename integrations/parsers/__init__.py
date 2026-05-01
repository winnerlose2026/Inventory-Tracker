"""CSV parsers for SFTP inbox files.

Two file flavors per distributor:

  * inventory  — daily on-hand snapshot per DC. Emits ``on_hand``
                 EmailEvents that overwrite ``quantity`` on each SKU.
  * shipments  — weekly shipment history with PO numbers. Emits
                 ``restock`` EmailEvents that flow through the existing
                 PO revision-replace pipeline.

These are deliberately permissive: we accept either case-insensitive
header variants commonly used by Cheney's CB Direct exports and
US Foods' MOXē exports, and fall back to ignoring rows with missing
required fields rather than failing the whole file. When a real file
arrives whose headers we don't recognize, extend the alias maps below
rather than rewriting the parser.
"""

from .inventory_csv import parse_inventory_csv
from .shipments_csv import parse_shipments_csv

__all__ = ["parse_inventory_csv", "parse_shipments_csv"]
