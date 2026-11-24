"""Automatic per-slot retries of the programming, readback and RF steps.

Floor finding: random failures at Stock programming, Stock MAC readback,
Stock RF test, Pricol programming and Pricol MAC readback. Each slot that
fails is tried again on its own (default three attempts) before the tray
moves on; slots that passed are never repeated.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import select

from stationapp.data.models import ManufacturingEventModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState, ProgrammingMode
from stationapp.domain.slot import DeviceState
from stationapp.services.production import ProductionController
from stationapp.services.simulation import (
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedProgrammer,
)

from tests.integration.services.test_production_controller import (  # noqa: F401 - fixture
    JIG,
    STATION,
    TEMP_BASE,
    ports,
    run_until,
    session_factory,
)


class FlakyProgrammer:
    """SimulatedProgrammer that fails (stage, slot) for its first N calls."""

    def __init__(self, fail_first: dict[tuple[str, int], int]) -> None:
        self._fail_first = dict(fail_first)
        self.calls: Counter[tuple[str, int]] = Counter()
        self._good = SimulatedProgrammer(delay_seconds=0, failures=frozenset())

    def _call(self, stage: str, target, **kwargs):
        key = (stage, target.slot_number)
        self.calls[key] += 1
        failing = self.calls[key] <= self._fail_first.get(key, 0)
        failures = frozenset({(stage, target.slot_number)}) if failing else frozenset()
        simulator = SimulatedProgrammer(delay_seconds=0, failures=failures)
        return getattr(simulator, {
            "stock_program": "program_stock",
            "pricol_program": "program_pricol",
            "stock_readback": "verify_stock_mac",
            "pricol_readback": "verify_pricol_mac",
        }[stage])(target, **kwargs)

    def program_stock(self, target, **kwargs):
        return self._call("stock_program", target, **kwargs)

    def program_pricol(self, target, **kwargs):
        return self._call("pricol_program", target, **kwargs)

    def verify_stock_mac(self, target, **kwargs):
        return self._call("stock_readback", target, **kwargs)

    def verify_pricol_mac(self, target, **kwargs):
        return self._call("pricol_readback", target, **kwargs)


class FlakyRig:
    """Fails RF on the given slots for their first N tests; records requests."""

    def __init__(self, fail_first: dict[int, int]) -> None:
        self._fail_first = dict(fail_first)
        self.requests: list[tuple[int, ...]] = []
        self.calls: Counter[int] = Counter()

    def run_test(self, *, targets, **kwargs):
        self.requests.append(tuple(target.slot_number for target in targets))
        failing = set()
        for target in targets:
            self.calls[target.slot_number] += 1
            if self.calls[target.slot_number] <= self._fail_first.get(target.slot_number, 0):
                failing.add(("rf", target.slot_number))
        return SimulatedGoldenRig(delay_seconds=0, failures=frozenset(failing)).run_test(targets=targets, **kwargs)


def make(session_factory, *, programmer=None, rig=None, attempts=3):
    return ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=programmer or FlakyProgrammer({}),
        rf_tester=rig or FlakyRig({}),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=frozenset()),
        temporary_stock_mac_base=TEMP_BASE,
        stage_attempts=attempts,
        retry_pause_seconds=0,
    )


def retry_events(session_factory, batch_id):
    with session_factory() as session:
        rows = session.scalars(
            select(ManufacturingEventModel)
            .where(ManufacturingEventModel.batch_id == batch_id, ManufacturingEventModel.event_type == "STAGE_RETRY")
            .order_by(ManufacturingEventModel.id)
        ).all()
        return [(row.slot_number, row.payload["data"]["stage"], row.payload["data"]["attempt"]) for row in rows]


@pytest.mark.integration
def test_stock_programming_retries_only_the_failed_slot(session_factory):
    programmer = FlakyProgrammer({("stock_program", 2): 2})
    controller = make(session_factory, programmer=programmer)
    batch = controller.start_batch(occupied=(1, 2, 3), ports=ports(1, 2, 3), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    # Slot 2 passed on its third attempt; slots 1 and 3 were programmed once.
    assert programmer.calls[("stock_program", 1)] == 1
    assert programmer.calls[("stock_program", 2)] == 3
    assert programmer.calls[("stock_program", 3)] == 1
    assert all(batch.slots[n].state is DeviceState.STOCK_READBACK_VERIFIED for n in (1, 2, 3))
    assert retry_events(session_factory, batch.batch_id) == [
        (2, "STOCK_PROGRAMMING", 1),
        (2, "STOCK_PROGRAMMING", 2),
    ]


@pytest.mark.integration
def test_slot_failing_every_attempt_fails_after_three(session_factory):
    programmer = FlakyProgrammer({("stock_program", 2): 99})
    controller = make(session_factory, programmer=programmer)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    assert programmer.calls[("stock_program", 2)] == 3
    assert batch.slots[2].state is not DeviceState.STOCK_READBACK_VERIFIED
    assert batch.slots[1].state is DeviceState.STOCK_READBACK_VERIFIED
    # Readback is not attempted on a slot whose programming failed.
    assert programmer.calls[("stock_readback", 2)] == 0
    assert len(retry_events(session_factory, batch.batch_id)) == 2


@pytest.mark.integration
def test_one_attempt_setting_disables_retries(session_factory):
    programmer = FlakyProgrammer({("stock_program", 1): 1})
    controller = make(session_factory, programmer=programmer, attempts=1)
    batch = controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.QR_SCANNING)

    assert programmer.calls[("stock_program", 1)] == 1
    assert retry_events(session_factory, batch.batch_id) == []


@pytest.mark.integration
def test_stock_readback_mismatch_is_read_again(session_factory):
    programmer = FlakyProgrammer({("stock_readback", 1): 1})
    controller = make(session_factory, programmer=programmer)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    assert programmer.calls[("stock_readback", 1)] == 2
    assert programmer.calls[("stock_readback", 2)] == 1
    # The function that failed is repeated, not the programming before it.
    assert programmer.calls[("stock_program", 1)] == 1
    assert batch.slots[1].state is DeviceState.STOCK_READBACK_VERIFIED
    assert retry_events(session_factory, batch.batch_id) == [(1, "STOCK_MAC_READBACK", 1)]


@pytest.mark.integration
def test_rf_test_is_repeated_for_failed_slots_only(session_factory):
    rig = FlakyRig({2: 1, 3: 2})
    controller = make(session_factory, rig=rig)
    batch = controller.start_batch(occupied=(1, 2, 3), ports=ports(1, 2, 3), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.QR_SCANNING)

    assert rig.requests == [(1, 2, 3), (2, 3), (3,)]
    assert all(batch.slots[n].state is DeviceState.STOCK_RF_CONFIRMED for n in (1, 2, 3))
    with session_factory() as session:
        results = {
            row.slot_number: row.payload["data"]
            for row in session.scalars(
                select(ManufacturingEventModel).where(
                    ManufacturingEventModel.batch_id == batch.batch_id,
                    ManufacturingEventModel.event_type == "STOCK_RF_RESULT",
                )
            )
        }
    assert {n: results[n]["outcome"] for n in results} == {1: "PASS", 2: "PASS", 3: "PASS"}
    assert {n: results[n]["attempt"] for n in results} == {1: 1, 2: 2, 3: 3}


@pytest.mark.integration
def test_rf_slot_failing_every_attempt_is_failed_and_others_continue(session_factory):
    rig = FlakyRig({2: 99})
    controller = make(session_factory, rig=rig)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)

    run_until(controller, BatchState.PRICOL_MACS_RESERVED)

    assert rig.requests == [(1, 2), (2,), (2,)]
    # Slot 1 goes on to Pricol; slot 2 failed RF and gets no Pricol MAC.
    assert batch.slots[1].state is DeviceState.PRICOL_MAC_RESERVED
    assert batch.slots[2].state is not DeviceState.PRICOL_MAC_RESERVED
    assert batch.slots[2].pricol_mac is None


class RigDownOnRetry(FlakyRig):
    def run_test(self, **kwargs):
        if self.requests:
            self.requests.append(("down",))
            raise ConnectionError("rig unreachable")
        return super().run_test(**kwargs)


@pytest.mark.integration
def test_rf_retry_that_cannot_run_keeps_first_results(session_factory):
    rig = RigDownOnRetry({2: 99})
    controller = make(session_factory, rig=rig)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.QR_SCANNING)

    assert batch.slots[1].state is DeviceState.STOCK_RF_CONFIRMED
    assert batch.slots[2].state is not DeviceState.STOCK_RF_CONFIRMED


@pytest.mark.integration
def test_pricol_programming_and_readback_retry_failed_slots(session_factory):
    programmer = FlakyProgrammer({("pricol_program", 1): 1, ("pricol_readback", 2): 2})
    controller = make(session_factory, programmer=programmer)
    batch = controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_AND_PRICOL)

    run_until(controller, BatchState.PRICOL_READBACK_VERIFIED)

    assert programmer.calls[("pricol_program", 1)] == 2
    assert programmer.calls[("pricol_program", 2)] == 1
    assert programmer.calls[("pricol_readback", 2)] == 3
    assert programmer.calls[("pricol_readback", 1)] == 1
    assert sorted(retry_events(session_factory, batch.batch_id)) == [
        (1, "PRICOL_PROGRAMMING", 1),
        (2, "PRICOL_MAC_READBACK", 1),
        (2, "PRICOL_MAC_READBACK", 2),
    ]


class RaisingAttempts(FlakyProgrammer):
    """A Supervisor raises the attempts setting during the first attempt."""

    def __init__(self, fail_first, setting: dict) -> None:
        super().__init__(fail_first)
        self._setting = setting

    def program_stock(self, target, **kwargs):
        self._setting["attempts"] = 5
        return super().program_stock(target, **kwargs)


@pytest.mark.integration
def test_attempts_change_applies_to_the_step_already_running(session_factory):
    setting = {"attempts": 3}
    programmer = RaisingAttempts({("stock_program", 1): 4}, setting)
    controller = make(session_factory, programmer=programmer, attempts=lambda: setting["attempts"])
    batch = controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    # Started with 3 attempts; raised to 5, so the 5th attempt passed.
    assert programmer.calls[("stock_program", 1)] == 5
    assert batch.slots[1].state is DeviceState.STOCK_READBACK_VERIFIED


@pytest.mark.integration
def test_attempts_lowered_mid_step_stops_retrying(session_factory):
    setting = {"attempts": 5}
    programmer = FlakyProgrammer({("stock_program", 1): 99})
    original = programmer.program_stock

    def lower_after_two(target, **kwargs):
        if programmer.calls[("stock_program", 1)] >= 1:
            setting["attempts"] = 2
        return original(target, **kwargs)

    programmer.program_stock = lower_after_two
    controller = make(session_factory, programmer=programmer, attempts=lambda: setting["attempts"])
    controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)

    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    assert programmer.calls[("stock_program", 1)] == 2
