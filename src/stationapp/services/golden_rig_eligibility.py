
"""Select physical DUT slots eligible for Golden Rig RF testing."""

from __future__ import annotations

from stationapp.domain.batch import Batch
from stationapp.domain.errors import InvalidBatchTransition
from stationapp.domain.golden_rig import GoldenRigTarget
from stationapp.domain.mac import MacPurpose, MacStatus
from stationapp.domain.slot import DeviceState


class GoldenRigEligibilityService:
    """Build RF targets from verified slot and MAC state."""

    def build_targets(self, batch: Batch) -> tuple[GoldenRigTarget, ...]:

        targets: list[GoldenRigTarget] = []
        for slot in batch.ordered_slots:

            # Excludes empty, failed, held, and completed slots.
            if slot.state is not DeviceState.STOCK_READBACK_VERIFIED:
                continue
            record = slot.stock_mac_record
            if record is None:
                raise InvalidBatchTransition(f"Slot {slot.number} has no Stock MAC record")

            if (record.purpose is not MacPurpose.STOCK_RF_TEST
                or record.batch_id != batch.batch_id
                or record.slot_number != slot.number
                or record.status is not MacStatus.ISSUED
                or slot.stock_readback_mac != record.address
            ):
                raise InvalidBatchTransition(
                    f"Slot {slot.number} has inconsistent "
                    "Stock MAC verification evidence"
                )

            targets.append(
                GoldenRigTarget(
                    slot_number=slot.number,
                    expected_mac=record.address,
                )
            )

        return tuple(targets)
