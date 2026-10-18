"""End-to-end production workflow against SQLite and simulated hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.data.models import AllocatedMacModel, BatchModel, OutboxModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState, ProgrammingMode, SlotDisposition
from stationapp.domain.mac import MacPurpose, MacStatus
from stationapp.domain.slot import DeviceState
from stationapp.services.mac_cache import MacCacheService
from stationapp.services.production import (
    ProductionController,
    ProductionError,
    SlotPort,
)
from stationapp.services.simulation import (
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedMacAllocationGateway,
    SimulatedProgrammer,
    parse_failures,
)

STATION = "STATION-01"
JIG = "JIG-01"


@pytest.fixture
def session_factory(tmp_path: Path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    cache = MacCacheService(
        session_factory=factory,
        station_id=STATION,
        jig_id=JIG,
        jig_positions=8,
        threshold=16,
        gateway=SimulatedMacAllocationGateway(),
        request_size=32,
    )
    cache.ensure(MacPurpose.STOCK_RF_TEST)
    cache.ensure(MacPurpose.PRICOL_PRODUCTION)
    yield factory
    engine.dispose()


def make_controller(session_factory, *, failures: str = "", rf_tester=None, readiness_gate=None):
    failure_set = parse_failures(failures)
    return ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=failure_set),
        rf_tester=rf_tester or SimulatedGoldenRig(delay_seconds=0, failures=failure_set),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=failure_set),
        readiness_gate=readiness_gate,
    )


def ports(*slots: int) -> dict[int, SlotPort]:
    return {
        number: SlotPort(number, f"USB:0403:6001:SERIAL:SLOT{number}", f"COM{10 + number}")
        for number in slots
    }


def run_until(controller: ProductionController, state: BatchState, limit: int = 20) -> None:
    for _ in range(limit):
        if controller.batch.state is state:
            return
        controller.advance()
    raise AssertionError(f"Batch did not reach {state}; stuck at {controller.batch.state}")


@pytest.mark.integration
def test_partial_jig_with_independent_slot_failures(session_factory):
    controller = make_controller(session_factory, failures="stock_program:2,rf:4")
    controller.start_batch(
        occupied=(1, 2, 4, 7),
        ports=ports(1, 2, 4, 7),
        mode=ProgrammingMode.STOCK_AND_PRICOL,
        batch_label="LOT-A",
        operator_id="OP-7",
    )
    batch = controller.batch

    assert batch.state is BatchState.STOCK_MACS_RESERVED
    assert batch.empty_slot_numbers == (3, 5, 6, 8)

    run_until(controller, BatchState.QR_SCANNING)

    # Failures stay on their own slots; unrelated slots continue.
    assert batch.slots[2].state is DeviceState.HOLD
    # The HOLD reason carries the programmer's explanation, not just "failed".
    assert "Simulated programming failure" in batch.slots[2].hold_reason
    assert batch.slots[4].state is DeviceState.HOLD
    assert batch.slots[1].pricol_mac is not None
    assert batch.slots[2].pricol_mac is None

    # Ascending eligible order, skipping held slots.
    assert batch.next_qr_slot == 1
    assert controller.scan_qr("QR-0001") == 1

    with pytest.raises(ProductionError, match="already"):
        controller.scan_qr("QR-0001")

    with pytest.raises(ProductionError, match="Invalid QR"):
        controller.scan_qr("  ")

    assert controller.scan_qr("QR-0007") == 7
    assert batch.state is BatchState.QR_BOUND

    with pytest.raises(ProductionError, match="not active"):
        controller.scan_qr("QR-0099")

    controller.advance()

    assert batch.state is BatchState.UPLOAD_PENDING
    assert batch.dispositions == {
        1: SlotDisposition.PASS,
        2: SlotDisposition.HOLD,
        3: SlotDisposition.EMPTY,
        4: SlotDisposition.HOLD,
        5: SlotDisposition.EMPTY,
        6: SlotDisposition.EMPTY,
        7: SlotDisposition.PASS,
        8: SlotDisposition.EMPTY,
    }

    with session_factory() as session:
        model = session.get(BatchModel, batch.batch_id)
        assert model.state == "UPLOAD_PENDING"
        assert model.batch_label == "LOT-A"
        assert model.committed_at is not None

        held = session.scalar(
            select(AllocatedMacModel).where(
                AllocatedMacModel.address == batch.slots[2].stock_mac.compact
            )
        )
        # A MAC from failed programming never returns to the pool.
        assert held.status == MacStatus.HOLD.value

        outbox = session.scalars(select(OutboxModel)).all()
        assert len(outbox) == 1
        assert outbox[0].payload["batch_id"] == batch.batch_id
        assert outbox[0].payload["slots"][0]["module_qr"] == "QR-0001"

    controller.release_batch()
    assert controller.batch is None


@pytest.mark.integration
def test_stock_only_skips_pricol_stages(session_factory):
    controller = make_controller(session_factory)
    controller.start_batch(occupied=(1, 3), ports=ports(1, 3), mode=ProgrammingMode.STOCK_ONLY)
    batch = controller.batch

    run_until(controller, BatchState.QR_SCANNING)

    assert all(slot.pricol_mac_record is None for slot in batch.occupied_slots)
    assert controller.scan_qr("QR-A") == 1
    assert controller.scan_qr("QR-B") == 3

    controller.advance()

    assert batch.state is BatchState.UPLOAD_PENDING
    assert batch.dispositions[1] is SlotDisposition.PASS
    assert batch.dispositions[3] is SlotDisposition.PASS

    with session_factory() as session:
        pricol_used = session.scalars(
            select(AllocatedMacModel).where(
                AllocatedMacModel.purpose == MacPurpose.PRICOL_PRODUCTION.value,
                AllocatedMacModel.batch_id == batch.batch_id,
            )
        ).all()
        assert pricol_used == []


@pytest.mark.integration
def test_readback_mismatch_holds_only_that_slot(session_factory):
    controller = make_controller(session_factory, failures="stock_readback:2")
    controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.QR_SCANNING)

    batch = controller.batch
    assert batch.slots[2].state is DeviceState.HOLD
    assert "mismatch" in batch.slots[2].hold_reason.lower()
    assert batch.next_qr_slot == 1


class FlakyRig:
    """Fails once with an infrastructure error, then delegates."""

    def __init__(self) -> None:
        self.calls = 0
        self._real = SimulatedGoldenRig(delay_seconds=0, failures=frozenset())

    def run_test(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("rig unreachable")
        return self._real.run_test(**kwargs)


@pytest.mark.integration
def test_rf_infrastructure_failure_blocks_then_retries(session_factory):
    rig = FlakyRig()
    controller = make_controller(session_factory, rf_tester=rig)
    controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)

    with pytest.raises(ProductionError, match="Supervisor intervention"):
        run_until(controller, BatchState.QR_SCANNING)

    batch = controller.batch
    assert batch.state is BatchState.STOCK_RF_TESTING
    assert batch.slots[1].state is DeviceState.STOCK_READBACK_VERIFIED
    assert controller.snapshot().last_error is not None

    controller.advance()

    assert batch.state is BatchState.QR_SCANNING
    assert controller.snapshot().last_error is None


@pytest.mark.integration
def test_readiness_gate_blocks_start(session_factory):
    controller = make_controller(session_factory, readiness_gate=lambda mode, occupied: ("Golden Rig not approved",))

    with pytest.raises(ProductionError, match="not READY"):
        controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)

    assert controller.batch is None


@pytest.mark.integration
def test_start_requires_programming_interface_for_every_loaded_slot(session_factory):
    controller = make_controller(session_factory)

    with pytest.raises(ProductionError, match="slot\\(s\\) 2"):
        controller.start_batch(occupied=(1, 2), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)


class RecordingProgrammer(SimulatedProgrammer):
    """Checks persisted MAC status at the moment MPCLI would run."""

    def __init__(self, session_factory) -> None:
        super().__init__(delay_seconds=0, failures=frozenset())
        self._session_factory = session_factory
        self.status_during_programming: list[str] = []

    def program_stock(self, target, **kwargs):
        with self._session_factory() as session:
            row = session.scalar(
                select(AllocatedMacModel).where(AllocatedMacModel.address == target.mac.compact)
            )
            self.status_during_programming.append(row.status)
        return super().program_stock(target, **kwargs)


@pytest.mark.integration
def test_mac_is_persisted_as_programming_before_mpcli_runs(session_factory):
    programmer = RecordingProgrammer(session_factory)
    controller = ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=programmer,
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=frozenset()),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=frozenset()),
    )
    controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    controller.advance()  # request stock program mode
    controller.advance()  # confirm -> program + readback

    assert programmer.status_during_programming == ["PROGRAMMING", "PROGRAMMING"]


@pytest.mark.integration
def test_cannot_start_second_batch_until_first_completes(session_factory):
    controller = make_controller(session_factory)
    controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)

    with pytest.raises(ProductionError, match="not been completed"):
        controller.start_batch(occupied=(2,), ports=ports(2), mode=ProgrammingMode.STOCK_ONLY)


@pytest.mark.integration
def test_held_batch_requires_supervisor_abort(session_factory):
    controller = make_controller(session_factory, failures="stock_program:1")
    controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)

    controller.advance()
    controller.advance()

    assert controller.batch.state is BatchState.HOLD
    assert controller.snapshot().workflow.requires_supervisor

    with pytest.raises(ProductionError):
        controller.advance()

    controller.abort_batch(reason="All DUTs failed programming", actor_id="SUP-1")
    assert controller.batch.state is BatchState.ABORTED
    controller.release_batch()


@pytest.mark.integration
def test_pricol_functional_test_over_uart_records_evidence(session_factory):
    from stationapp.data.event_repository import ManufacturingEventRepository
    from stationapp.domain.events import EventType
    from stationapp.drivers.pricol.functional import PricolFunctionalTester
    from stationapp.drivers.serial.session import SerialSession
    from tests.unit.drivers.test_pricol_functional import APP_OK, FakeDut

    duts = {
        "COM11": FakeDut({b"PRICOLAPP\r\n": APP_OK, b"PRICOLDFU\r\n": [b"PRICOLDFU\r\n"]}),
        "COM12": FakeDut({b"PRICOLAPP\r\n": [b"BOOTLOADER ERROR\r\n"], b"PRICOLDFU\r\n": [b"PRICOLDFU\r\n"]}),
        "COM13": FakeDut(fail_open=True),
    }
    functional = PricolFunctionalTester(
        response_timeout_seconds=0.2,
        session_factory=lambda config: SerialSession(config=config, backend=duts[config.port]),
        poll_seconds=0.001,
    )
    none = frozenset()
    controller = ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=none),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=none),
        functional_tester=functional,
    )
    controller.start_batch(occupied=(1, 2, 3), ports=ports(1, 2, 3), mode=ProgrammingMode.STOCK_AND_PRICOL)

    run_until(controller, BatchState.QR_SCANNING)

    batch = controller.batch
    # Each module received exactly the commands it was eligible for.
    assert duts["COM11"].written == [b"PRICOLAPP\r\n", b"PRICOLDFU\r\n"]
    assert duts["COM12"].written == [b"PRICOLAPP\r\n", b"PRICOLDFU\r\n"]
    assert batch.slots[1].state is DeviceState.FUNCTIONAL_TEST_PASSED
    assert batch.slots[2].state is DeviceState.FUNCTIONAL_TEST_FAILED
    assert batch.slots[3].state is DeviceState.HOLD
    assert "serial communication failed" in batch.slots[3].hold_reason

    controller.scan_qr("QR-P1")
    controller.scan_qr("QR-P2")
    controller.advance()
    assert batch.dispositions[1] is SlotDisposition.PASS
    assert batch.dispositions[2] is SlotDisposition.FAIL
    assert batch.dispositions[3] is SlotDisposition.HOLD

    with session_factory() as session:
        events = ManufacturingEventRepository(session).list_for_batch(batch.batch_id)
    app = {event.slot_number: event.payload for event in events if event.event_type is EventType.PRICOLAPP_RESULT}
    assert app[1]["passed"] is True and app[1]["response"].strip() == "PRICOLAPP"
    assert app[2]["passed"] is False and app[2]["response"].startswith("BOOTLOADER ERROR")
    assert app[3]["passed"] is None and app[3]["com_port"] == "COM13"
