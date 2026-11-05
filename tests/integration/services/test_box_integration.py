"""Station <-> box (SDK 7.0) against the in-memory demo box.

Follows docs/box_integration.md and the SDK's STATION-APP-REQUIREMENTS
(R4-R17): blocks and promotes, temporary Stock MACs, one report per
finished tray, the box going away, sessions ending, holds, a QR scanned
again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import unisemble_client.client as sdk_client
from sqlalchemy import select

from stationapp.data.base import Base
from stationapp.data.box_block_repository import BoxBlockRepository
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.data.models import AllocatedMacModel, BatchModel, ModuleQrRegistryModel, OutboxModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState, ProgrammingMode
from stationapp.domain.mac import TEMPORARY_STOCK_MACS, MacAddress, MacPurpose, MacStatus
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import AuthenticatedUser, AuthenticationFailed, AuthenticationMethod, AuthorizationError, PinChangeRequired, UserRole
from stationapp.drivers.box.demo import DEMO_PRICOL_RANGE, DEMO_STOCK_RANGE, DemoBox
from stationapp.services.access import StationAccessService
from stationapp.services.batch_codes import BatchCodeService
from stationapp.services.box_auth import BoxAuthenticationService
from stationapp.services.box_blocks import BoxBlockService
from stationapp.services.box_reporting import BoxReportService
from stationapp.services.production import ProductionController, ProductionError, SlotPort
from stationapp.services.simulation import SimulatedFunctionalTester, SimulatedGoldenRig, SimulatedProgrammer

STATION = "STATION-01"
JIG = "JIG-01"
TEMP_BASE = MacAddress.parse(TEMPORARY_STOCK_MACS[0])     # built-in list, slot 1
OPERATOR = ("9000000001", "123456")
SUPERVISOR = ("9000000003", "246810")
ADMIN = ("9000000004", "135790")


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    # The SDK backs off 1-4 s between retries; tests do not wait.
    monkeypatch.setattr(sdk_client, "_sleep", lambda _seconds: None)


@pytest.fixture
def station(tmp_path: Path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    box = DemoBox()
    gateway = box.gateway()
    blocks = BoxBlockService(
        session_factory=factory,
        gateway=gateway,
        station_id=STATION,
        jig_id=JIG,
        master_ranges={MacPurpose.STOCK_RF_TEST: DEMO_STOCK_RANGE, MacPurpose.PRICOL_PRODUCTION: DEMO_PRICOL_RANGE},
        block_size=100,
        low_water=8,
    )
    reports = BoxReportService(session_factory=factory, station_id=STATION, gateway=gateway, blocks=blocks)
    auth = BoxAuthenticationService(gateway=gateway, session_factory=factory, station_id=STATION, mode_description="demo", test_users=True)
    access = StationAccessService(
        session_factory=factory,
        station_id=STATION,
        authentication=auth,
        configuration_provider=lambda: StationConfiguration(JIG, 8, tuple((n, f"USB:{n}") for n in range(1, 9)), "RIG", "a" * 64),
    )
    reports.set_session_listener(access.box_session_ended)
    batch_codes = BatchCodeService(session_factory=factory, station_id=STATION, access=access)
    yield {"factory": factory, "box": box, "gateway": gateway, "blocks": blocks, "reports": reports, "access": access, "batch_codes": batch_codes}
    engine.dispose()


def controller_for(station, *, failures: frozenset = frozenset(), qr_reuse=None) -> ProductionController:
    # Trays get Batch Codes once a session is open (login_operator).
    batch_codes = station["batch_codes"] if station["access"].session is not None else None
    return ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(station["factory"]),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=failures),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=failures),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=failures),
        qr_reuse=qr_reuse,
        batch_codes=batch_codes,
    )


def ports(*slots: int) -> dict[int, SlotPort]:
    return {n: SlotPort(n, f"USB:{n}", f"COM{10 + n}") for n in slots}


def start_tray(controller: ProductionController, mode: ProgrammingMode, slots: tuple[int, ...]):
    """Start a tray and run it up to QR scanning."""
    controller.start_batch(occupied=slots, ports=ports(*slots), mode=mode)
    batch = controller.batch
    for _ in range(30):
        if batch.state is BatchState.QR_SCANNING:
            break
        controller.advance()
    return batch


def run_tray(controller: ProductionController, mode: ProgrammingMode, slots: tuple[int, ...], qr_prefix: str):
    batch = start_tray(controller, mode, slots)
    for slot in slots:
        controller.scan_qr(f"{qr_prefix}{slot:02d}")
    controller.advance()
    assert batch.state is BatchState.UPLOAD_PENDING
    controller.release_batch()
    return batch


def login_operator(station, *, threshold: int = 1000, series: str = "TST01") -> None:
    access = station["access"]
    access.login(*SUPERVISOR)
    if access.session is None:
        access.start_session(access.current_configuration(), batch_code_threshold=threshold)
    access.logout_current()
    access.login(*OPERATOR)
    if not station["batch_codes"].current().ready:
        station["batch_codes"].start_series(series)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_current_then_reserve_block_at_low_water(station):
    blocks, box = station["blocks"], station["box"]

    status = blocks.ensure(MacPurpose.PRICOL_PRODUCTION)
    state = blocks.state(MacPurpose.PRICOL_PRODUCTION)
    assert status.available == 100 and state.current is not None and state.reserve is None
    assert box.blocks[0]["range"] == DEMO_PRICOL_RANGE

    blocks.ensure(MacPurpose.PRICOL_PRODUCTION)
    assert len(box.blocks) == 1                      # not low yet: no second request

    with station["factory"]() as session:            # use the block down to low water
        rows = session.scalars(select(AllocatedMacModel).order_by(AllocatedMacModel.id).limit(92)).all()
        for row in rows:
            row.status = MacStatus.CONFIRMED.value
        session.commit()
    blocks.ensure(MacPurpose.PRICOL_PRODUCTION)
    state = blocks.state(MacPurpose.PRICOL_PRODUCTION)
    assert state.reserve is not None and state.available == 8 + 100
    assert len(box.blocks) == 2


@pytest.mark.integration
def test_tray_never_spans_blocks_switch_and_promote(station):
    blocks, box, factory = station["blocks"], station["box"], station["factory"]
    blocks.ensure(MacPurpose.STOCK_RF_TEST)
    first = blocks.state(MacPurpose.STOCK_RF_TEST).current
    with factory() as session:                       # 5 addresses left in the current block
        for row in session.scalars(select(AllocatedMacModel).order_by(AllocatedMacModel.id).limit(95)):
            row.status = MacStatus.CONFIRMED.value
        session.commit()
    blocks.ensure(MacPurpose.STOCK_RF_TEST)          # reserve fetched
    reserve = blocks.state(MacPurpose.STOCK_RF_TEST).reserve

    controller = controller_for(station)
    controller.start_batch(occupied=tuple(range(1, 9)), ports=ports(*range(1, 9)), mode=ProgrammingMode.STOCK_ONLY)
    macs = [controller.batch.slots[n].stock_mac for n in range(1, 9)]

    # All 8 from the reserve, bottom up; the 5 left in the old block are abandoned.
    assert macs == [MacAddress(reserve.first_mac.value + i) for i in range(8)]
    state = blocks.state(MacPurpose.STOCK_RF_TEST)
    assert state.current.block_id == reserve.block_id and state.reserve is None
    assert state.owed_promote == (first.block_id, reserve.block_id)
    assert state.available == 92                     # the old block's 5 are not counted

    requests = box.calls.count("/blocks/request")
    blocks.ensure(MacPurpose.STOCK_RF_TEST)          # the owed promote goes out first
    assert box.calls[-1] == "/blocks/promote"
    assert box.calls.count("/blocks/request") == requests    # 92 left: no new block needed
    assert blocks.state(MacPurpose.STOCK_RF_TEST).owed_promote is None
    assert [b["state"] for b in box.blocks] == ["closed", "open"]


@pytest.mark.integration
def test_owed_promote_survives_box_outage_and_blocks_new_requests(station):
    blocks, box, factory = station["blocks"], station["box"], station["factory"]
    blocks.ensure(MacPurpose.STOCK_RF_TEST)
    with factory() as session:
        for row in session.scalars(select(AllocatedMacModel).order_by(AllocatedMacModel.id).limit(96)):
            row.status = MacStatus.CONFIRMED.value
        session.commit()
    blocks.ensure(MacPurpose.STOCK_RF_TEST)
    controller_for(station).start_batch(occupied=tuple(range(1, 9)), ports=ports(*range(1, 9)), mode=ProgrammingMode.STOCK_ONLY)
    assert blocks.state(MacPurpose.STOCK_RF_TEST).owed_promote is not None

    box.down = True
    status = blocks.ensure(MacPurpose.STOCK_RF_TEST)
    assert "not answering" in status.message
    # A fresh service (restart) still owes it, and sends it first.
    box.down = False
    requests_before = box.calls.count("/blocks/request")
    restarted = BoxBlockService(session_factory=factory, gateway=station["gateway"], station_id=STATION, jig_id=JIG,
                                master_ranges={MacPurpose.STOCK_RF_TEST: DEMO_STOCK_RANGE}, block_size=100, low_water=8)
    restarted.send_owed_promotes()
    assert restarted.state(MacPurpose.STOCK_RF_TEST).owed_promote is None
    assert box.calls.count("/blocks/request") == requests_before


@pytest.mark.integration
def test_lost_state_redraws_and_closes_blocks_the_box_still_holds(station):
    box, blocks = station["box"], station["blocks"]
    # The box believes this station holds a current and a reserve block that
    # the station no longer knows about (state lost or restored, R17).
    other = box.gateway()
    old_open = other.request_block(100, DEMO_STOCK_RANGE)
    old_reserve = other.request_block(100, DEMO_STOCK_RANGE)

    blocks.ensure(MacPurpose.STOCK_RF_TEST)

    state = blocks.state(MacPurpose.STOCK_RF_TEST)
    assert state.current is not None
    assert state.current.block_id not in (old_open.block_id, old_reserve.block_id)
    assert next(b for b in box.blocks if b["id"] == old_open.block_id)["state"] == "closed"


def use_up(station, purpose: MacPurpose, leave: int) -> None:
    """Mark the current block's addresses used until `leave` are left."""
    current = station["blocks"].state(purpose).current
    with station["factory"]() as session:
        rows = session.scalars(
            select(AllocatedMacModel)
            .where(AllocatedMacModel.allocation_id == f"BOX-{current.block_id}", AllocatedMacModel.status == MacStatus.AVAILABLE.value)
            .order_by(AllocatedMacModel.id)
            .limit(current.available - leave)
        ).all()
        for row in rows:
            row.status = MacStatus.CONFIRMED.value
        session.commit()


@pytest.mark.integration
def test_mode_change_keeps_one_block_pair_at_the_box(station):
    blocks, box = station["blocks"], station["box"]
    stock, pricol = MacPurpose.STOCK_RF_TEST, MacPurpose.PRICOL_PRODUCTION

    def open_at_box():
        return [(b["id"], b["range"], b["state"]) for b in box.blocks if b["state"] != "closed"]

    blocks.ensure(pricol)
    use_up(station, pricol, 8)
    blocks.ensure(pricol)
    assert blocks.state(pricol).reserve is not None

    # Mode changed to Stock Only: the box holds this station's Pricol pair
    # and refuses a third block; the pair is promoted and a Stock block drawn.
    status = blocks.ensure(stock)
    assert blocks.state(stock).current is not None and "call support" not in status.message
    assert blocks.state(pricol).current is None and blocks.state(pricol).reserve is None
    assert blocks.available(pricol) == 0                        # the old blocks are never used again
    assert len(open_at_box()) == 2

    # The Stock reserve: the box still files the Pricol block as open.
    use_up(station, stock, 8)
    blocks.ensure(stock)
    state = blocks.state(stock)
    assert state.reserve is not None
    assert {block_id for block_id, _, _ in open_at_box()} == {state.current.block_id, state.reserve.block_id}

    # A Stock Only tray runs from the Stock block.
    login_operator(station)
    batch = run_tray(controller_for(station), ProgrammingMode.STOCK_ONLY, (1,), "MQR")
    assert state.current.first_mac.value <= batch.slots[1].stock_mac.value <= state.current.last_mac.value

    # And back to Stock + Pricol.
    blocks.ensure(pricol)
    assert blocks.state(pricol).current is not None and blocks.state(stock).current is None
    assert len(open_at_box()) == 2
    assert blocks.state(pricol).current.block_id in {block_id for block_id, _, _ in open_at_box()}


@pytest.mark.integration
def test_mode_change_while_box_is_down_keeps_the_old_blocks(station):
    blocks, box = station["blocks"], station["box"]
    blocks.ensure(MacPurpose.PRICOL_PRODUCTION)
    held = blocks.state(MacPurpose.PRICOL_PRODUCTION).current

    box.down = True
    status = blocks.ensure(MacPurpose.STOCK_RF_TEST)
    assert status.available == 0 and "not answering" in status.message
    assert blocks.state(MacPurpose.PRICOL_PRODUCTION).current == held      # nothing lost


@pytest.mark.integration
def test_blocks_from_another_box_are_never_used(station):
    blocks = station["blocks"]
    blocks.bind_to_box("DEMO")
    blocks.ensure(MacPurpose.STOCK_RF_TEST)
    demo_block = blocks.state(MacPurpose.STOCK_RF_TEST).current

    blocks.bind_to_box("DEMO")                                  # same box: nothing changes
    assert blocks.state(MacPurpose.STOCK_RF_TEST).current == demo_block

    blocks.bind_to_box("http://real-box:3001")                  # switched to the real box
    assert blocks.state(MacPurpose.STOCK_RF_TEST).current is None
    assert blocks.available(MacPurpose.STOCK_RF_TEST) == 0
    with pytest.raises(Exception, match="reservation failed"):
        controller_for(station).start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)


@pytest.mark.integration
def test_saving_a_block_twice_is_refused(station):
    repository = BoxBlockRepository(station["factory"])
    kwargs = dict(master_range="R", first_mac="0A0000000000", last_mac="0A0000000009", size=10, purpose=MacPurpose.STOCK_RF_TEST, station_id=STATION, jig_id=JIG)
    repository.add(block_id=1, **kwargs)
    with pytest.raises(Exception, match="overlaps"):
        repository.add(block_id=2, **kwargs)


# ---------------------------------------------------------------------------
# Trays and reports
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_stock_and_pricol_tray_uses_temporary_stock_macs_and_reports_pricol_macs(station):
    station["blocks"].ensure(MacPurpose.PRICOL_PRODUCTION)
    login_operator(station)
    controller = controller_for(station)

    batch = run_tray(controller, ProgrammingMode.STOCK_AND_PRICOL, (1, 3), "PQR")

    # Temporary Stock MAC per slot; never a cache row.
    assert batch.slots[1].stock_mac == TEMP_BASE
    assert batch.slots[3].stock_mac == MacAddress(TEMP_BASE.value + 2)
    with station["factory"]() as session:
        assert session.scalar(select(AllocatedMacModel).where(AllocatedMacModel.address == TEMP_BASE.compact)) is None
    # No Stock block was ever requested in this mode.
    assert all(block["range"] == DEMO_PRICOL_RANGE for block in station["box"].blocks)

    run = station["reports"].report("TEST")
    assert run.success and run.modules == 2
    recorded = {entry["qr"]: mac for mac, entry in station["box"].modules.items()}
    assert recorded == {"PQR01": batch.slots[1].pricol_mac.value, "PQR03": batch.slots[3].pricol_mac.value}

    with station["factory"]() as session:
        assert session.scalar(select(OutboxModel)).status == "UPLOADED"
        assert session.get(BatchModel, batch.batch_id).state == "UPLOADED"
        assert session.get(ModuleQrRegistryModel, "PQR01").server_status == "SYNCED"


@pytest.mark.integration
def test_stock_only_reports_stock_mac_and_failed_modules(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    controller = controller_for(station, failures=frozenset({("rf", 2)}))

    batch = run_tray(controller, ProgrammingMode.STOCK_ONLY, (1, 2), "SQR")
    station["reports"].report("TEST")

    box = station["box"]
    assert box.modules[batch.slots[1].stock_mac.value] == {"qr": "SQR01", "passed": True, "state": "confirmed", "batch_id": "TST01"}
    # A module that had a box address programmed but failed is reported too (R11).
    assert box.modules[batch.slots[2].stock_mac.value]["passed"] is False


@pytest.mark.integration
def test_box_unreachable_keeps_trays_and_reports_them_together_later(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    controller = controller_for(station)
    box, reports = station["box"], station["reports"]

    box.down = True
    run_tray(controller, ProgrammingMode.STOCK_ONLY, (1,), "AQR")
    run = reports.report("TEST")
    assert not run.success and "not answering" in run.detail
    run_tray(controller, ProgrammingMode.STOCK_ONLY, (2,), "BQR")    # the line keeps running (R13)

    status = reports.status()
    assert status.pending_modules == 2 and status.pending_batches == 2

    box.down = False
    run = reports.report("RETRY")
    assert run.success and run.modules == 2 and run.batches == 2
    assert reports.status().pending_modules == 0


@pytest.mark.integration
def test_session_ended_by_box_logs_operator_out_and_reports_after_login(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    run_tray(controller_for(station), ProgrammingMode.STOCK_ONLY, (1,), "EQR")
    station["box"].end_session()                                 # idle / shift end on the box

    run = station["reports"].report("TEST")
    assert run.login_required and not run.success
    assert station["access"].operator is None                   # asked to log in again
    assert station["reports"].status().pending_modules == 1     # kept (R12)

    station["access"].login(*OPERATOR)
    assert station["reports"].report("LOGIN").success


@pytest.mark.integration
def test_rejected_module_holds_the_line_until_a_supervisor_clears_it(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    box, reports = station["box"], station["reports"]
    # The block's first address is already on another module at the box.
    box.modules[box.blocks[0]["start"]] = {"qr": "SOMEONE-ELSE", "passed": True, "state": "confirmed"}

    run_tray(controller_for(station), ProgrammingMode.STOCK_ONLY, (1,), "HQR")
    run = reports.report("TEST")
    assert run.success and run.rejected == 1

    hold = reports.hold()
    assert hold.on_hold and hold.rejected[0].reason == "duplicate"
    assert "already on another module" in hold.rejected[0].words
    with station["factory"]() as session:
        assert session.get(ModuleQrRegistryModel, "HQR01").server_status == "CONFLICT"

    # The hold survives a restart (R15).
    restarted = BoxReportService(session_factory=station["factory"], station_id=STATION, gateway=station["gateway"])
    assert restarted.hold().on_hold

    with pytest.raises(AuthenticationFailed, match="not recognised"):
        reports.clear_hold(SUPERVISOR[0], "000000")
    with pytest.raises(AuthorizationError, match="cannot clear holds"):
        reports.clear_hold(*OPERATOR)                               # not a supervisor (R16)
    assert reports.hold().on_hold

    reports.clear_hold(*SUPERVISOR)
    assert not reports.hold().on_hold and not box.holds
    assert box.cleared_by == ["Demo Supervisor"]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_box_roles_map_to_station_roles(station):
    auth = station["access"].authentication
    assert auth.authenticate(*OPERATOR, purpose="t").role is UserRole.OPERATOR
    assert auth.authenticate(*SUPERVISOR, purpose="t").role is UserRole.SUPERVISOR
    assert auth.authenticate(*ADMIN, purpose="t").role is UserRole.SUPERVISOR


@pytest.mark.integration
def test_forced_pin_change(station):
    access = station["access"]
    with pytest.raises(PinChangeRequired):
        access.login("9000000002", "000000")
    with pytest.raises(AuthenticationFailed, match="6 digits"):
        access.login_with_new_pin("9000000002", "000000", "12")
    access.login(*SUPERVISOR)
    access.start_session(access.current_configuration())
    access.logout_current()
    with pytest.raises(PinChangeRequired):
        access.login("9000000002", "000000")
    user = access.login_with_new_pin("9000000002", "000000", "424242")
    assert user.role is UserRole.OPERATOR and access.operator is not None


@pytest.mark.integration
def test_first_time_supervisor_pin_changed_from_an_authorization_dialog(station):
    """A Supervisor's first use may be an approval (HOLD, Batch Code ...)."""
    access, box, gateway = station["access"], station["box"], station["gateway"]
    login_operator(station)
    box.people[SUPERVISOR[0]]["must_change"] = True

    with pytest.raises(PinChangeRequired):
        access.authorize_supervisor(*SUPERVISOR)
    with pytest.raises(AuthenticationFailed, match="differ"):
        access.authorize_supervisor(*SUPERVISOR, new_pin=SUPERVISOR[1])
    supervisor = access.authorize_supervisor(*SUPERVISOR, new_pin="135135")

    assert supervisor.is_supervisor and gateway.holder.user_id == "SUP-1"
    assert box.people[SUPERVISOR[0]] == {**box.people[SUPERVISOR[0]], "pin": "135135", "must_change": False}
    # From now on the box checks the new PIN only.
    with pytest.raises(AuthenticationFailed, match="not recognised"):
        access.authorize_supervisor(*SUPERVISOR)
    assert access.authorize_supervisor(SUPERVISOR[0], "135135").user_id == "SUP-1"


@pytest.mark.integration
def test_first_time_operator_pin_changed_from_the_operator_login_dialog(station):
    access, box = station["access"], station["box"]
    login_operator(station)
    access.logout_current()
    with pytest.raises(PinChangeRequired):
        access.login_operator("9000000002", "000000")
    with pytest.raises(AuthenticationFailed, match="not recognised"):
        access.login_operator("9000000002", "999999", new_pin="424242")   # wrong current PIN
    user = access.login_operator("9000000002", "000000", new_pin="424242")
    assert access.operator == user and box.people["9000000002"]["pin"] == "424242"


@pytest.mark.integration
def test_supervisor_login_takes_the_box_session_from_the_operator(station):
    access, gateway = station["access"], station["gateway"]
    login_operator(station)
    assert gateway.holder.user_id == "OP-1"

    supervisor = access.authorize_supervisor(*SUPERVISOR)          # e.g. a HOLD disposition
    assert supervisor.is_supervisor
    assert gateway.holder.user_id == "SUP-1"
    assert access.operator is None                                 # must log in again


@pytest.mark.integration
def test_operator_logout_reports_first_then_logs_out_of_the_box(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    run_tray(controller_for(station), ProgrammingMode.STOCK_ONLY, (1,), "LQR")
    reports = station["reports"]
    station["access"].authentication.set_before_logout(lambda _user: reports.report("LOGOUT"))

    station["access"].logout_operator()

    assert reports.status().pending_modules == 0
    assert station["gateway"].holder is None and station["box"].session is None


# ---------------------------------------------------------------------------
# A QR scanned again (same QR, new MAC)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_qr_scanned_again_holds_the_station_until_a_supervisor_clears_it(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    reports, box = station["reports"], station["box"]
    controller = controller_for(station, qr_reuse=reports)

    first = run_tray(controller, ProgrammingMode.STOCK_ONLY, (1,), "RW")
    old_mac = first.slots[1].stock_mac
    assert reports.report("TEST").success

    # The module comes back for re-work and gets a new MAC in a new tray.
    batch = start_tray(controller, ProgrammingMode.STOCK_ONLY, (1, 2))
    with pytest.raises(ProductionError, match="STATION ON HOLD"):
        controller.scan_qr("RW01")
    hold = reports.hold()
    assert hold.on_hold and not hold.box_on_hold
    assert hold.reused_qrs[0].qr == "RW01" and first.batch_id in hold.reused_qrs[0].previous
    assert batch.slots[1].module_qr is None

    # Scanning stops, whatever is scanned; the hold survives a restart.
    with pytest.raises(ProductionError, match="STATION ON HOLD"):
        controller.scan_qr("RW99")
    restarted = BoxReportService(session_factory=station["factory"], station_id=STATION, gateway=station["gateway"])
    assert restarted.hold().reused_qrs

    with pytest.raises(AuthorizationError):
        reports.clear_hold(*OPERATOR)
    assert reports.scanning_stopped()
    reports.clear_hold(*ADMIN)                                  # Admin: same as a Supervisor
    assert not reports.hold().on_hold

    # Scanned again after the clear: bound as a re-worked module.
    assert controller.scan_qr("RW01") == 1
    controller.scan_qr("RW02")
    controller.advance()
    controller.release_batch()
    assert not reports.reuse_approved("RW01")                   # approval used up
    assert batch.slots[1].stock_mac != old_mac

    assert reports.report("TEST").success
    assert box.modules[batch.slots[1].stock_mac.value]["qr"] == "RW01"
    assert box.modules[old_mac.value]["state"] == "unused"       # the box: re-programmed
    with station["factory"]() as session:
        entry = session.get(ModuleQrRegistryModel, "RW01")
        assert entry.batch_id == batch.batch_id and entry.server_status == "SYNCED"


@pytest.mark.integration
def test_qr_scanned_twice_in_one_tray_is_refused_without_a_hold(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    reports = station["reports"]
    controller = controller_for(station, qr_reuse=reports)

    start_tray(controller, ProgrammingMode.STOCK_ONLY, (1, 2))
    controller.scan_qr("TWICE")
    with pytest.raises(ProductionError):
        controller.scan_qr("TWICE")
    assert not reports.hold().on_hold


# ---------------------------------------------------------------------------
# Restarting a tray: its MACs are kept until it is reported
# ---------------------------------------------------------------------------


def tray_macs(factory, batch_id: str) -> dict[tuple[str, int], tuple[str, str]]:
    """(purpose, slot) -> (address, status) of the box MACs held by a tray."""
    with factory() as session:
        rows = session.scalars(select(AllocatedMacModel).where(AllocatedMacModel.batch_id == batch_id)).all()
        return {(row.purpose, row.slot_number): (row.address, row.status) for row in rows}


def used_count(factory, purpose: MacPurpose) -> int:
    with factory() as session:
        return len(session.scalars(select(AllocatedMacModel).where(
            AllocatedMacModel.purpose == purpose.value,
            AllocatedMacModel.status != MacStatus.AVAILABLE.value,
        )).all())


@pytest.mark.integration
def test_restart_during_qr_scanning_programs_the_same_macs_again(station):
    """A QR label that cannot be read: restart, replace the label, rescan."""
    station["blocks"].ensure(MacPurpose.PRICOL_PRODUCTION)
    login_operator(station)
    reports, box, factory = station["reports"], station["box"], station["factory"]
    controller = controller_for(station, qr_reuse=reports)

    batch = start_tray(controller, ProgrammingMode.STOCK_AND_PRICOL, (1, 2))
    pricol = {n: batch.slots[n].pricol_mac for n in (1, 2)}
    controller.scan_qr("RS01")                       # slot 2's label cannot be read

    controller.restart_batch()
    assert batch.state is BatchState.STOCK_MACS_RESERVED and batch.restart_count == 1
    assert batch.slots[1].module_qr is None and controller.snapshot().restarts == 1
    assert batch.slots[1].stock_mac == TEMP_BASE      # the temporary Stock MAC again
    assert tray_macs(factory, batch.batch_id)[("PRICOL_PRODUCTION", 1)] == (pricol[1].compact, "RESERVED")

    for _ in range(30):
        if batch.state is BatchState.QR_SCANNING:
            break
        controller.advance()
    assert {n: batch.slots[n].pricol_mac for n in (1, 2)} == pricol
    controller.scan_qr("RS01")                       # scanned before the restart: no hold
    controller.scan_qr("RS02-NEW")
    controller.advance()
    assert batch.state is BatchState.UPLOAD_PENDING
    controller.release_batch()

    assert not reports.hold().on_hold
    assert used_count(factory, MacPurpose.PRICOL_PRODUCTION) == 2    # nothing spent on the restart
    assert reports.report("TEST").success
    assert box.modules[pricol[1].value]["qr"] == "RS01"
    assert box.modules[pricol[2].value]["qr"] == "RS02-NEW"


@pytest.mark.integration
def test_restart_brings_back_an_undetected_slot_and_keeps_the_other_macs(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    factory = station["factory"]
    controller = controller_for(station, failures=frozenset({("absent", 2)}))

    controller.start_batch(occupied=(1, 2, 3), ports=ports(1, 2, 3), mode=ProgrammingMode.STOCK_ONLY)
    batch = controller.batch
    before = {n: batch.slots[n].stock_mac for n in (1, 2, 3)}
    controller.advance()                             # request program mode
    controller.advance()                             # program: slot 2 not detected
    assert batch.occupied_slot_numbers == (1, 3)

    # The module is re-seated; the tray starts again with all three slots.
    controller._programmer = SimulatedProgrammer(delay_seconds=0, failures=frozenset())
    controller.restart_batch()
    assert batch.occupied_slot_numbers == (1, 2, 3)
    assert batch.slots[1].stock_mac == before[1] and batch.slots[3].stock_mac == before[3]
    assert batch.slots[2].stock_mac is not None

    for _ in range(30):
        if batch.state is BatchState.QR_SCANNING:
            break
        controller.advance()
    for n in (1, 2, 3):
        controller.scan_qr(f"UD{n:02d}")
    controller.advance()
    assert batch.state is BatchState.UPLOAD_PENDING
    macs = tray_macs(factory, batch.batch_id)
    assert sorted(macs) == [("STOCK_RF_TEST", 1), ("STOCK_RF_TEST", 2), ("STOCK_RF_TEST", 3)]
    assert {status for _address, status in macs.values()} == {"CONFIRMED"}
    assert used_count(factory, MacPurpose.STOCK_RF_TEST) == 3


@pytest.mark.integration
def test_pricol_mac_not_needed_after_a_restart_is_released_at_commit(station):
    station["blocks"].ensure(MacPurpose.PRICOL_PRODUCTION)
    factory = station["factory"]
    controller = controller_for(station)

    batch = start_tray(controller, ProgrammingMode.STOCK_AND_PRICOL, (1, 2))
    slot2 = batch.slots[2].pricol_mac
    controller.restart_batch()
    assert set(batch.carried_pricol_macs) == {1, 2}

    # Second attempt: slot 2 fails the RF test, so gets no Pricol MAC.
    controller._rf_tester = SimulatedGoldenRig(delay_seconds=0, failures=frozenset({("rf", 2)}))
    for _ in range(30):
        if batch.state is BatchState.QR_SCANNING:
            break
        controller.advance()
    assert set(batch.carried_pricol_macs) == {2}
    controller.scan_qr("PR01")
    controller.scan_qr("PR02")
    controller.advance()
    assert batch.state is BatchState.UPLOAD_PENDING

    assert batch.carried_pricol_macs == {}
    with factory() as session:
        row = session.scalar(select(AllocatedMacModel).where(AllocatedMacModel.address == slot2.compact))
        assert row.status == "AVAILABLE" and row.batch_id is None
    assert used_count(factory, MacPurpose.PRICOL_PRODUCTION) == 1


@pytest.mark.integration
def test_reworked_module_scanned_before_a_restart_needs_no_new_hold(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    reports = station["reports"]
    controller = controller_for(station, qr_reuse=reports)
    run_tray(controller, ProgrammingMode.STOCK_ONLY, (1,), "RR")

    batch = start_tray(controller, ProgrammingMode.STOCK_ONLY, (1, 2))
    with pytest.raises(ProductionError, match="STATION ON HOLD"):
        controller.scan_qr("RR01")
    reports.clear_hold(*SUPERVISOR)
    controller.scan_qr("RR01")                       # approved once, used here

    controller.restart_batch()
    for _ in range(30):
        if batch.state is BatchState.QR_SCANNING:
            break
        controller.advance()
    assert controller.scan_qr("RR01") == 1           # still this tray's module: no hold
    controller.scan_qr("RR02")
    assert not reports.hold().on_hold


@pytest.mark.integration
def test_restart_refused_once_the_tray_is_committed_or_on_hold_without_a_supervisor(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    controller = ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(station["factory"]),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=frozenset()),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=frozenset()),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=frozenset()),
        access=station["access"],
    )
    batch = start_tray(controller, ProgrammingMode.STOCK_ONLY, (1,))
    mac = batch.slots[1].stock_mac
    batch.place_on_hold("RF verification mismatch")
    with pytest.raises(ProductionError, match="Supervisor"):
        controller.restart_batch()

    supervisor = AuthenticatedUser("SUP-1", "Supervisor", UserRole.SUPERVISOR, AuthenticationMethod.PIN, datetime.now(timezone.utc))
    controller.restart_batch(supervisor=supervisor)
    assert batch.state is BatchState.STOCK_MACS_RESERVED and batch.slots[1].stock_mac == mac

    for _ in range(30):
        if batch.state is BatchState.QR_SCANNING:
            break
        controller.advance()
    controller.scan_qr("CM01")
    controller.advance()
    with pytest.raises(ProductionError, match="finished"):
        controller.restart_batch()


# ---------------------------------------------------------------------------
# Batch Codes (SDK 7.0 batch_id)
# ---------------------------------------------------------------------------


def reported_batch_ids(box: DemoBox) -> dict[str, str]:
    return {module["qr"]: module["batch_id"] for module in box.modules.values()}


@pytest.mark.integration
def test_every_module_is_reported_with_its_batch_code_split_exactly(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station, threshold=3, series="S5A01")
    controller = controller_for(station)

    run_tray(controller, ProgrammingMode.STOCK_ONLY, (1, 2, 3, 4), "BA")
    # Slot 2 fails the RF test: reported on the current code, not counted.
    controller._rf_tester = SimulatedGoldenRig(delay_seconds=0, failures=frozenset({("rf", 2)}))
    run_tray(controller, ProgrammingMode.STOCK_ONLY, (1, 2, 3, 5), "BB")
    assert station["reports"].report("TEST").success

    assert reported_batch_ids(station["box"]) == {
        "BA01": "S5A01", "BA02": "S5A01", "BA03": "S5A01", "BA04": "S5A02",
        "BB01": "S5A02", "BB02": "S5A02", "BB03": "S5A02", "BB05": "S5A03",
    }
    state = station["batch_codes"].current()
    assert (state.code, state.passed) == ("S5A03", 1)


@pytest.mark.integration
def test_aborted_tray_never_counts_and_a_restarted_tray_counts_once(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station, threshold=2, series="R01")
    controller = controller_for(station)

    start_tray(controller, ProgrammingMode.STOCK_ONLY, (1, 2))
    controller.abort_batch(reason="jig fault", actor_id="SUP-1")
    controller.release_batch()
    assert station["batch_codes"].current().passed == 0

    batch = start_tray(controller, ProgrammingMode.STOCK_ONLY, (1,))
    controller.restart_batch()
    controller.restart_batch()
    for _ in range(30):
        if batch.state is BatchState.QR_SCANNING:
            break
        controller.advance()
    controller.scan_qr("RT01")
    controller.advance()
    assert batch.state is BatchState.UPLOAD_PENDING
    state = station["batch_codes"].current()
    assert (state.code, state.passed) == ("R01", 1)


@pytest.mark.integration
def test_no_tray_without_a_batch_code_series(station):
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    access = station["access"]
    access.login(*SUPERVISOR)
    access.start_session(access.current_configuration(), batch_code_threshold=50)
    access.logout_current()
    access.login(*OPERATOR)
    controller = controller_for(station)
    with pytest.raises(ProductionError, match="Batch Code starting series"):
        controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)

    station["batch_codes"].start_series("Q01")
    controller.start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)


@pytest.mark.integration
def test_session_without_threshold_cannot_run_trays(station):
    access = station["access"]
    access.login(*SUPERVISOR)
    access.start_session(access.current_configuration())
    access.logout_current()
    access.login(*OPERATOR)
    with pytest.raises(ProductionError, match="threshold"):
        controller_for(station).start_batch(occupied=(1,), ports=ports(1), mode=ProgrammingMode.STOCK_ONLY)


@pytest.mark.integration
def test_only_a_supervisor_starts_a_new_series_with_a_reason(station):
    login_operator(station, threshold=10, series="S01")
    codes, access = station["batch_codes"], station["access"]
    with pytest.raises(AuthorizationError, match="Only a Supervisor"):
        codes.start_series("S50")
    supervisor = access.authorize_supervisor(*SUPERVISOR)
    with pytest.raises(ValueError, match="reason"):
        codes.start_series("S50", supervisor=supervisor)
    state = codes.start_series("S50", supervisor=supervisor, reason="label roll changed")
    assert (state.code, state.passed, state.series_start) == ("S50", 0, "S50")


@pytest.mark.integration
def test_series_ends_with_the_session(station):
    login_operator(station, threshold=10, series="S01")
    access = station["access"]
    access.login(*SUPERVISOR)
    access.close_session("shift end")
    access.start_session(access.current_configuration(), batch_code_threshold=20)
    state = station["batch_codes"].current()
    assert state.threshold == 20 and not state.ready


@pytest.mark.integration
def test_tray_committed_without_batch_codes_is_not_reported(station):
    """A tray queued by a pre-7.0 station: the 7.0 SDK refuses it locally."""
    station["blocks"].ensure(MacPurpose.STOCK_RF_TEST)
    login_operator(station)
    controller = controller_for(station)
    controller._batch_codes = None                   # as committed before SDK 7.0
    run_tray(controller, ProgrammingMode.STOCK_ONLY, (1,), "OLD")
    run = station["reports"].report("TEST")
    assert not run.success and "before Batch Codes" in run.detail
    assert station["box"].modules == {}
