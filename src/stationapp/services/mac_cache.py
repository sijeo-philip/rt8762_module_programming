"""Authorized MAC cache check and allocation import (workflow steps 5-6).

The LAN server is the allocation authority. The station keeps a local
cache of server-authorized MAC blocks so production continues while the
server is unavailable. Before a batch can become READY the cache must
hold enough Stock and Pricol MACs for a full jig load.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.production_repository import ProductionStateRepository
from stationapp.data.repositories import AllocationRepository
from stationapp.domain.allocation import AllocationDocument
from stationapp.domain.mac import MacPurpose
from stationapp.services.contracts import MacAllocationGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MacCacheStatus:
    purpose: MacPurpose
    available: int
    required: int
    imported: int = 0
    message: str = ""

    @property
    def sufficient(self) -> bool:
        return self.available >= self.required


def allocation_digest(document: AllocationDocument) -> str:
    """Stable digest making re-import of the same document idempotent."""
    canonical = {
        "allocation_id": document.allocation_id,
        "station_id": document.station_id,
        "jig_id": document.jig_id,
        "purpose": document.purpose.value,
        "issued_at": document.issued_at.isoformat(),
        "expires_at": document.expires_at.isoformat() if document.expires_at else None,
        "schema_version": document.schema_version,
        "addresses": [address.compact for address in document.addresses],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MacCacheService:
    """Keep the local authorized MAC cache above its threshold."""

    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, jig_id: str, jig_positions: int, threshold: int, gateway: MacAllocationGateway | None = None, request_size: int = 200) -> None:
        if threshold < jig_positions:
            raise ValueError("MAC cache threshold must cover at least one full jig load")
        if request_size <= 0:
            raise ValueError("request_size must be positive")

        self._session_factory = session_factory
        self._allocations = AllocationRepository(session_factory)
        self._station_id = station_id
        self._jig_id = jig_id
        self._jig_positions = jig_positions
        self._threshold = threshold
        self._gateway = gateway
        self._request_size = request_size

    @property
    def online(self) -> bool:
        return self._gateway is not None

    def available(self, purpose: MacPurpose) -> int:
        with self._session_factory() as session:
            return ProductionStateRepository(session).count_available(purpose)

    def ensure(self, purpose: MacPurpose) -> MacCacheStatus:
        """Top up the cache if it is below threshold and a server exists.

        Never raises for a server problem: production may continue on the
        existing cache as long as one full jig load remains.
        """
        available = self.available(purpose)
        imported = 0
        message = f"{available} authorized {purpose.value} MACs cached"

        if available < self._threshold:
            if self._gateway is None:
                message += f"; below threshold {self._threshold} and no LAN server configured"
            else:
                try:
                    imported = self._import_from_server(purpose)
                    available = self.available(purpose)
                    message = f"Imported {imported} MACs; {available} {purpose.value} MACs cached"
                except Exception as exc:  # noqa: BLE001 - reported, cache may still suffice
                    logger.exception("MAC allocation request failed | purpose=%s", purpose.value)
                    message += f"; allocation request failed: {exc}"

        return MacCacheStatus(
            purpose=purpose,
            available=available,
            required=self._jig_positions,
            imported=imported,
            message=message,
        )

    def _import_from_server(self, purpose: MacPurpose) -> int:
        assert self._gateway is not None
        document = self._gateway.request_allocation(
            station_id=self._station_id,
            jig_id=self._jig_id,
            purpose=purpose,
            minimum_count=self._request_size,
        )

        # Only a block issued to this station/jig/purpose and still valid
        # may enter the cache.
        document.validate_for_station(
            station_id=self._station_id,
            jig_id=self._jig_id,
            purpose=purpose,
        )

        inserted = self._allocations.import_allocation(
            document,
            document_digest=allocation_digest(document),
        )

        count = len(document.addresses) if inserted else 0
        logger.info(
            "MAC allocation import | allocation_id=%s purpose=%s imported=%d",
            document.allocation_id,
            purpose.value,
            count,
        )
        return count
