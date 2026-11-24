"""Operator RETRY of one failed slot.

Every step runs once per slot; there are no automatic retries. A slot that
failed (or whose module was not detected) may be tried again by the
operator, as often as needed, but only while the rest of the tray waits
for the operator at the step after the failed one, so a retry never runs
beside another step. A pass brings the module level with the others.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import select

from stationapp.data.models import AllocatedMacModel, ManufacturingEventModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState, ProgrammingMode, RetryStep, SlotDisposition
from stationapp.domain.mac import MacStatus
from stationapp.domain.slot import DeviceState
from stationapp.services.production import ProductionController, ProductionError
from stationapp.services.simulation import (
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedProgrammer,
    parse_failures,
)

from tests.integration.services.test_production_controller import (  # noqa: F401 - fixture
    JIG,
    STATION,
    TEMP_BASE,
    ports,
    run_until,
    session_factory,
)


class Bench:
    """Simulated MPCLI, Golden Rig and UART tester whose faults the test
    changes between operator presses (e.g. a module re-seated)."""

    def __init__(self, spec: str = "") -> None:
        self.failures = set(parse_failures(spec))
        self.calls: Counter[tuple[str, int]] = Counter()
        self.rig_down = False

    def fix(self, spec: str) -> None:
        self.failures -= set(parse_failures(spec))

    def _faults(self) -> frozenset:
        return frozenset(self.failures)

    def _program(self, method: str, stage: str, target, **kwargs):
        self.calls[(stage, target.slot_number)] += 1
        return getattr(SimulatedProgrammer(delay_seconds=0, failures=self._faults()), method)(target, **kwargs)

    def program_stock(self, target, **kwargs):
        return self._program("program_stock", "stock_program", target, **kwargs)

    def program_pricol(self, target, **kwargs):
        return self._program("program_pricol", "pricol_program", target, **kwargs)

    def verify_stock_mac(self, target, **kwargs):
        return self._program("verify_stock_mac", "stock_readback", target, **kwargs)

    def verify_pricol_mac(self, target, **kwargs):
        return self._program("verify_pricol_mac", "pricol_readback", target, **kwargs)

    def run_test(self, *, targets, **kwargs):
        for target in targets:
            self.calls[("rf", target.slot_number)] += 1
        if self.rig_down:
            raise ConnectionError("rig unreachable")
        return SimulatedGoldenRig(delay_seconds=0, failures=self._faults()).run_test(targets=targets, **kwargs)

    def run_app_test(self, *, slot_number, **kwargs):
        self.calls[("app", slot_number)] += 1
        return SimulatedFunctionalTester(delay_seconds=0, failures=self._faults()).run_app_test(slot_number=slot_number, **kwargs)

    def run_dfu_test(self, *, slot_number, **kwargs):
        self.calls[("dfu", slot_number)] += 1
        return SimulatedFunctionalTester(delay_seconds=0, failures=self._faults()).run_dfu_test(slot_number=slot_number, **kwargs)


def make(session_factory, bench: Bench) -> ProductionController:
    return ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=bench,
        rf_tester=bench,
        functional_tester=bench,
        temporary_stock_mac_base=TEMP_BASE,
    )


def retry_events(session_factory, batch_id):
    with session_factory() as session:
        rows = session.scalars(
            select(ManufacturingEventModel)
            .where(ManufacturingEventModel.batch_id == batch_id, ManufacturingEventModel.event_type == "STAGE_RETRY")
            .order_by(ManufacturingEventModel.id)
        ).all()
        return [(row.slot_number, row.payload["data"]["stage"], row.payload["data"]["attempt"], row.payload["data"]["manual"]) for row in rows]


def mac_status(session_factory, mac) -> str:
    with session_factory() as session:
        row = session.scalar(select(AllocatedMacModel).where(AllocatedMacModel.address == mac.compact))
        return row.status


def finish(controller: ProductionController, qrs: dict[int, str]) -> None:
    batch = controller.batch
    run_until(controller, BatchState.QR_SCANNING)
    while batch.next_qr_slot is not None:
        controller.scan_qr(qrs[batch.next_qr_slot])
    controller.advance()  # COMPLETE BATCH


QRS = {n: f"QR{n:04d}" for n in range(1, 9)}


@pytest.mark.integration
def test_no_automatic_retry_a_failed_slot_fails_once(session_factory):
    bench = Bench("stock_program:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    assert bench.calls[("stock_program", 2)] == 1
    assert batch.slots[2].is_held
    assert retry_events(session_factory, batch.batch_id) == []
    assert controller.snapshot().retry_slots == ((2, RetryStep.STOCK),)


@pytest.mark.integration
def test_operator_retries_stock_programming_until_it_passes(session_factory):
    bench = Bench("stock_program:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2, 3), ports=ports(1, 2, 3), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)
    mac = batch.slots[2].stock_mac

    # Fails again: still held, still retryable - any number of times.
    controller.retry_slot(2)
    controller.retry_slot(2)
    assert batch.slots[2].is_held and controller.snapshot().retry_slots == ((2, RetryStep.STOCK),)

    bench.fix("stock_program:2")
    controller.retry_slot(2)

    assert batch.slots[2].state is DeviceState.STOCK_READBACK_VERIFIED
    assert batch.slots[2].stock_mac == mac                       # same MAC
    assert batch.state is BatchState.STOCK_READBACK_VERIFIED     # tray still waits for RUN RF TEST
    assert bench.calls[("stock_program", 1)] == 1                # others untouched
    assert bench.calls[("stock_program", 3)] == 1
    assert bench.calls[("stock_program", 2)] == 4
    assert bench.calls[("stock_readback", 2)] == 1               # read back after the pass
    assert controller.snapshot().retry_slots == ()
    assert retry_events(session_factory, batch.batch_id) == [
        (2, "STOCK_PROGRAMMING", 1, True),
        (2, "STOCK_PROGRAMMING", 2, True),
        (2, "STOCK_PROGRAMMING", 3, True),
    ]

    finish(controller, QRS)
    assert batch.dispositions[2] is SlotDisposition.PASS


@pytest.mark.integration
def test_retry_window_closes_when_the_tray_moves_on(session_factory):
    bench = Bench("stock_program:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    controller.advance()  # RUN RF TEST: the others go on; slot 2's failure is final

    assert batch.state is BatchState.QR_SCANNING
    assert controller.snapshot().retry_slots == ()
    with pytest.raises(ProductionError, match="cannot be retried now"):
        controller.retry_slot(2)


@pytest.mark.integration
def test_retry_refused_for_a_slot_that_passed_or_while_running(session_factory):
    bench = Bench("stock_program:2")
    controller = make(session_factory, bench)
    controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    with pytest.raises(ProductionError, match="cannot be retried now"):
        controller.retry_slot(2)                                 # not failed yet
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)
    with pytest.raises(ProductionError, match="cannot be retried now"):
        controller.retry_slot(1)                                 # passed

    controller._running = True                                   # another step is running
    with pytest.raises(ProductionError, match="Wait for the running step"):
        controller.retry_slot(2)


@pytest.mark.integration
def test_not_detected_slot_rejoins_with_a_new_mac(session_factory):
    bench = Bench("absent:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)
    assert batch.occupied_slot_numbers == (1,)
    assert controller.snapshot().retry_slots == ((2, RetryStep.STOCK),)

    # Still no module: it leaves the tray again.
    controller.retry_slot(2)
    assert batch.occupied_slot_numbers == (1,) and 2 in batch.not_detected_slots

    bench.fix("absent:2")                                        # module re-seated
    controller.retry_slot(2)

    assert batch.occupied_slot_numbers == (1, 2)
    assert 2 not in batch.not_detected_slots
    assert batch.slots[2].state is DeviceState.STOCK_READBACK_VERIFIED
    assert mac_status(session_factory, batch.slots[2].stock_mac) == MacStatus.ISSUED.value

    finish(controller, QRS)
    assert batch.dispositions[2] is SlotDisposition.PASS


@pytest.mark.integration
def test_not_detected_slot_in_stock_and_pricol_gets_its_temporary_mac(session_factory):
    bench = Bench("absent:3")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 3), ports=ports(1, 3), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    bench.fix("absent:3")
    controller.retry_slot(3)

    assert batch.slots[3].state is DeviceState.STOCK_READBACK_VERIFIED
    finish(controller, QRS)
    assert batch.dispositions[3] is SlotDisposition.PASS


@pytest.mark.integration
def test_stock_readback_failure_is_retried(session_factory):
    bench = Bench("stock_readback:1")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)
    assert batch.slots[1].is_held

    bench.fix("stock_readback:1")
    controller.retry_slot(1)

    assert batch.slots[1].state is DeviceState.STOCK_READBACK_VERIFIED


@pytest.mark.integration
def test_rf_retry_joins_the_pricol_step(session_factory):
    bench = Bench("rf:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.PRICOL_MACS_RESERVED)
    assert controller.snapshot().retry_slots == ((2, RetryStep.STOCK_RF),)

    # Fails again: the Pricol MAC reserved for it goes back to the cache.
    controller.retry_slot(2)
    assert batch.slots[2].is_held and batch.slots[2].pricol_mac is None
    assert batch.released_macs and mac_status(session_factory, batch.released_macs[-1].address) == MacStatus.AVAILABLE.value

    bench.fix("rf:2")
    controller.retry_slot(2)

    assert batch.slots[2].state is DeviceState.PRICOL_MAC_RESERVED
    assert mac_status(session_factory, batch.slots[2].pricol_mac) == MacStatus.RESERVED.value
    assert bench.calls[("rf", 1)] == 1                           # only slot 2 tested again
    assert bench.calls[("rf", 2)] == 3

    finish(controller, QRS)
    assert batch.dispositions[2] is SlotDisposition.PASS
    assert mac_status(session_factory, batch.slots[2].pricol_mac) == MacStatus.CONFIRMED.value


@pytest.mark.integration
def test_rig_down_during_rf_retry_leaves_the_slot_held(session_factory):
    bench = Bench("rf:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.PRICOL_MACS_RESERVED)

    bench.rig_down = True
    with pytest.raises(ProductionError, match="could not run"):
        controller.retry_slot(2)

    assert batch.slots[2].is_held and batch.slots[2].pricol_mac is None
    assert controller.snapshot().last_error is not None
    assert controller.snapshot().retry_slots == ((2, RetryStep.STOCK_RF),)   # may be pressed again


@pytest.mark.integration
def test_stock_only_rf_retry_during_qr_scanning(session_factory):
    bench = Bench("rf:1")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.QR_SCANNING)
    assert controller.snapshot().retry_slots == ((1, RetryStep.STOCK_RF),)

    bench.fix("rf:1")
    controller.retry_slot(1)

    assert batch.slots[1].state is DeviceState.STOCK_RF_CONFIRMED
    finish(controller, QRS)
    assert batch.dispositions[1] is SlotDisposition.PASS


@pytest.mark.integration
def test_qr_scanned_closes_the_retry(session_factory):
    bench = Bench("rf:1")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.QR_SCANNING)

    controller.scan_qr(QRS[1])

    assert batch.retry_step(1) is None


@pytest.mark.integration
def test_pricol_programming_failure_is_retried(session_factory):
    bench = Bench("pricol_program:1")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.PRICOL_READBACK_VERIFIED)
    mac = batch.slots[1].pricol_mac
    assert controller.snapshot().retry_slots == ((1, RetryStep.PRICOL),)

    bench.fix("pricol_program:1")
    controller.retry_slot(1)

    assert batch.slots[1].state is DeviceState.PRICOL_MAC_CONFIRMED
    assert batch.slots[1].pricol_mac == mac                      # same Pricol MAC
    finish(controller, QRS)
    assert batch.dispositions[1] is SlotDisposition.PASS


@pytest.mark.integration
def test_pricol_not_detected_slot_gets_a_new_pricol_mac(session_factory):
    bench = Bench()
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.PRICOL_MACS_RESERVED)
    bench.failures.add(("absent", 2))                            # module lost before Pricol
    controller.advance()
    assert batch.slots[2].is_held and batch.slots[2].pricol_mac is None

    bench.fix("absent:2")
    controller.retry_slot(2)

    assert batch.slots[2].state is DeviceState.PRICOL_MAC_CONFIRMED
    assert mac_status(session_factory, batch.slots[2].pricol_mac) == MacStatus.CONFIRMED.value
    finish(controller, QRS)
    assert batch.dispositions[2] is SlotDisposition.PASS


@pytest.mark.integration
def test_app_test_failure_is_retried_before_the_dfu_test(session_factory):
    bench = Bench("app:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.AWAITING_PRICOLDFU_RESET)
    assert batch.slots[2].functional.pricol_app_passed is False
    assert controller.snapshot().retry_slots == ((2, RetryStep.PRICOL_APP),)

    bench.fix("app:2")
    controller.retry_slot(2)

    assert batch.slots[2].functional.pricol_app_passed is True
    assert controller.snapshot().retry_slots == ()
    finish(controller, QRS)
    assert batch.dispositions[2] is SlotDisposition.PASS


@pytest.mark.integration
def test_dfu_test_failure_is_retried_during_qr_scanning(session_factory):
    bench = Bench("dfu:1")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.QR_SCANNING)
    assert batch.slots[1].state is DeviceState.FUNCTIONAL_TEST_FAILED
    assert controller.snapshot().retry_slots == ((1, RetryStep.PRICOL_DFU),)

    bench.fix("dfu:1")
    controller.retry_slot(1)

    assert batch.slots[1].state is DeviceState.FUNCTIONAL_TEST_PASSED
    assert bench.calls[("app", 1)] == 1                          # APP not repeated
    finish(controller, QRS)
    assert batch.dispositions[1] is SlotDisposition.PASS


@pytest.mark.integration
def test_every_slot_failed_offers_no_retry(session_factory):
    bench = Bench("stock_program:1,stock_program:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)

    run_until(controller, BatchState.QR_SCANNING)

    # No module waits for a next step: RESTART TRAY repeats the tray.
    assert controller.snapshot().retry_slots == ()
    assert batch.retry_step(1) is None


@pytest.mark.integration
def test_restart_clears_retry_counts(session_factory):
    bench = Bench("stock_program:2")
    controller = make(session_factory, bench)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)
    controller.retry_slot(2)

    controller.restart_batch()
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)
    controller.retry_slot(2)

    assert [event[2] for event in retry_events(session_factory, batch.batch_id)] == [1, 1]


class HeldRig(Bench):
    """A rig that holds each RF request until released; tracks overlap."""

    def __init__(self, spec: str = "") -> None:
        super().__init__(spec)
        import threading
        self.entered = threading.Event()
        self.release = threading.Event()
        self.hold = False
        self.active = 0
        self.max_active = 0
        self.requests: list[tuple[int, ...]] = []

    def run_test(self, *, targets, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.requests.append(tuple(target.slot_number for target in targets))
        try:
            if self.hold:
                self.entered.set()
                assert self.release.wait(5)
            return super().run_test(targets=targets, **kwargs)
        finally:
            self.active -= 1


@pytest.mark.integration
def test_rf_retries_reach_the_rig_one_slot_at_a_time(session_factory, tmp_path):
    import threading

    from stationapp.services.golden_rig_configuration import RigEndpoint
    from stationapp.services.golden_rig_configuration import GoldenRigIdentityError, GoldenRigIdentityProbe

    rig = HeldRig("rf:2,rf:3")
    controller = make(session_factory, rig)
    batch = controller.start_batch(occupied=(1, 2, 3), ports=ports(1, 2, 3), mode=ProgrammingMode.STOCK_AND_PRICOL)
    run_until(controller, BatchState.PRICOL_MACS_RESERVED)
    assert rig.requests == [(1, 2, 3)]
    rig.fix("rf:2,rf:3")

    rig.hold = True
    worker = threading.Thread(target=controller.retry_slot, args=(2,))
    worker.start()
    assert rig.entered.wait(5)

    # While slot 2 is at the rig: no other slot, no tray step, no check.
    with pytest.raises(ProductionError, match="Wait for the running step"):
        controller.retry_slot(3)
    with pytest.raises(ProductionError, match="already running"):
        controller.advance()
    ca = tmp_path / "ca.pem"
    ca.write_text("unused")
    with pytest.raises(GoldenRigIdentityError, match="RF test is running"):
        GoldenRigIdentityProbe(ca_certificate=ca).probe(RigEndpoint("127.0.0.1", 9))

    rig.release.set()
    worker.join(5)
    rig.hold = False
    controller.retry_slot(3)

    assert rig.requests == [(1, 2, 3), (2,), (3,)]       # one slot per retry request
    assert rig.max_active == 1                            # never two requests at once
    assert batch.slots[2].state is DeviceState.PRICOL_MAC_RESERVED
    assert batch.slots[3].state is DeviceState.PRICOL_MAC_RESERVED


@pytest.mark.unit
def test_rig_lock_refuses_a_second_exchange():
    from stationapp.services.golden_rig_lock import GoldenRigBusy, golden_rig_exchange

    with golden_rig_exchange("first"):
        with pytest.raises(GoldenRigBusy):
            with golden_rig_exchange("second", wait=False):
                pass
        with pytest.raises(GoldenRigBusy):
            with golden_rig_exchange("third", timeout=0.05):
                pass
    with golden_rig_exchange("after", wait=False):
        pass
