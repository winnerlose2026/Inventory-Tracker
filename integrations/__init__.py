"""Distributor inventory sync integrations (Cheney Brothers, US Foods)."""

from .base import DistributorClient, NotConfiguredError, SyncItem
from .cheney import CheneyBrothersClient
from .usfoods import USFoodsClient

__all__ = [
    "DistributorClient",
    "NotConfiguredError",
    "SyncItem",
    "CheneyBrothersClient",
    "USFoodsClient",
    "all_clients",
]


def all_clients() -> list[DistributorClient]:
    return [CheneyBrothersClient(), USFoodsClient()]
