"""US Foods inventory sync client.

US Foods does not offer a public customer inventory API. Real-time stock
for a specific customer/warehouse is typically obtained through one of:

  - An EDI 846 Inventory Advice feed over AS2 or SFTP, arranged with your
    US Foods rep or through the US Foods EDI onboarding process.
  - A scheduled inventory export from US Foods Online / MOXē.

Set the following env vars to enable authenticated sync. If they are
missing this client falls back to integrations/us_foods_inventory.csv,
which you can populate by exporting from US Foods Online:

  USFOODS_API_URL        base URL for your US Foods data feed
  USFOODS_API_KEY        API key / bearer token
  USFOODS_ACCOUNT_ID     your US Foods customer account number
"""

import os
from typing import Iterable

from .base import DistributorClient, NotConfiguredError, SyncItem
from .csv_loader import read_csv


class USFoodsClient(DistributorClient):
    name = "US Foods"

    def _has_live_credentials(self) -> bool:
        return bool(
            os.environ.get("USFOODS_API_URL")
            and os.environ.get("USFOODS_API_KEY")
            and os.environ.get("USFOODS_ACCOUNT_ID")
        )

    def fetch_inventory(self) -> Iterable[SyncItem]:
        if self._has_live_credentials():
            return self._fetch_live()

        path = self.csv_path()
        if path.exists():
            return list(read_csv(path, distributor=self.name))

        raise NotConfiguredError(
            "US Foods sync is not configured. Either set "
            "USFOODS_API_URL / USFOODS_API_KEY / USFOODS_ACCOUNT_ID, or drop "
            f"an export from US Foods Online at {path}. See "
            "integrations/examples/us_foods_inventory.example.csv "
            "for the expected format."
        )

    def _fetch_live(self):
        # TODO: implement once the US Foods integration contract is in place.
        # Typical flow is EDI 846 → parsed into SyncItem rows. Leaving this as
        # a stub so credentials can be added without any speculative network
        # calls against an undocumented endpoint.
        raise NotConfiguredError(
            "US Foods live API client is stubbed. Fill in "
            "integrations/usfoods.py::_fetch_live() against your US Foods "
            "data feed (EDI 846 via AS2/SFTP is the standard path)."
        )
