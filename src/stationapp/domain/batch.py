

""" Production batch aggregate and manufacturing workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from stationapp.domain.errors import (
    BatchOnHold,
    DuplicateMacAddress,
    DuplicateModuleQr,
    InvalidBatchTransition,
    VerificationMismatch,
)

from stationapp.domain.mac import AllocatedMac, MacPurpose, MacStatus
from stationapp.domain.slot import DeviceState, JigSlot

class BatchState(str, Enum):
    CREATED = "CREATED"

    PORT_BINDING = "PORT_BINDING"
    PORTS_BOUND = "PORTS_BOUND"

    STOCK_MACS_RESERVED = "STOCK_MACS_RESERVED"
    AWAITING_STOCK_PROGRAM_MODE = "AWAITING_STOCK_PROGRAM_MODE"
    STOCK_PROGRAMMING = "STOCK_PROGRAMMING"
    STOCK_PROGRAMMED = "STOCK_PROGRAMMED"

    AWAITING_STOCK_RF_MODE = "AWAITING_STOCK_RF_MODE"
    STOCK_RF_TESTING = "STOCK_RF_TESTING"
    STOCK_RF_CONFIRMED = "STOCK_RF_CONFIRMED"

    PRICOL_MACS_RESERVED = "PRICOL_MACS_RESERVED"
    AWAITING_PRICOL_PROGRAM_MODE = "AWAITING_PRICOL_PROGRAM_MODE"
    PRICOL_PROGRAMMING = "PRICOL_PROGRAMMING"
    PRICOL_PROGRAMMED = "PRICOL_PROGRAMMED"
    PRICOL_READBACK_VERIFIED = "PRICOL_READBACK_VERIFIED"

    AWAITING_FUNCTIONAL_TEST_MODE = "AWAITING_FUNCTIONAL_TEST_MODE"
    AWAITING_PRICOLAPP_RESET = "AWAITING_PRICOLAPP_RESET"
    PRICOLAPP_TESTING = "PRICOLAPP_TESTING"
    AWAITING_PRICOLDFU_RESET = "AWAITING_PRICOLDFU_RESET"
    PRICOLDFU_TESTING = "PRICOLDFU_TESTING"
    FUNCTIONAL_TEST_COMPLETE = "FUNCTIONAL_TEST_COMPLETE"

    QR_SCANNING = "QR_SCANNING"
    QR_BOUND = "QR_BOUND"

    COMMITTED = "COMMITTED"
    UPLOAD_PENDING = "UPLOAD_PENDING"
    UPLOADED = "UPLOADED"

    HOLD = "HOLD"
    ABORTED = "ABORTED"

    
    
@dataclass(slots=True)
class Batch:
    """One complete 4-up or 8-up production cycle."""
    station_id: str
    jig_id: str
    slot_count: int
    
    batch_id: str = field(default_factory=lambda: str(uuid4()))
    state: BatchState = BatchState.CREATED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    slots: dict[int, JigSlot] = field(init=False)
    hold_reason: str | None = None
    
    def __post_init__(self) -> None:
        if self.slot_count not in (4, 8):
            raise ValueError("slot_count must be 4 or 8")
            
        if not self.station_id.strip():
            raise ValueError("station_id cannot be empty")
            
        if not self.jig_id.strip():
            raise ValueError("jig_id cannot be empty")
            
        if not self.batch_id.strip():
            raise ValueError("batch_id cannot be empty")
            
        self.slots = { number: JigSlot(number) for number in range(1, self.slot_count + 1) }
        
    @property
    def ordered_slots(self) -> tuple[JigSlot, ...]:
        return tuple(self.slots[number] for number in sorted(self.slots))
        
        
    @property
    def all_ports_bound(self) -> bool:
        return all(slot.port_identity is not None for slot in self.ordered_slots)
        
    @property
    def any_slot_held(self) -> bool:
        return any(slot.is_held for slot in self.ordered_slots)
        
    @property
    def can_commit(self) -> bool:
        """A batch is committable only after all post-test QR scans."""
        return (
            self.state is BatchState.QR_BOUND
            and not self.any_slot_held
            and all(
                slot.module_qr is not None
                and slot.functional.complete
                and slot.state is DeviceState.QR_BOUND
                for slot in self.ordered_slots
            )
        )

    # ---------------------------------------------------------------------------
    # Loading and identity binding
    # ---------------------------------------------------------------------------
    
        
    def begin_port_binding(self) -> None:
        self._transition(
            BatchState.CREATED,
            BatchState.PORT_BINDING,
        )
            
    def bind_port(self, slot_number: int, port_identity: str) -> None:
        if self.state not in {
            BatchState.PORT_BINDING,
            BatchState.PORTS_BOUND,
        }:
            raise InvalidBatchTransition(
                f"Ports cannot be bound while batch is "
                f"{self.state.value}"
            )

        identity = port_identity.strip()

        if not identity:
            raise ValueError("port identity cannot be empty")

        for slot in self.ordered_slots:
            if (
                slot.number != slot_number
                and slot.port_identity == identity
            ):
                raise InvalidBatchTransition(
                    f"Port identity {identity} is already bound "
                    f"to slot {slot.number}"
                )

        self._get_slot(slot_number).bind_port(identity)

        if self.all_ports_bound:
            self.state = BatchState.PORTS_BOUND
            
    # --------------------------------------------------------------------------------
    # Stock MAC reservation and programming
    # --------------------------------------------------------------------------------
    
    def accept_stock_reservations( self, records: tuple[AllocatedMac, ...]) -> None:
        self._require_state(BatchState.PORTS_BOUND)
        self._validate_reservation_set(records, MacPurpose.STOCK_RF_TEST)
        
        for record in records:
            assert record.slot_number is not None
            self.slots[record.slot_number].assign_stock_mac(record)
            
        self.state = BatchState.STOCK_MACS_RESERVED
        
    def request_stock_program_mode(self) -> None:
        self._transition(BatchState.STOCK_MACS_RESERVED, BatchState.AWAITING_STOCK_PROGRAM_MODE)
        
    def confirm_stock_program_mode(self) -> None:
        self._transition(BatchState.AWAITING_STOCK_PROGRAM_MODE, BatchState.STOCK_PROGRAMMING)
        for slot in self.ordered_slots:
            slot.begin_stock_programming()
            
    def record_stock_programming(self, slot_number: int, *, succeeded: bool) -> None:
        self._require_state(BatchState.STOCK_PROGRAMMING)
        slot = self._get_slot(slot_number)
        slot.complete_stock_programming(succeeded)
        
        if not succeeded:
            self.place_on_hold(f"Stock programming failed or become uncertain for slot {slot_number}")
            return 
            
        if all(item.state is DeviceState.STOCK_PROGRAMMED for item in self.ordered_slots):
            self.state = BatchState.STOCK_PROGRAMMED
            
    # -----------------------------------------------------------------------------------
    # Only RF-test stage: stock firmware
    # -----------------------------------------------------------------------------------
    
    def request_stock_rf_mode(self) -> None:
        self._transition(BatchState.STOCK_PROGRAMMED, BatchState.AWAITING_STOCK_RF_MODE)
        
    def begin_stock_rf_test(self) -> None:
        self._transition(BatchState.AWAITING_STOCK_RF_MODE, BatchState.STOCK_RF_TESTING)
        
    def record_stock_rf_result( self, slot_number: int, reported_mac: str) -> None:
        self._require_state(BatchState.STOCK_RF_TESTING)
        slot = self._get_slot(slot_number)
        
        try:
            slot.verify_stock_rf(reported_mac)
        except VerificationMismatch:
            self.place_on_hold(slot.hold_reason or f"Stock RF verification failed for slot {slot_number}")
            raise
            
        if all(item.state is DeviceState.STOCK_RF_CONFIRMED for item in self.ordered_slots):
            self.state = BatchState.STOCK_RF_CONFIRMED
            
    # ----------------------------------------------------------------------------------
    # Pricol reservation, programming, and MP CLI read-back
    #-----------------------------------------------------------------------------------
    
    def accept_pricol_reservations(self, records: tuple[AllocatedMac, ...]) -> None:
        self._require_state(BatchState.STOCK_RF_CONFIRMED)
        self._validate_reservation_set(records, MacPurpose.PRICOL_PRODUCTION)
        
        stock_addresses = { slot.stock_mac for slot in self.ordered_slots }
        pricol_addresses = {record.address for record in records}
        
        overlap = stock_addresses & pricol_addresses
        if overlap:
            raise DuplicateMacAddress("Pricol allocation reuses stock MAC(s): "+", ".join(str(mac) for mac in sorted(overlap)))
            
        for record in records:
            assert record.slot_number is not None
            self.slots[record.slot_number].assign_pricol_mac(record)
            
        self.state = BatchState.PRICOL_MACS_RESERVED
        
    def request_pricol_program_mode(self) -> None:
        self._transition(BatchState.PRICOL_MACS_RESERVED, BatchState.AWAITING_PRICOL_PROGRAM_MODE)
        
    def confirm_pricol_program_mode(self) -> None:
        self._transition(BatchState.AWAITING_PRICOL_PROGRAM_MODE, BatchState.PRICOL_PROGRAMMING)
        for slot in self.ordered_slots:
            slot.begin_pricol_programming()
            
    def record_pricol_programming(self, slot_number: int, *, succeeded: bool) -> None:
        self._require_state(BatchState.PRICOL_PROGRAMMING)
        slot = self._get_slot(slot_number)
        slot.complete_pricol_programming(succeeded)
        
        if not succeeded:
            self.place_on_hold(f"Pricol programming failed or become uncertain for slot {slot_number}")
            return
            
        if all(item.state is DeviceState.PRICOL_PROGRAMMED for item in self.ordered_slots):
            self.state = BatchState.PRICOL_PROGRAMMED
            
    def record_pricol_readback(self, slot_number: int, reported_mac: str) -> None:
        """ Record MP CLI read-back; no RF test is performed here."""
        self._require_state(BatchState.PRICOL_PROGRAMMED)
        slot = self._get_slot(slot_number)
        
        try:
            slot.verify_pricol_readback(reported_mac)
        except VerificationMismatch:
            self.place_on_hold(slot.hold_reason or f"Pricol read-back failed for slot {slot_number}")
            raise
            
        if all(item.state is DeviceState.PRICOL_MAC_CONFIRMED for item in self.ordered_slots):
            self.state = BatchState.PRICOL_READBACK_VERIFIED
            
    # ---------------------------------------------------------------------------------
    # Two-reset functional test
    # ---------------------------------------------------------------------------------
    
    def request_functional_test_mode(self) -> None:
        self._transition(BatchState.PRICOL_READBACK_VERIFIED, BatchState.AWAITING_FUNCTIONAL_TEST_MODE )
        
    def confirm_functional_test_mode(self) -> None:
        self._transition(BatchState.AWAITING_FUNCTIONAL_TEST_MODE, BatchState.AWAITING_PRICOLAPP_RESET)
        
    def confirm_pricol_app_reset(self) -> None:
        self._transition(BatchState.AWAITING_PRICOLAPP_RESET, BatchState.PRICOLAPP_TESTING)
        
    def record_pricol_app(self, slot_number: int, passed: bool) -> None:
        self._require_state(BatchState.PRICOLAPP_TESTING)
        self._get_slot(slot_number).record_pricol_app(passed)
        
        if all( slot.functional.pricol_app_passed is not None for slot in self.ordered_slots):
            self.state = BatchState.AWAITING_PRICOLDFU_RESET
            
    def confirm_pricol_dfu_reset(self) -> None:
        self._transition(BatchState.AWAITING_PRICOLDFU_RESET, BatchState.PRICOLDFU_TESTING)
        
    def record_pricol_dfu(self, slot_number: int, passed: bool) -> None:
        self._require_state(BatchState.PRICOLDFU_TESTING)
        self._get_slot(slot_number).record_pricol_dfu(passed)
        
        if all( slot.functional.complete for slot in self.ordered_slots):
            
            self.state = BatchState.FUNCTIONAL_TEST_COMPLETE
            
            
    def begin_qr_scanning(self) -> None:
        """Open the final identity-binding stage after all tests finish."""
        self._transition(
            BatchState.FUNCTIONAL_TEST_COMPLETE,
            BatchState.QR_SCANNING,
            )


    @property
    def next_qr_slot(self) -> int | None:
        """Return the physical slot expected for the next scanner input."""
        for slot in self.ordered_slots:
            if slot.module_qr is None:
                return slot.number

        return None


    def scan_module_qr(self, module_qr: str) -> int:
        """Bind one scanner input to the next physical slot.

        The operator must scan the module physically present in the slot shown
        by next_qr_slot. The scanner supplies only the QR text; it does not
        select a slot.
        """
        self._require_state(BatchState.QR_SCANNING)

        qr = module_qr.strip()

        if not qr:
            raise ValueError("module QR cannot be empty")

        for slot in self.ordered_slots:
            if slot.module_qr == qr:
                raise DuplicateModuleQr(
                    f"Module QR {qr} is already linked to "
                    f"slot {slot.number}"
                )

        slot_number = self.next_qr_slot

        if slot_number is None:
            raise InvalidBatchTransition(
                "All Module QRs have already been scanned"
            )

        slot = self.slots[slot_number]
        slot.bind_module(qr)

        if self.next_qr_slot is None:
            self.state = BatchState.QR_BOUND

        return slot_number
  
    #--------------------------------------------------------------------------------------
    # Commit and upload lifecycle
    #--------------------------------------------------------------------------------------
    
    def commit(self) -> None:
        if not self.can_commit:
            raise InvalidBatchTransition("Batch cannot be committed until all functional results exist and no slot is on HOLD")
        self.state = BatchState.COMMITTED
        
    def queue_upload(self) -> None:
        self._transition(BatchState.COMMITTED, BatchState.UPLOAD_PENDING)
        
    def mark_uploaded(self) -> None:
        self._transition(BatchState.UPLOAD_PENDING, BatchState.UPLOADED)
        
    def place_on_hold(self, reason: str) -> None:
        text = reason.strip()
        if not text:
            raise ValueError("hold reason cannot be empty")
            
        self.hold_reason = text
        self.state = BatchState.HOLD
        
    def abort(self, reason: str) -> None:
        if self.state in { BatchState.COMMITTED, BatchState.UPLOAD_PENDING, BatchState.UPLOADED}:
            raise InvalidBatchTransition("A committed batch cannot be aborted")
            
        text = reason.strip()
        if not text:
            raise ValueError("abort reason cannot be empty")
            
        self.hold_reason = text
        self.state = BatchState.ABORTED
        
    # -------------------------------------------------------------------------------------------
    # Internal Validation
    # -------------------------------------------------------------------------------------------
    
    def _validate_reservation_set(self, records: tuple[AllocatedMac, ...], purpose: MacPurpose) -> None:
        """Validate a complete repository-reserved jig load."""
        if len(records) != self.slot_count:
            raise InvalidBatchTransition(
                f"Expected {self.slot_count} reserved MACs, "
                f"received {len(records)}"
            )

        addresses = [record.address for record in records]

        if len(set(addresses)) != len(addresses):
            raise DuplicateMacAddress(
                "Reservation set contains duplicate MAC addresses"
            )

        supplied_slots = [record.slot_number for record in records]
        expected_slots = set(self.slots)

        if set(supplied_slots) != expected_slots:
            display_slots = sorted(
                slot
                for slot in supplied_slots
                if slot is not None
            )
            raise InvalidBatchTransition(
                f"Reservation slots must be "
                f"{sorted(expected_slots)}; received {display_slots}"
            )

        for record in records:
            if record.purpose is not purpose:
                raise InvalidBatchTransition(
                    f"MAC {record.address} has purpose "
                    f"{record.purpose.value}; expected {purpose.value}"
                )

            if record.status is not MacStatus.RESERVED:
                raise InvalidBatchTransition(
                    f"MAC {record.address} must be RESERVED; "
                    f"current status is {record.status.value}"
                )

            if record.batch_id != self.batch_id:
                raise InvalidBatchTransition(
                    f"MAC {record.address} belongs to batch "
                    f"{record.batch_id}; expected {self.batch_id}"
                )

            if record.module_qr is not None:
                raise InvalidBatchTransition(
                    f"MAC {record.address} was linked to Module QR "
                    f"{record.module_qr} before post-test scanning"
                )

                
    def _transition(self, expected: BatchState, target: BatchState) -> None:
        self._require_state(expected)
        self.state = target
        
    def _require_state(self, expected: BatchState) -> None:
        if self.state is BatchState.HOLD:
            raise BatchOnHold(self.hold_reason or "Batch is on HOLD")
            
        if self.state is not expected:
            raise InvalidBatchTransition(f"Batch must be {expected.value}; current state is {self.state.value}")
            
    def _get_slot(self, slot_number: int) -> JigSlot:
        try:
            return self.slots[slot_number]
        except KeyError as exc:
            raise ValueError(f"slot number must be between 1 and {self.slot_count}") from exc
            
            
        
