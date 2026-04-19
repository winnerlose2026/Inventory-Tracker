"""Base types for distributor inventory sync clients."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


class NotConfiguredError(RuntimeError):
    """Raised when required credentials or data files are missing."""


@dataclass
class SyncItem:
    """A single row pulled from a distributor's inventory feed.

    Matching against our local inventory is done in priority order:
      1. `name` — exact match against our local item key (name.lower())
      2. (`variety`, `distributor`, `warehouse`) — reconstructed name using
         the same convention as seed_bagels.py
    """
    quantity: float
    distributor: str
    name: Optional[str] = None
    variety: Optional[str] = None
    warehouse: Optional[str] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    distributor_sku: Optional[str] = None  # informational
    case_cost: Optional[float] = None
    case_size: Optional[int] = None
    weekly_usage: Optional[float] = None


class DistributorClient:
    """Abstract distributor client. Subclasses implement fetch_inventory()."""

    name: str = "Distributor"

    def fetch_inventory(self) -> Iterable[SyncItem]:
        """Return current on-hand quantities (and prices) from the distributor.

        Concrete clients try the authenticated API first; if creds are missing
        they fall back to a CSV at integrations/<slug>_inventory.csv.
        """
        raise NotImplementedError

    def csv_path(self) -> Path:
        slug = self.name.lower().replace(" ", "_")
        return Path(__file__).parent / f"{slug}_inventory.csv"
