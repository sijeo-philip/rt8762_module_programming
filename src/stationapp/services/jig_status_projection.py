
"""Read-only projection of a manufacturing batch for the jig UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stationapp.domain.batch import Batch
from stationapp.domain.slot import DeviceState, JigSlot


class SlotVisualStatus(str, Enum):
    EMPTY = "EMPTY"
    READY = "READY"
    PROGRAMMING = "PROGRAMMING"
    READBACK = "READBACK"
    RF_READY = "RF READY"
    RF_PASS = "RF PASS"
    PRICOL_PASS = "PRICOL PASS"
    FUNCTIONAL_PASS = "FUNCTIONAL PASS"
    FUNCTIONAL_FAIL = "FUNCTIONAL FAIL"
    QR_BOUND = "QR BOUND"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class SlotView:
    number: int
    status: SlotVisualStatus
    stage: str
    mac: str = ""
    detail: str = ""
    progress: int | None = None


@dataclass(frozen=True, slots=True)
class JigView:
    batch_id: str
    station_id: str
    jig_id: str
    batch_state: str
    slots: tuple[SlotView, ...]
    occupied_count: int
    held_count: int
    rf_pass_count: int


class JigStatusProjection:
    """Convert actual DUT states into graphical status records."""

    @staticmethod
    def _mac(slot: JigSlot) -> str:
        # Show the permanent MAC once allocated.
        # Otherwise display the temporary Stock MAC.
        mac = slot.pricol_mac or slot.stock_mac
        return str(mac) if mac is not None else ""

    @classmethod
    def _project_slot(cls, slot: JigSlot) -> SlotView:
        state = slot.state

        if state is DeviceState.EMPTY:
            return SlotView(
                slot.number,
                SlotVisualStatus.EMPTY,
                "Unoccupied",
                detail="No DUT in this batch",
            )

        if state is DeviceState.HOLD:
            stage = (
                "Golden Rig RF"
                if slot.golden_rf_result is not None
                else "Manufacturing"
            )
            return SlotView(
                slot.number,
                SlotVisualStatus.HOLD,
                stage,
                mac=cls._mac(slot),
                detail=slot.hold_reason or "Investigation required",
            )

        if state is DeviceState.FUNCTIONAL_TEST_FAILED:
            return SlotView(
                slot.number,
                SlotVisualStatus.FUNCTIONAL_FAIL,
                "Functional test",
                mac=cls._mac(slot),
                detail="Functional test failed",
            )

        if state is DeviceState.FUNCTIONAL_TEST_PASSED:
            return SlotView(
                slot.number,
                SlotVisualStatus.FUNCTIONAL_PASS,
                "Functional test",
                mac=cls._mac(slot),
                detail="APP and DFU tests passed",
            )

        if state is DeviceState.QR_BOUND:
            if slot.functional.passed:
                return SlotView(
                    number=slot.number,
                    status=SlotVisualStatus.QR_BOUND,
                    stage="Identity bound / Functional PASS",
                    mac=cls._mac(slot),
                    detail=f"QR: {slot.module_qr or '—'}",
                )

            return SlotView(
                number=slot.number,
                status=SlotVisualStatus.FUNCTIONAL_FAIL,
                stage="Identity bound / Functional FAIL",
                mac=cls._mac(slot),
                detail=(
                    f"QR: {slot.module_qr or '—'} | "
                    "Module rejected after functional testing"
                ),
            )

        mapping = {
            DeviceState.PORT_BOUND: (SlotVisualStatus.READY, "Port bound"),
            DeviceState.STOCK_MAC_RESERVED: (SlotVisualStatus.READY, "Stock MAC reserved"),
            DeviceState.STOCK_PROGRAMMING: (SlotVisualStatus.PROGRAMMING, "Stock programming"),
            DeviceState.STOCK_PROGRAMMED: (SlotVisualStatus.READBACK, "Stock readback"),
            DeviceState.STOCK_READBACK_VERIFIED: (SlotVisualStatus.RF_READY, "Golden Rig RF pending"),
            DeviceState.STOCK_RF_CONFIRMED: (SlotVisualStatus.RF_PASS, "Stock RF verified"),
            DeviceState.PRICOL_MAC_RESERVED: (SlotVisualStatus.READY, "Pricol MAC reserved"),
            DeviceState.PRICOL_PROGRAMMING: (SlotVisualStatus.PROGRAMMING, "Pricol programming"),
            DeviceState.PRICOL_PROGRAMMED: (SlotVisualStatus.READBACK, "Pricol readback"),
            DeviceState.PRICOL_MAC_CONFIRMED: (SlotVisualStatus.PRICOL_PASS,"Pricol MAC verified"),
        }

        visual, stage = mapping[state]

        return SlotView(
            number=slot.number,
            status=visual,
            stage=stage,
            mac=cls._mac(slot),
            detail=stage,
        )

    def project(self, batch: Batch) -> JigView:
        occupied = set(batch.occupied_slot_numbers)

        views = []

        for slot in batch.ordered_slots:
            if slot.number not in occupied:
                views.append(
                    SlotView(
                        number=slot.number,
                        status=SlotVisualStatus.EMPTY,
                        stage="Unoccupied",
                        detail="No DUT in this batch",
                    )
                )
            else:
                views.append(self._project_slot(slot))

        return JigView(
            batch_id=batch.batch_id,
            station_id=batch.station_id,
            jig_id=batch.jig_id,
            batch_state=batch.state.value,
            slots=tuple(views),
            occupied_count=len(occupied),
            held_count=sum(slot.state is DeviceState.HOLD for slot in batch.occupied_slots),
            rf_pass_count=sum(slot.golden_rf_result is not None and slot.golden_rf_result.outcome.value == "PASS" for slot in batch.occupied_slots),
        )
