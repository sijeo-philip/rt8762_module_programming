

"""Server-issued MAC allocation documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from stationapp.domain.errors import ( AllocationExpired, AllocationOwnershipMismatch, AllocationPurposeMismatch, DuplicateMacAddress, InvalidAllocation)

from stationapp.domain.mac import (AllocatedMac, MacAddress, MacPurpose)

@dataclass(frozen=True, slots=True)
class AllocationDocument:
    """Immutable allocation authourized by the local server.
    
    Cryptographic signature bytes and encrypted-file details do not belong
    in this value object. The storage/verification driver handles those and 
    constructs this object only after verification succeeds.
    """
    
    allocation_id: str
    station_id: str
    jig_id: str
    purpose: MacPurpose
    issued_at: datetime
    addresses: tuple[MacAddress, ...]
    schema_version: int = 1
    expires_at: datetime | None = None
    
    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise InvalidAllocation(f"Unsupported allocation schema version {self.schema_version}")
            
        if not self.allocation_id.strip():
            raise InvalidAllocation("allocation_id cannot be empty")
            
        if not self.station_id.strip():
            raise InvalidAllocation(f"station_id cannot be empty")
            
        if not self.jig_id.strip():
            raise InvalidAllocation("jig_id cannot be empty")
            
        if self.issued_at.tzinfo is None:
            raise InvalidAllocation("issued_at must be timezone-aware")
            
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise InvalidAllocation("expires_at must be timezone-awre")
                
            if self.expires_at <= self.issued_at:
                raise InvalidAllocation("expires_at must be later than issued_at")
                
        if not self.addresses:
            raise InvalidAllocation("allocation must contain at least one MAC address")
            
        if len(set(self.addresses)) != len(self.addresses):
            raise DuplicateMacAddress(f"Allocation {self.allocation_id} contains duplicate MACs")
            
    def validate_for_station(self, *, station_id: str, jig_id: str, purpose: MacPurpose, now: datetime | None = None) -> None:
        if self.station_id != station_id or self.jig_id != jig_id:
            raise AllocationOwnershipMismatch(f"Allocation {self.allocation_id} belongs to {self.station_id}/{self.jig_id}, not {station_id}/{jig_id}")
            
        if self.purpose is not purpose:
            raise AllocationPurposeMismatch(f"Allocation {self.allocation_id} is for {self.purpose.value}, not {purpose.value}")
            
        current = now or datetime.now(timezone.utc)
        
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
            
        if self.expires_at is not None and current >= self.expires_at:
            raise AllocationExpired(f"Allocation {self.allocation_id} expired at {self.expires_at.isoformat()}")
            
    def to_allocated_macs(self) -> tuple[AllocatedMac, ...]:
        """Produce local lifecycle records for first-time database import."""
        return tuple(
            AllocatedMac(
                allocation_id=self.allocation_id,
                address=address,
                purpose=self.purpose,
            )
            for address in self.addresses
        )
        
        