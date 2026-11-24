"""Station app: a slot where MPCLI finds no module leaves the batch."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from stationapp.data.models import AllocatedMacModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState, ProgrammingMode, SlotDisposition
from stationapp.domain.events import EventType
from stationapp.domain.mac import MacStatus
from stationapp.drivers.mpcli.types import ProcessOutcome, ProcessResult, ProgrammingOutcome, ProgrammingResult
from stationapp.services.production import ProductionController, build_batch_record
from stationapp.services.programming import SlotProgrammingResult
from stationapp.services.simulation import SimulatedFunctionalTester, SimulatedGoldenRig, SimulatedProgrammer
from tests.integration.services.test_production_controller import JIG, STATION, TEMP_BASE, make_controller, ports, run_until, session_factory  # noqa: F401


def mac_status(factory, mac: str) -> str:
    with factory() as session:
        row = session.scalar(select(AllocatedMacModel).where(AllocatedMacModel.address == mac.replace(":", "")))
        return row.status


@pytest.mark.integration
def test_empty_slot_gets_no_mac_no_test_and_no_qr(session_factory):  # noqa: F811
    controller = make_controller(session_factory, failures="absent:3")
    controller.start_batch(occupied=(1, 2, 3), ports=ports(1, 2, 3), mode=ProgrammingMode.STOCK_ONLY)
    batch = controller.batch
    released = str(batch.slots[3].stock_mac)

    run_until(controller, BatchState.QR_SCANNING)

    assert batch.occupied_slot_numbers == (1, 2)
    assert 3 in batch.not_detected_slots and "ERR 6" in batch.not_detected_slots[3]
    assert batch.slots[3].stock_mac is None
    assert mac_status(session_factory, released) == MacStatus.AVAILABLE.value     # back in the cache
    view = controller.snapshot().jig
    assert view.slots[2].stage == "NOT DETECTED - skipped"

    assert batch.next_qr_slot == 1
    controller.scan_qr("QR-0001")
    controller.scan_qr("QR-0002")
    assert batch.state is BatchState.QR_BOUND                      # slot 3 never asked for
    controller.advance()

    assert batch.dispositions[3] is SlotDisposition.EMPTY
    slot3 = next(item for item in build_batch_record(batch)["slots"] if item["slot_number"] == 3)
    assert slot3["stock_mac"] is None and slot3["module_qr"] is None

    from stationapp.data.event_repository import ManufacturingEventRepository

    with session_factory() as session:
        events = ManufacturingEventRepository(session).list_for_batch(batch.batch_id)
    missing = [event for event in events if event.event_type is EventType.MODULE_NOT_DETECTED]
    assert len(missing) == 1 and missing[0].slot_number == 3 and missing[0].payload["released_mac"] == released


@pytest.mark.integration
def test_no_module_anywhere_ends_the_batch_without_using_macs(session_factory):  # noqa: F811
    controller = make_controller(session_factory, failures="absent:1,absent:2")
    controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    batch = controller.batch
    macs = [str(batch.slots[n].stock_mac) for n in (1, 2)]

    controller.advance()
    controller.advance()

    assert batch.state is BatchState.ABORTED
    assert "No module was detected" in batch.hold_reason
    assert all(mac_status(session_factory, mac) == MacStatus.AVAILABLE.value for mac in macs)
    controller.release_batch()                                      # operator can load the next batch


class PricolAbsentProgrammer:
    """Stock fine; slot 1's module does not answer for Pricol."""

    def __init__(self) -> None:
        self._real = SimulatedProgrammer(delay_seconds=0, failures=frozenset())

    def __getattr__(self, name):
        return getattr(self._real, name)

    def program_pricol(self, target, **kwargs):
        if target.slot_number != 1:
            return self._real.program_pricol(target, **kwargs)
        process = ProcessResult(("-c", "COM11", "-E", "-r"), ProcessOutcome.COMPLETED, 6, "-ERR: 6-\nrecv hci evt Fail!\nDetect FW Fail", "", 1.0)
        result = ProgrammingResult(ProgrammingOutcome.NOT_DETECTED, process, "MPCLI ERR 6. NO MODULE DETECTED")
        from stationapp.services.programming import ProgrammingStage

        return SlotProgrammingResult(target=target, stage=ProgrammingStage.PRICOL, programming=result)


@pytest.mark.integration
def test_module_lost_before_pricol_releases_the_pricol_mac_and_is_held(session_factory):  # noqa: F811
    controller = ProductionController(
        station_id=STATION, jig_id=JIG, slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=PricolAbsentProgrammer(),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=frozenset()),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=frozenset()),
        temporary_stock_mac_base=TEMP_BASE,
    )
    controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)
    batch = controller.batch
    run_until(controller, BatchState.PRICOL_MACS_RESERVED)
    pricol_mac = str(batch.slots[1].pricol_mac)

    run_until(controller, BatchState.QR_SCANNING)

    assert batch.slots[1].pricol_mac is None
    assert mac_status(session_factory, pricol_mac) == MacStatus.AVAILABLE.value
    assert batch.disposition_for(1) is SlotDisposition.HOLD
    assert "not detected for Pricol" in batch.slots[1].hold_reason
    assert batch.next_qr_slot == 1                                  # its Stock record exists: QR is scanned
