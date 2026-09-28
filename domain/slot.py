

"""Physical jig-slot and per-module production state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from stationapp.domain.errors import (
    InvalidDeviceTransition,
    MacAlreadyAssigned,
    SlotAlreadyOccupied,
    SlotNotBound,
    VerificationMismatch,
)
from stationapp.domain.mac import (
    AllocatedMac,
    MacAddress,
    MacPurpose,
    MacStatus,
)

class DeviceState(str, Enum):
    EMPTY = "EMPTY"
    QR_BOUND = "QR_BOUND"
    PORT_BOUND = "PORT_BOUND"
    
    STOCK_MAC_RESERVED = "STOCK_MAC_RESERVED"
    STOCK_PROGRAMMING = "STOCK_PROGRAMMING"
    STOCK_PROGRAMMED = "STOCK_PROGRAMMED"
    STOCK_RF_CONFIRMED = "STOCK_RF_CONFIRMED"
    
    PRICOL_MAC_RESERVED = "PRICOL_MAC_RESERVED"
    PRICOL_PROGRAMMING = "PRICOL_PROGRAMMING"
    PRICOL_PROGRAMMED = "PRICOL_PROGRAMMED"
    PRICOL_MAC_CONFIRMED = "PRICOL_MAC_CONFIRMED"
    
    FUNCTIONAL_TEST_PASSED = "FUNCTIONAL_TEST_PASSED"
    FUNCTIONAL_TEST_FAILED = "FUNCTIONAL_TEST_FAILED"
    
    HOLD = "HOLD"
    
@dataclass(slots=True)
class FunctionalResults:
    pricol_app_passed: bool | None = None
    pricol_dfu_passed: bool | None = None
    
    @property
    def complete(self) -> bool:
        return(self.pricol_app_passed is not None and self.pricol_dfu_passed is not None)
        
    @property
    def passed(self) -> bool:
        return(self.pricol_app_passed is True and self.pricol_dfu_passed is True)
        
        
@dataclass(slots=True)
class JigSlot:
    """One physical module position in a 4-up and 8-up jig."""
    
    number: int
    module_qr: str | None = None
    
    # Stable USB identity,  not an unstable COM Number.
    port_identity: str | None = None
    
    stock_mac_record: AllocatedMac | None = None
    pricol_mac_record: AllocatedMac | None = None
    
    golden_reported_mac: MacAddress | None = None
    pricol_readback_mac: MacAddress | None = None
    
    functional: FunctionalResults = field(default_factory = FunctionalResults)
    state: DeviceState = DeviceState.EMPTY
    hold_reason: str | None = None
    
    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ValueError("slot number must be positive")
            
    @property
    def stock_mac(self) -> MacAddress | None:
        if self.stock_mac_record is None:
            return None
        return self.stock_mac_record.address
        
    @property
    def pricol_mac(self) -> MacAddress | None:
        if self.pricol_mac_record is None:
            return None
        return self.pricol_mac_record.address
        
    @property
    def is_held(self) -> bool:
        return self.state is DeviceState.HOLD
        
    def bind_module(self, module_qr: str) -> None:
        qr = module_qr.strip()
        
        if not qr: 
            raise ValueError("module QR cannot be empty")
            
        if self.module_qr is not None and self.module_qr != qr:
            raise SlotAlreadyOccupied(f"Slot {self.number} already contains {self.module_qr}")
            
        self.module_qr = qr
        self.state = DeviceState.QR_BOUND
        
    def bind_port(self, port_identity: str) -> None:
        