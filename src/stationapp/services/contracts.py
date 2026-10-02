"""External service contracts used by station orchestration.

Concrete LAN/network implementations belong in stationapp.drivers.
"""

from __future__ import annotations

from typing import Protocol

from stationapp.domain.allocation import (
    AllocationDocument,
)
from stationapp.domain.mac import MacPurpose


class MacAllocationGateway(Protocol):
    """LAN MAC-server boundary.

    The central server remains the allocation authority.

    The station requests authorized blocks and stores them locally.
    Production subsequently consumes addresses from the local cache.
    """

    def request_allocation(self, *, station_id: str, jig_id: str, purpose: MacPurpose, minimum_count: int) -> AllocationDocument:
        ...


class ProductionSyncGateway(Protocol):
    """LAN manufacturing-record synchronization boundary."""

    def upload_pending(self, *, station_id: str) -> int:
        """Upload queued records.

        Returns:
            Number of transactions confirmed by the LAN server.
        """
        ...

