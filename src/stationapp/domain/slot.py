

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
        self._require_module()
        
        identity = port_identity.strip()
        if not identity:
            raise ValueError("port identity cannot be empty")
        self.port_identity = identity
        if self.state is DeviceState.QR_BOUND:
            self.state = DeviceState.PORT_BOUND
            
            
    def assign_stock_mac(self, mac_record: AllocatedMac) -> None:
        self._require_module()
        
        if self.stock_mac_record is not None:
            raise MacAlreadyAssigned(f"Slot {self.number} already has stock MAC {self.stock_mac}")
            
        if mac_record.purpose is not MacPurpose.STOCK_RF_TEST:
            raise ValueError("Expected a STOCK_RF_TEST MAC")
            
        self._validate_reservation_owner(mac_record)
        self.stock_mac_record = mac_record
        self.state = DeviceState.STOCK_MAC_RESERVED
        
    def begin_stock_programming(self) -> None:
        self._require_state(DeviceState.STOCK_MAC_RESERVED)
        
        assert self.stock_mac_record is not None
        self.stock_mac_record.begin_programming()
        self.state = DeviceState.STOCK_PROGRAMMING
        
    def complete_stock_programming(self, succeeded: bool) -> None:
        self._require_state(DeviceState.STOCK_PROGRAMMING)
        assert self.stock_mac_record is not None
        
        if not succeeded:
            self.place_on_hold("Stock programming failed or its MAC outcome is uncertain")
            return 
            
        self.stock_mac_record.mark_issued()
        self.state = DeviceState.STOCK_PROGRAMMED
        
        
    def verify_stock_rf(self, reported: str | MacAddress) -> None:
        self._require_state(DeviceState.STOCK_PROGRAMMED)
        expected = self._require_stock_mac()
        actual = MacAddress.parse(reported)
        self.golden_reported_mac = actual
        
        if actual != expected:
            reason = (f"Golden-rig stock MAC mismatch: expected {expected}, reported {actual}")
            self.place_on_hold(reason)
            raise VerificationMismatch(reason)
            
        assert self.stock_mac_record is not None
        self.stock_mac_record.confirm()
        self.state = DeviceState.STOCK_RF_CONFIRMED
        
        
    def assign_pricol_mac(self, mac_record: AllocatedMac) -> None:
        self._require_state(DeviceState.STOCK_RF_CONFIRMED)
        
        if self.pricol_mac_record is not None:
            raise MacAlreadyAssigned(f"Slot {self.number} already has Pricol MAC {self.pricol_mac}")
            
        if mac_record.purpose is not MacPurpose.PRICOL_PRODUCTION:
            raise ValueError("Expected a PRICOL_PRODUCTION MAC")
            
        self._validate_reservation_owner(mac_record)
        
        if mac_record.address == self.stock_mac:
            reason = (f"Pricol MAC {mac_record.address} equals the temporary stock MAC for slot {self.number}")
            self.place_on_hold(reason)
            raise VerificationMismatch(reason)
            
        self.pricol_mac_record = mac_record
        self.state = DeviceState.PRICOL_MAC_RESERVED
        
    def begin_pricol_programming(self) -> None:
        self._require_state(DeviceState.PRICOL_MAC_RESERVED)
        
        assert self.pricol_mac_record is not None
        self.pricol_mac_record.begin_programming()
        self.state = DeviceState.PRICOL_PROGRAMMING
        
    def complete_pricol_programming(self, succeeded: bool) -> None:
        self._require_state(DeviceState.PRICOL_PROGRAMMING)
        assert self.pricol_mac_record is not None
        
        if not succeeded:
            self.place_on_hold("Pricol programming failed or its MAC outcome is uncertain")
            return 
            
        self.pricol_mac_record.mark_issued()
        self.state = DeviceState.PRICOL_PROGRAMMED
        
    def verify_pricol_readback(self, reported: str | MacAddress) -> None:
        """ Verify Pricol MAC using MP CLI read-back.
        No golden-rig RF test occurs after pricol programming. 
        """
        self._require_state(DeviceState.PRICOL_PROGRAMMED)
        expected = self._require_pricol_mac()
        actual = MacAddress.parse(reported)
        self.pricol_readback_mac = actual
        if actual != expected:
            reason = (f"Pricol MAC read-back mismatch: expected {expected}, reported {actual}")
            self.place_on_hold(reason)
            raise VerificationMismatch(reason)
            
        assert self.pricol_mac_record is not None
        self.pricol_mac_record.confirm()
        self.state = DeviceState.PRICOL_MAC_CONFIRMED
        
    def record_pricol_app(self, passed: bool) -> None:
        self._require_state(DeviceState.PRICOL_MAC_CONFIRMED)
        self.functional.pricol_app_passed = passed
        
    def record_pricol_dfu(self, passed: bool) -> None:
        self._require_state(DeviceState.PRICOL_MAC_CONFIRMED)
        
        if self.functional.pricol_app_passed is None:
            raise InvalidDeviceTransition("PRICOLAPP result must be recorded before PRICOLDFU")
            
        self.functional.pricol_dfu_passed = passed
        
        self.state = (DeviceState.FUNCTIONAL_TEST_PASSED if self.functional.passed else DeviceState.FUNCTIONAL_TEST_FAILED)
        
    def place_on_hold(self, reason: str) -> None:
        text = reason.strip()
        if not text:
            raise ValueError("hold reason cannot be empty")
            
        # The currently active, non confirmed address is quarantined.
        for mac_record in ( self.stock_mac_record, self.pricol_mac_record):
            if ( mac_record is not None and mac_record.status not in { MacStatus.CONFIRMED, MacStatus.HOLD}):
                mac_record.place_on_hold(text)
        self.hold_reason = text
        self.state = DeviceState.HOLD
        
    def _validate_reservation_owner(self, mac_record: AllocatedMac) -> None:
        self._require_module()
        
        if mac_record.status is not MacStatus.RESERVED:
            raise InvalidDeviceTransition(f"MAC {mac_record.address} must be RESERVED before assignment")
            
        if mac_record.module_qr != self.module_qr:
            raise InvalidDeviceTransition(f"MAC {mac_record.address} was reserved for {mac_record.module_qr}, not {self.module_qr}")
            
        if mac_record.slot_number != self.number:
            raise InvalidDeviceTransition(f"MAC {mac_record.address} was reserved for slot {mac_record.slot_number}, not {self.number}")
            
            
    def _require_module(self) -> None:
        if self.module_qr is None:
            raise SlotNotBound(f"Slot {self.number} has no Module QR")
            
    def _require_state(self, expected: DeviceState) -> None:
        if self.state is not expected:
            raise InvalidDeviceTransition(f"Slot {self.number} must be {expected.value}; current state is {self.state.value}")
            
    def _require_stock_mac(self) -> MacAddress:
        if self.stock_mac is None:
            raise InvalidDeviceTransition(f"Slot {self.number} has no stock MAC")
        return self.stock_mac
        
        
    def _require_pricol_mac(self) -> MacAddress:
        if self.pricol_mac is None:
            raise InvalidDeviceTransition(f"Slot {self.number} has no Pricol MAC")
        return self.pricol_mac
        
        
        