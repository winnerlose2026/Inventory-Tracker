"""Cheney Brothers inventory sync client.

Cheney Brothers does not publish a public HTTP API for customer inventory.
Real-time stock typically comes from one of:

  - CB Direct EDI integration (846 Inventory Inquiry/Advice) — arranged
    through your Cheney account rep; credentials are an AS2/SFTP endpoint.
  - A scheduled CSV/Excel export from the CB Direct ordering portal.

Set the following env vars to enable authenticated sync. If they are
missing this client falls back to integrations/cheney_brothers_inventory.csv,
which you can populate by exporting inventory from the CB Direct portal:

  CHENEY_API_URL        base URL for your Cheney data feed
  CHENEY_API_KEY        API key / bearer token
  CHENEY_CUSTOMER_ID    your Cheney customer account number
"""

import os
from typing import Iterable

from .base import DistributorClient, NotConfiguredError, SyncItem
from .csv_loader import read_csv


class CheneyBrothersClient(DistributorClient):
    name = "Cheney Brothers"

    def _has_live_credentials(self) -> bool:
        return bool(
            os.environ.get("CHENEY_API_URL")
            and os.environ.get("CHENEY_API_KEY")
            and os.environ.get("CHENEY_CUSTOMER_ID")
        )

    def fetch_inventory(self) -> Iterable[SyncItem]:
        if self._has_live_credentials():
            return self._fetch_live()

        path = self.csv_path()
        if path.exists():
            return list(read_csv(path, distributor=self.name))

        raise NotConfiguredError(
            "Cheney Brothers sync is not configured. Either set "
            "CHENEY_API_URL / CHENEY_API_KEY / CHENEY_CUSTOMER_ID, or drop an "
            f"export from CB Direct at {path}. See "
            "integrations/examples/cheney_brothers_inventory.example.csv "
            "for the expected format."
        )

    def _fetch_live(self):
        # TODO: implement once the Cheney integration contract is in place.
        # The endpoint/auth scheme is specific to each customer's agreement,
        # so we leave the concrete HTTP call as a stub to be filled in when
        # credentials and the data-feed spec are provided.
        raise NotConfiguredError(
            "Cheney Brothers live API client is stubbed. Fill in "
            "integrations/cheney.py::_fetch_live() against your Cheney data "
            "feed (EDI 846 via AS2/SFTP, or a REST endpoint if arranged)."
        )
