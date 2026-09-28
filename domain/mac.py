"""MAC address and MAC lifecycle value objects."""


from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from stationapp.domain.errors import ( InvalidMacAddress, InvalidMacTransition )

_HEX_12 = re.compile(r"^[0-9A-F]{12}$")
_MAX_MAC = 0xFFFFFFFFFFFF

@dataclass(frozen=True, order=True, slots=True)
class MacAddress:
    """Canonical 48-bit MAC address."""
    
    value: int
    
    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise InvalidMacAddress("MAC value must be an integer")
            
        if not 0 <= self.value <= _MAX_MAC:
            raise InvalidMacAddress("MAC value must fit within 48 bits")
            
    @classmethod
    def parse(cls, raw: str | int | "MacAddress") -> "MacAddress":
        if isinstance(raw, cls):
            return raw
        
        if isinstance(raw, bool):
            raise InvalidMacAddress("Boolean is not a MAC address")
            
        if isinstance(raw, int):
            return cls(raw)
            
            
        if not isinstance(raw, str):
            raise InvalidMacAddress(f"Unsupported MAC type: {type(raw).__name__}")
            
        compact = raw.strip().upper()
        
        if compact.startswith("0X"):
            compact = compact[2:]
            
        compact = (compact.replace(":", "").replace("-", "").replace(" ",""))
        
        if not _HEX_12.fullmatch(compact):
            raise InvalidMacAddress(f"Invalid MAC {raw!r}; expected exactly 12 hexadecimal digits")
            
        return cls(int(compact, 16))
        
    @property
    def compact(self) -> str:
        return f"{self.value:012X}"
        
    @property
    def colon(self) -> str:
        value = self.compact
        return ":".join(value[index:index + 2] for index in range(0,12,2))
        
    @property
    def hyphen(self) -> str:
        return self.colon.replace(":","-")
        
    def __str__(self) -> str:
        return self.colon
        
class MacPurpose(str, Enum):
    """Reason an address was allocated"""
    STOCK_RF_TEST = "STOCK_RF_TEST"
    PRICOL_PRODUCTION = "PRICOL_PRODUCTION"
    
class MacStatus(str, Enum):
    """ Local lifecycle of one server-authourized MAC."""
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    PROGRAMMING = "PROGRAMMING"
    ISSUED = "ISSUED"
    CONFIRMED = "CONFIRMED"
    HOLD = "HOLD"
    
_ALLOWED_MAC_TRANSITIONS: dict[MacStatus, frozenset[MacStatus]] = {
    MacStatus.AVAILABLE: frozenset({
        MacStatus.RESERVED,
        MacStatus.HOLD,
    })
    MacStatus.RESERVED: frozenset({
        MacStatus.PROGRAMMING,
        MacStatus.HOLD,
    })
    MacStatus.PROGRAMMING: frozenset({
        MacStatus.ISSUED,
        MacStatus.HOLD,
    })
    MacStatus.ISSUED: frozenset({
        MacStatus.CONFIRMED,
        MacStatus.HOLD,
    })
    MacStatus.CONFIRMED: frozenset()
        MacStatus.HOLD: frozenset(),
}

@dataclass(slots=True)
class AllocatedMac:
    """One MAC Imported from an authourized server allocation."""
    
    allocation_id: str
    address: MacAddress
    purpose: MacPurpose
    status: MacStatus = MacStatus.AVAILABLE
    batch_id: str | None = None
    module_qr: str | None = None
    slot_number: int | None = None
    hold_reason: str | None = None
    
    def reserve(self, *, batch_id: str, module_qr: str, slot_number: int) -> None:
        if not batch_id.strip():
            raise ValueError("batch_id cannot be empty")
            
        if not module_qr.strip():
            raise ValueError("module_qr cannot be empty")
            
        if slot_number <= 0:
            raise ValueError("slot number must be positive")
            
        self._transition(MacStatus.RESERVED)
        self.batch_id = batch_id.strip()
        self.module_qr = module_qr.strip()
        self.slot_number = slot_number
        
    def begin_programming(self) -> None:
        self._transition(MacStatus.PROGRAMMING)
        
    def mark_issued(self) -> None:
        self._transition(MacStatus.ISSUED)
        
    def confirm(self) -> None:
        self._transition(MacStatus.CONFIRMED)
        
    def place_on_hold(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("hold reason cannot be empty")
        
        if self.status is MacStatus.CONFIRMED:
            raise InvalidMacTransition("A confirmed MAC cannot be moved to HOLD automatically")
            
        if self.status is MacStatus.HOLD:
            if self.hold_reason is None:
                self.hold_reason = reason.strip()
            return 
            
        self._transition(MacStatus.HOLD)
        self.hold_reason = reason.strip()
        
    def _transition(self, target: MacStatus) -> None:
        allowed = _ALLOWED_MAC_TRANSITIONS[self.status]
        
        if target not in allowed:
            raise InvalidMacTransition(f"MAC {self.address} cannot transition from {self.status.value} to {target.value}")
            
        self.status = target
        
    