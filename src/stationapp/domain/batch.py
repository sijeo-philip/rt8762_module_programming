

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

from stationapp.domain.golden_rig import (
    GoldenRigOutcome,
    GoldenRigProtocolError,
)

from stationapp.services.golden_rig_service import (
    GoldenRigTestReport,
    INFRASTRUCTURE_ERROR_CODES,
)

class ProgrammingMode(str, Enum):
    """Operator-selected programming scope for one batch."""

    STOCK_ONLY = "STOCK_ONLY"
    STOCK_AND_PRICOL = "STOCK_AND_PRICOL"


class SlotDisposition(str, Enum):
    """Final production outcome recorded for one physical slot."""

    PASS = "PASS"
    FAIL = "FAIL"
    HOLD = "HOLD"
    EMPTY = "EMPTY"
    INCOMPLETE = "INCOMPLETE"


class BatchState(str, Enum):
    CREATED = "CREATED"

    PORT_BINDING = "PORT_BINDING"
    PORTS_BOUND = "PORTS_BOUND"

    STOCK_MACS_RESERVED = "STOCK_MACS_RESERVED"
    AWAITING_STOCK_PROGRAM_MODE = "AWAITING_STOCK_PROGRAM_MODE"
    STOCK_PROGRAMMING = "STOCK_PROGRAMMING"
    STOCK_PROGRAMMED = "STOCK_PROGRAMMED"
    STOCK_READBACK_VERIFIED = "STOCK_READBACK_VERIFIED"

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
    occupied_slot_numbers: tuple[int, ...] | None = None
    programming_mode: ProgrammingMode = ProgrammingMode.STOCK_AND_PRICOL

    # Operator-entered batch identification and the logged-in operator.
    # Traceability metadata only; no workflow rule depends on them.
    batch_label: str | None = None
    operator_id: str | None = None
    
    def __post_init__(self) -> None:
        if self.slot_count not in (4, 8):
            raise ValueError("slot_count must be 4 or 8")
            
        if not self.station_id.strip():
            raise ValueError("station_id cannot be empty")
            
        if not self.jig_id.strip():
            raise ValueError("jig_id cannot be empty")
            
        if not self.batch_id.strip():
            raise ValueError("batch_id cannot be empty")
            
        if not isinstance(self.programming_mode, ProgrammingMode):
            raise ValueError("programming_mode must be a ProgrammingMode")

        self.slots = { number: JigSlot(number) for number in range(1, self.slot_count + 1) }
        if self.occupied_slot_numbers is None:
            self.occupied_slot_numbers = tuple(range(1, self.slot_count + 1))
        else:
            self._validate_occupied_slots(self.occupied_slot_numbers)

    @property
    def ordered_slots(self) -> tuple[JigSlot, ...]:
        """Return every physical jig position in slot order."""
        return tuple(self.slots[number] for number in sorted(self.slots))
        
    @property
    def all_ports_bound(self) -> bool:
        return all(slot.port_identity is not None for slot in self.occupied_slots)
        
    @property
    def any_slot_held(self) -> bool:
        return any(slot.is_held for slot in self.ordered_slots)
        
    
    @property
    def can_commit(self) -> bool:
        """All occupied DUTs must have a documented final outcome.

        This is a mixed-disposition manufacturing commit,
        not authorization to ship every DUT.
        """
        if self.state is not BatchState.QR_BOUND:
            return False

        candidates = self.qr_candidates

        # No completed, identified module means no
        # normal batch commit.
        if not candidates:
            return False

        if any(not self._identity_complete(slot) for slot in candidates):
            return False

        # Every occupied position must be either:
        # - QR-bound after completed functional testing, or
        # - quarantined with a documented reason.
        return all(
            (
                slot.state is DeviceState.QR_BOUND
                and slot in candidates
            )
            or (
                slot.state is DeviceState.HOLD
                and bool(slot.hold_reason)
                and (
                    slot.stock_mac_record is not None
                    or slot.pricol_mac_record is not None
                )
            )
            for slot in self.occupied_slots
        )


    @property
    def requires_pricol(self) -> bool:
        return self.programming_mode is ProgrammingMode.STOCK_AND_PRICOL

    def _identity_complete(self, slot: JigSlot) -> bool:
        """QR-bound slot carries every identity record its mode requires."""
        stock = slot.stock_mac_record
        if (
            slot.state is not DeviceState.QR_BOUND
            or not slot.module_qr
            or stock is None
            or stock.status is not MacStatus.CONFIRMED
            or stock.module_qr != slot.module_qr
        ):
            return False

        if not self.requires_pricol:
            return True

        pricol = slot.pricol_mac_record
        return (
            slot.functional.complete
            and pricol is not None
            and pricol.status is MacStatus.CONFIRMED
            and pricol.module_qr == slot.module_qr
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
        """Bind a USB port to one occupied physical DUT slot."""

        if self.state not in { BatchState.PORT_BINDING, BatchState.PORTS_BOUND}:
            raise InvalidBatchTransition(
                f"Ports cannot be bound while batch is "
                f"{self.state.value}"
            )

        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(
                f"Slot {slot_number} is not populated "
                "in the current batch"
            )

        identity = port_identity.strip()

        if not identity:
            raise ValueError("port identity cannot be empty")

        # Detect duplicate USB identities across
        # the entire physical jig.
        for slot in self.ordered_slots:
            if (slot.number != slot_number and slot.port_identity == identity):
                raise InvalidBatchTransition(
                    f"Port identity {identity} is already "
                    f"bound to slot {slot.number}"
                )

        self._get_slot(slot_number).bind_port(identity)

        # Only occupied positions are required
        # to complete this production stage.
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
        self._require_state(BatchState.AWAITING_STOCK_PROGRAM_MODE)

        for slot in self.occupied_slots:
            if slot.state is not DeviceState.STOCK_MAC_RESERVED:
                raise InvalidBatchTransition(f"Slot {slot.number} is not ready for Stock programming")

        for slot in self.occupied_slots:
            slot.begin_stock_programming()

        self.state = BatchState.STOCK_PROGRAMMING
            
    
    def record_stock_programming(self, slot_number: int, *, succeeded: bool) -> None:
        """Record one independent Stock programming result."""

        self._require_state(BatchState.STOCK_PROGRAMMING)

        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(f"Slot {slot_number} is not populated")

        slot = self._get_slot(slot_number)

        slot.complete_stock_programming(succeeded)

        # Wait until all occupied slots have reported an outcome.
        if any(item.state is DeviceState.STOCK_PROGRAMMING for item in self.occupied_slots):
            return

        # At least one module must have programmed successfully.
        if any(item.state is DeviceState.STOCK_PROGRAMMED for item in self.occupied_slots):
            self.state = BatchState.STOCK_PROGRAMMED
            return

        # There is no valid module to continue.
        self.place_on_hold("No occupied slots completed Stock programming")

    def record_stock_readback(self, slot_number: int, reported_mac: str) -> None:
        """Record one MPCLI Stock MAC readback result."""

        self._require_state(BatchState.STOCK_PROGRAMMED)
        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(f"Slot {slot_number} is not populated")

        slot = self._get_slot(slot_number)
        try:
            slot.verify_stock_readback(reported_mac)
        except VerificationMismatch:
            # The affected slot and its issued MAC are held.
            # Do not place the entire batch on HOLD.
            self._complete_stock_readback_stage()
            raise

        self._complete_stock_readback_stage()


    def record_stock_readback_unverified(self, slot_number: int, *, reason: str) -> None:
        """Quarantine a Stock MAC when MPCLI cannot verify it."""

        self._require_state(BatchState.STOCK_PROGRAMMED)
        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(f"Slot {slot_number} is not populated")
        slot = self._get_slot(slot_number)
        if slot.state is not DeviceState.STOCK_PROGRAMMED:
            raise InvalidBatchTransition(f"Slot {slot_number} is not awaiting readback")
        if not reason.strip():
            raise ValueError("Readback failure reason is required")
        slot.place_on_hold(f"Stock MAC readback not verified: {reason}")
        self._complete_stock_readback_stage()


    def _complete_stock_readback_stage(self) -> None:
        """Advance after every programming-success slot is resolved."""

        if any(slot.state is DeviceState.STOCK_PROGRAMMED for slot in self.occupied_slots):
            return
        if any(slot.state is DeviceState.STOCK_READBACK_VERIFIED for slot in self.occupied_slots):
            self.state = BatchState.STOCK_READBACK_VERIFIED
        else:
            self.place_on_hold("No Stock MAC addresses passed readback")

    # -----------------------------------------------------------------------------------
    # Only RF-test stage: stock firmware
    # -----------------------------------------------------------------------------------
    
    def request_stock_rf_mode(self) -> None:
        self._transition(BatchState.STOCK_READBACK_VERIFIED, BatchState.AWAITING_STOCK_RF_MODE)
        
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
    
    def accept_pricol_reservations(self,records: tuple[AllocatedMac, ...]) -> None:
        """Reserve Pricol MACs only for RF-confirmed DUT slots."""

        self._require_state(BatchState.STOCK_RF_CONFIRMED)
        if not self.requires_pricol:
            raise InvalidBatchTransition("Stock-only batches do not reserve Pricol MACs")
        eligible = self.pricol_eligible_slots
        expected_slots = {slot.number for slot in eligible}
        if not expected_slots:
            raise InvalidBatchTransition("No RF-confirmed DUTs available for Pricol")
        if not isinstance(records, tuple):
            raise InvalidBatchTransition("Pricol reservations must be a tuple")
        if len(records) != len(expected_slots):
            raise InvalidBatchTransition(f"Expected {len(expected_slots)} Pricol MACs, received {len(records)}")

        supplied_slots = [record.slot_number for record in records]
        if (len(supplied_slots) != len(set(supplied_slots)) or set(supplied_slots) != expected_slots):
            raise InvalidBatchTransition("Pricol reservation slots must match RF-confirmed slots {sorted(expected_slots)}")

        addresses = [record.address for record in records]
        if len(addresses) != len(set(addresses)):
            raise DuplicateMacAddress("Duplicate Pricol MAC in reservation set")

        # Check against all Stock MAC addresses,
        # including failed and held slots.
        stock_addresses = {slot.stock_mac for slot in self.occupied_slots if slot.stock_mac is not None}
        overlap = stock_addresses.intersection(addresses)

        if overlap:
            raise DuplicateMacAddress(
                "Pricol MAC reuses a Stock MAC: "
                + ", ".join(
                    str(mac)
                    for mac in sorted(overlap)
                )
            )

        # Validate the complete reservation set
        # before modifying individual DUT slots.
        for record in records:

            if record.purpose is not MacPurpose.PRICOL_PRODUCTION:
                raise InvalidBatchTransition("Incorrect Pricol MAC purpose")

            if record.status is not MacStatus.RESERVED:
                raise InvalidBatchTransition("Pricol MAC is not RESERVED")

            if record.batch_id != self.batch_id:
                raise InvalidBatchTransition("Pricol MAC belongs to another batch")

            if record.slot_number not in expected_slots:
                raise InvalidBatchTransition("Pricol MAC reserved for an ineligible slot")

            if record.module_qr is not None:
                raise InvalidBatchTransition("Pricol MAC unexpectedly has a module QR")

            slot = self.slots[record.slot_number]

            if slot.pricol_mac_record is not None:
                raise InvalidBatchTransition(f"Slot {slot.number} already has a Pricol MAC")

        # All validation has passed.
        for record in records:
            assert record.slot_number is not None

            self.slots[record.slot_number].assign_pricol_mac(record)

        self.state = BatchState.PRICOL_MACS_RESERVED
        
    def request_pricol_program_mode(self) -> None:
        self._transition(BatchState.PRICOL_MACS_RESERVED, BatchState.AWAITING_PRICOL_PROGRAM_MODE)
        
    def confirm_pricol_program_mode(self) -> None:
        self._require_state(BatchState.AWAITING_PRICOL_PROGRAM_MODE)

        candidates = tuple(
            slot
            for slot in self.occupied_slots
            if slot.state is DeviceState.PRICOL_MAC_RESERVED
        )

        if not candidates:
            raise InvalidBatchTransition(
                "No Pricol programming candidates"
            )

        for slot in candidates:
            if (slot.pricol_mac_record is None or slot.pricol_mac_record.status is not MacStatus.RESERVED):
                raise InvalidBatchTransition(f"Slot {slot.number} has invalid Pricol reservation")

        for slot in candidates:
            slot.begin_pricol_programming()

        self.state = BatchState.PRICOL_PROGRAMMING
            
    
    def record_pricol_programming(self, slot_number: int, *, succeeded: bool) -> None:
        """Record independent Pricol programming outcome."""
        self._require_state(BatchState.PRICOL_PROGRAMMING)
        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(f"Slot {slot_number} is not occupied")
        slot = self._get_slot(slot_number)
        if slot.state is not DeviceState.PRICOL_PROGRAMMING:
            raise InvalidBatchTransition(f"Slot {slot_number} is not programming Pricol")
        slot.complete_pricol_programming(succeeded)
        if any(item.state is DeviceState.PRICOL_PROGRAMMING for item in self.occupied_slots):
            return
        if any(item.state is DeviceState.PRICOL_PROGRAMMED for item in self.occupied_slots):
            self.state = BatchState.PRICOL_PROGRAMMED
            return
        self.place_on_hold("No DUTs completed Pricol programming")

            
    
    def record_pricol_readback(self, slot_number: int, reported_mac: str) -> None:
        """Verify one permanent Pricol MAC through MPCLI."""

        self._require_state(BatchState.PRICOL_PROGRAMMED)

        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(f"Slot {slot_number} is not occupied")
        slot = self._get_slot(slot_number)
        try:
            slot.verify_pricol_readback(reported_mac)
        except VerificationMismatch:
            # Only this slot and its unconfirmed Pricol MAC
            # are quarantined. Other candidates continue.
            self._complete_pricol_readback_stage()
            raise
        self._complete_pricol_readback_stage()


    def record_pricol_readback_unverified(self, slot_number: int, *, reason: str) -> None:
        """Quarantine uncertain or unavailable MPCLI readback."""

        self._require_state(BatchState.PRICOL_PROGRAMMED)

        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(f"Slot {slot_number} is not occupied")
        slot = self._get_slot(slot_number)
        if slot.state is not DeviceState.PRICOL_PROGRAMMED:
            raise InvalidBatchTransition(f"Slot {slot_number} is not awaiting Pricol readback")

        if not reason.strip():
            raise ValueError("Readback failure reason is required")

        slot.place_on_hold(f"Pricol MAC readback not verified: {reason}")
        self._complete_pricol_readback_stage()


    def _complete_pricol_readback_stage(self) -> None:
        """Advance after all remaining Pricol readbacks resolve."""

        if any(slot.state is DeviceState.PRICOL_PROGRAMMED for slot in self.occupied_slots):
            return

        if any(slot.state is DeviceState.PRICOL_MAC_CONFIRMED for slot in self.occupied_slots):
            self.state = BatchState.PRICOL_READBACK_VERIFIED
        else:
            self.place_on_hold("No Pricol MAC addresses passed readback")

            
    # ---------------------------------------------------------------------------------
    # Two-reset functional test
    # ---------------------------------------------------------------------------------
    
    def request_functional_test_mode(self) -> None:
        self._transition(BatchState.PRICOL_READBACK_VERIFIED, BatchState.AWAITING_FUNCTIONAL_TEST_MODE )
        
    def confirm_functional_test_mode(self) -> None:
        self._transition(BatchState.AWAITING_FUNCTIONAL_TEST_MODE, BatchState.AWAITING_PRICOLAPP_RESET)
        
    
    def confirm_pricol_app_reset(self) -> None:
        """Start APP testing after the first physical reset."""
        self._require_state(BatchState.AWAITING_PRICOLAPP_RESET)
        if not self.functional_app_targets:
            raise InvalidBatchTransition("No Pricol-confirmed DUTs available for APP testing")
        self.state = BatchState.PRICOLAPP_TESTING


    def _require_functional_slot(self, slot_number: int) -> JigSlot:
        if slot_number not in self.occupied_slot_numbers:
            raise InvalidBatchTransition(f"Slot {slot_number} is not occupied")
        slot = self._get_slot(slot_number)
        if (slot.state is not DeviceState.PRICOL_MAC_CONFIRMED or slot.pricol_mac_record is None or slot.pricol_mac_record.status is not MacStatus.CONFIRMED):
            raise InvalidBatchTransition(f"Slot {slot_number} is not eligible for functional testing")
        return slot


    def _complete_app_stage(self) -> None:
        """Leave APP testing only after all candidates resolve."""
        if self.functional_app_targets:
            return
        if self.functional_dfu_targets:
            self.state = BatchState.AWAITING_PRICOLDFU_RESET
        else:
            self.place_on_hold("No eligible DUTs remain for DFU testing")


    def record_pricol_app(self, slot_number: int, passed: bool) -> None:
        self._require_state(BatchState.PRICOLAPP_TESTING)
        slot = self._require_functional_slot(slot_number)
        if slot.functional.pricol_app_passed is not None:
            raise InvalidBatchTransition(f"Slot {slot_number} already has an APP result")
        slot.record_pricol_app(passed)
        self._complete_app_stage()


    def record_pricol_app_unverified(self, slot_number: int, *,  reason: str) -> None:
        """Quarantine a DUT without trustworthy APP evidence."""
        self._require_state(BatchState.PRICOLAPP_TESTING)
        slot = self._require_functional_slot(slot_number)
        if slot.functional.pricol_app_passed is not None:
            raise InvalidBatchTransition(f"Slot {slot_number} already has an APP result")
        if not reason.strip():
            raise ValueError("APP failure reason required")
        slot.place_on_hold(f"PRICOLAPP outcome unverified: {reason.strip()}")
        self._complete_app_stage()


    def confirm_pricol_dfu_reset(self) -> None:
        """Start DFU testing only after all APP outcomes resolve."""
        self._require_state(BatchState.AWAITING_PRICOLDFU_RESET)
        if self.functional_app_targets:
            raise InvalidBatchTransition("APP testing is incomplete")
        if not self.functional_dfu_targets:
            raise InvalidBatchTransition("No DUTs available for DFU testing")
        self.state = BatchState.PRICOLDFU_TESTING


    def _complete_dfu_stage(self) -> None:
        """Advance after all DFU candidates finish or are held."""
        if self.functional_dfu_targets:
            return
        if self.qr_candidates:
            self.state = BatchState.FUNCTIONAL_TEST_COMPLETE
        else:
            self.place_on_hold("No completed functional results remain")


    def record_pricol_dfu(self, slot_number: int, passed: bool) -> None:
        self._require_state(BatchState.PRICOLDFU_TESTING)
        slot = self._require_functional_slot(slot_number)
        if slot.functional.pricol_app_passed is None:
            raise InvalidBatchTransition(f"APP result missing for slot {slot_number}")
        if slot.functional.pricol_dfu_passed is not None:
            raise InvalidBatchTransition(f"Slot {slot_number} already has a DFU result")
        slot.record_pricol_dfu(passed)
        self._complete_dfu_stage()


    def record_pricol_dfu_unverified(self, slot_number: int, *, reason: str) -> None:
        """Quarantine a DUT without trustworthy DFU evidence."""
        self._require_state(BatchState.PRICOLDFU_TESTING)
        slot = self._require_functional_slot(slot_number)
        if slot.functional.pricol_app_passed is None:
            raise InvalidBatchTransition(f"APP result missing for slot {slot_number}")
        if slot.functional.pricol_dfu_passed is not None:
            raise InvalidBatchTransition(f"Slot {slot_number} already has a DFU result")
        if not reason.strip():
            raise ValueError("DFU failure reason required")
        slot.place_on_hold(f"PRICOLDFU outcome unverified: {reason.strip()}")
        self._complete_dfu_stage()
           
            
    def begin_qr_scanning(self) -> None:
        """Start QR binding once the mode's final test stage completes.

        Stock + Pricol: after functional testing.
        Stock only:     directly after Stock RF confirmation.
        """
        self._require_state(
            BatchState.FUNCTIONAL_TEST_COMPLETE
            if self.requires_pricol
            else BatchState.STOCK_RF_CONFIRMED
        )
        if not self.qr_candidates:
            raise InvalidBatchTransition(
                "No completed DUTs eligible for QR scanning"
            )

        self.state = BatchState.QR_SCANNING


    @property
    def next_qr_slot(self) -> int | None:
        """Next completed functional DUT needing QR binding."""

        for slot in self.qr_candidates:
            if slot.module_qr is None:
                return slot.number

        return None


    
    def scan_module_qr(self, module_qr: str) -> int:
        """Bind the next completed DUT in physical slot order."""
        self._require_state(BatchState.QR_SCANNING)
        qr = module_qr.strip()

        if not qr:
            raise ValueError("Module QR cannot be empty")

        # Check across all slots, not only scan candidates.
        for slot in self.ordered_slots:
            if slot.module_qr == qr:
                raise DuplicateModuleQr(
                    f"Module QR {qr} already belongs "
                    f"to slot {slot.number}"
                )

        slot_number = self.next_qr_slot

        if slot_number is None:
            raise InvalidBatchTransition(
                "All eligible Module QRs have been scanned"
            )

        slot = self.slots[slot_number]

        # Existing JigSlot method validates completed
        # functional testing and MAC genealogy.
        slot.bind_module(qr, require_pricol=self.requires_pricol)

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
        if len(records) != self.occupied_count:
            raise InvalidBatchTransition(
                f"Expected {self.occupied_count} reserved MACs, "
                f"received {len(records)}"
            )

        addresses = [record.address for record in records]

        if len(set(addresses)) != len(addresses):
            raise DuplicateMacAddress(
                "Reservation set contains duplicate MAC addresses"
            )

        supplied_slots = [record.slot_number for record in records]
        expected_slots = set(self.occupied_slot_numbers)

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

    
    def _validate_occupied_slots(self, numbers: tuple[int, ...]) -> None:
        if not isinstance(numbers, tuple):
            raise ValueError("occupied_slot_numbers must be a tuple")

        if not numbers:
            raise ValueError("At least one occupied slot is required")

        if any(type(number) is not int for number in numbers):
            raise ValueError("Occupied slot numbers must be integers")

        if len(numbers) != len(set(numbers)):
            raise ValueError("Duplicate occupied jig slot")

        if any(number not in self.slots for number in numbers):
            raise ValueError("Occupied slot outside jig capacity")


    def configure_occupied_slots(self, numbers: tuple[int, ...]) -> None:
        """Record the operator-confirmed jig load.

        Occupancy becomes immutable when port binding begins.
        """
        self._require_state(BatchState.CREATED)
        self._validate_occupied_slots(numbers)
        self.occupied_slot_numbers = tuple(sorted(numbers))


    @property
    def occupied_slots(self) -> tuple[JigSlot, ...]:
        return tuple(self.slots[number] for number in self.occupied_slot_numbers)


    @property
    def empty_slot_numbers(self) -> tuple[int, ...]:
        occupied = set(self.occupied_slot_numbers)

        return tuple(number for number in self.slots if number not in occupied)


    @property
    def occupied_count(self) -> int:
        return len(self.occupied_slot_numbers)


    @property
    def stock_programming_targets(self) -> tuple[int, ...]:
        return tuple(slot.number for slot in self.occupied_slots if slot.state is DeviceState.STOCK_MAC_RESERVED)


    @property
    def stock_readback_targets(self) -> tuple[int, ...]:
        return tuple(slot.number for slot in self.occupied_slots if slot.state is DeviceState.STOCK_PROGRAMMED)

    
    @property
    def pricol_eligible_slots(self) -> tuple[JigSlot, ...]:
        """Occupied slots with confirmed Stock RF evidence."""
        return tuple(
            slot
            for slot in self.occupied_slots
            if slot.state is DeviceState.STOCK_RF_CONFIRMED
            and slot.stock_mac_record is not None
            and slot.stock_mac_record.status is MacStatus.CONFIRMED
            and slot.golden_rf_result is not None
            and slot.golden_rf_result.outcome is GoldenRigOutcome.PASS
        )


    @property
    def pricol_programming_targets(self) -> tuple[int, ...]:
        return tuple(
            slot.number
            for slot in self.occupied_slots
            if slot.state is DeviceState.PRICOL_MAC_RESERVED
        )


    @property
    def pricol_readback_targets(self) -> tuple[int, ...]:
        return tuple(
            slot.number
            for slot in self.occupied_slots
            if slot.state is DeviceState.PRICOL_PROGRAMMED
        )


    @property
    def pricol_confirmed_slots(self) -> tuple[int, ...]:
        return tuple(
            slot.number
            for slot in self.occupied_slots
            if slot.state is DeviceState.PRICOL_MAC_CONFIRMED
        )

    
    @property
    def functional_candidates(self) -> tuple[JigSlot, ...]:
        """DUTs that entered functional testing successfully.

        The Pricol MAC must already be confirmed.
        """
        return tuple(
            slot
            for slot in self.occupied_slots
            if (
                slot.pricol_mac_record is not None
                and slot.pricol_mac_record.status is MacStatus.CONFIRMED
                and slot.state in {
                    DeviceState.PRICOL_MAC_CONFIRMED,
                    DeviceState.FUNCTIONAL_TEST_PASSED,
                    DeviceState.FUNCTIONAL_TEST_FAILED,
                    DeviceState.QR_BOUND,
                }
            )
        )


    @property
    def functional_app_targets(self) -> tuple[int, ...]:
        """Slots still waiting for an APP test outcome."""
        return tuple(
            slot.number
            for slot in self.functional_candidates
            if (
                slot.state is DeviceState.PRICOL_MAC_CONFIRMED
                and slot.functional.pricol_app_passed is None
            )
        )


    @property
    def functional_dfu_targets(self) -> tuple[int, ...]:
        """Slots still waiting for a DFU test outcome."""
        return tuple(
            slot.number
            for slot in self.functional_candidates
            if (
                slot.state is DeviceState.PRICOL_MAC_CONFIRMED
                and slot.functional.pricol_app_passed is not None
                and slot.functional.pricol_dfu_passed is None
            )
        )

    @property
    def qr_candidates(self) -> tuple[JigSlot, ...]:
        """Completed DUTs requiring post-test identity.

        Stock + Pricol: functional testing complete.
        Stock only:     Stock RF confirmed.
        """
        if not self.requires_pricol:
            return tuple(
                slot
                for slot in self.occupied_slots
                if (
                    slot.stock_mac_record is not None
                    and slot.stock_mac_record.status is MacStatus.CONFIRMED
                    and slot.state in {
                        DeviceState.STOCK_RF_CONFIRMED,
                        DeviceState.QR_BOUND,
                    }
                )
            )

        return tuple(
            slot
            for slot in self.occupied_slots
            if (
                slot.functional.complete
                and slot.pricol_mac_record is not None
                and slot.pricol_mac_record.status is MacStatus.CONFIRMED
                and slot.stock_mac_record is not None
                and slot.stock_mac_record.status is MacStatus.CONFIRMED
                and slot.state in {
                    DeviceState.FUNCTIONAL_TEST_PASSED,
                    DeviceState.FUNCTIONAL_TEST_FAILED,
                    DeviceState.QR_BOUND,
                }
            )
        )


    @property
    def rejected_slot_numbers(self) -> tuple[int, ...]:
        """Held or functionally failed DUTs."""
        return tuple(
            slot.number
            for slot in self.occupied_slots
            if (
                slot.state is DeviceState.HOLD
                or (
                    slot.functional.complete
                    and not slot.functional.passed
                )
            )
        )

    @property
    def releasable_slot_numbers(self) -> tuple[int, ...]:
        """QR-bound DUTs that passed every stage their mode requires."""
        return tuple(
            slot.number
            for slot in self.occupied_slots
            if (
                self._identity_complete(slot)
                and (slot.functional.passed or not self.requires_pricol)
            )
        )

    def disposition_for(self, slot_number: int) -> SlotDisposition:
        """Final PASS / FAIL / HOLD / EMPTY outcome for one slot.

        No slot is PASS unless every mandatory identity and test record
        exists. INCOMPLETE means the slot is still moving through the
        workflow.
        """
        slot = self._get_slot(slot_number)

        if slot_number not in self.occupied_slot_numbers:
            return SlotDisposition.EMPTY

        if slot.state is DeviceState.HOLD:
            return SlotDisposition.HOLD

        if slot_number in self.releasable_slot_numbers:
            return SlotDisposition.PASS

        if slot.functional.complete and not slot.functional.passed:
            return SlotDisposition.FAIL

        return SlotDisposition.INCOMPLETE

    @property
    def dispositions(self) -> dict[int, SlotDisposition]:
        return {number: self.disposition_for(number) for number in sorted(self.slots)}


