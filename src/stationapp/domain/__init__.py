
"""Pure manufacturing domain model."""

from stationapp.domain.allocation import AllocationDocument
from stationapp.domain.batch import Batch, BatchState
from stationapp.domain.errors import (
    AllocationExhausted,
    AllocationExpired,
    AllocationOwnershipMismatch,
    AllocationPurposeMismatch,
    BatchOnHold,
    DomainError,
    DuplicateMacAddress,
    DuplicateModuleQr,
    InvalidAllocation,
    InvalidBatchTransition,
    InvalidDeviceTransition,
    InvalidMacAddress,
    InvalidMacTransition,
    MacAlreadyAssigned,
    SlotAlreadyOccupied,
    SlotNotBound,
    VerificationMismatch,
)
from stationapp.domain.events import EventType, ManufacturingEvent
from stationapp.domain.mac import (
    AllocatedMac,
    MacAddress,
    MacPurpose,
    MacStatus,
)
from stationapp.domain.slot import (
    DeviceState,
    FunctionalResults,
    JigSlot,
)
from stationapp.domain.audit import AuditRecord

from stationapp.domain.golden_rig import (
    GOLDEN_RIG_PROTOCOL_VERSION,
    GoldenRigOutcome,
    GoldenRigProtocolError,
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
)

from stationapp.domain.golden_rig_binding import (
    GoldenRigAuthorizationError,
    GoldenRigBinding,
    GoldenRigBindingError,
    GoldenRigNotBoundError,
)

__all__ = [
    "AllocatedMac",
    "AllocationDocument",
    "AllocationExhausted",
    "AllocationExpired",
    "AllocationOwnershipMismatch",
    "AllocationPurposeMismatch",
    "Batch",
    "BatchOnHold",
    "BatchState",
    "DeviceState",
    "DomainError",
    "DuplicateMacAddress",
    "DuplicateModuleQr",
    "EventType",
    "FunctionalResults",
    "InvalidAllocation",
    "InvalidBatchTransition",
    "InvalidDeviceTransition",
    "InvalidMacAddress",
    "InvalidMacTransition",
    "JigSlot",
    "MacAddress",
    "MacAlreadyAssigned",
    "MacPurpose",
    "MacStatus",
    "ManufacturingEvent",
    "SlotAlreadyOccupied",
    "SlotNotBound",
    "VerificationMismatch",
    "AuditRecord",
    "GOLDEN_RIG_PROTOCOL_VERSION",
    "GoldenRigOutcome",
    "GoldenRigProtocolError",
    "GoldenRigRequest",
    "GoldenRigResponse",
    "GoldenRigSlotResult",
    "GoldenRigTarget",
    "GoldenRigAuthorizationError",
    "GoldenRigBinding",
    "GoldenRigBindingError",
    "GoldenRigNotBoundError",
]
