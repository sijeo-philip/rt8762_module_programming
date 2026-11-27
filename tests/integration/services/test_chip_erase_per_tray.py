"""The Supervisor's per-tray "Chip erase" choice reaches the programmer."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from stationapp.data.models import ManufacturingEventModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import ProgrammingMode
from stationapp.services.production import ProductionController
from stationapp.services.simulation import SimulatedFunctionalTester, SimulatedGoldenRig, SimulatedProgrammer

from tests.integration.services.test_production_controller import (  # noqa: F401 - fixture
    JIG,
    STATION,
    ports,
    session_factory,
)


def make(session_factory, built: list[bool]) -> ProductionController:
    def factory(stock, pricol, *, chip_erase=True):
        built.append(chip_erase)
        return SimulatedProgrammer(delay_seconds=0, failures=frozenset())

    return ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer_factory=factory,
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=frozenset()),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=frozenset()),
        require_firmware=False,
    )


def created_payload(session_factory, batch_id: str) -> dict:
    with session_factory() as session:
        event = session.scalars(
            select(ManufacturingEventModel).where(
                ManufacturingEventModel.batch_id == batch_id,
                ManufacturingEventModel.event_type == "BATCH_CREATED",
            )
        ).one()
        return event.payload["data"]


@pytest.mark.integration
@pytest.mark.parametrize("chip_erase", [True, False])
def test_tray_chip_erase_choice(session_factory, chip_erase):
    built: list[bool] = []
    controller = make(session_factory, built)
    batch = controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY, chip_erase=chip_erase)

    assert built == [chip_erase]
    assert created_payload(session_factory, batch.batch_id)["chip_erase"] is chip_erase


@pytest.mark.integration
def test_chip_erase_defaults_on(session_factory):
    built: list[bool] = []
    make(session_factory, built).start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)
    assert built == [True]
