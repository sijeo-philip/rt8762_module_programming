"""USB keyboard-wedge QR / badge scanner: normalisation and input field."""

from __future__ import annotations

import pytest

from stationapp.drivers.qr_scanner import BADGE_FORMAT, MODULE_QR_FORMAT, InvalidScan, ScanFormat
from stationapp.ui.widgets.scanner_input import ScannerLineEdit


# ---------------------------------------------------------------------------
# Normalisation (legacy extract_hex: hex only, upper case, last 8)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("1a2b3c4d", "1A2B3C4D"),
        ("SN: 12-34-AB-CD", "1234ABCD"),
        ("PRICOL/0012AB34CD56", "AB34CD56"),   # longer label -> last 8
        ("  1234abcd\r\n", "1234ABCD"),
    ],
)
def test_module_qr_normalisation_matches_legacy(raw, code):
    assert MODULE_QR_FORMAT.normalise(raw) == code
    assert MODULE_QR_FORMAT.validate(raw) == code


@pytest.mark.unit
def test_incomplete_or_non_hex_scan_is_invalid():
    with pytest.raises(InvalidScan, match="8 hexadecimal characters"):
        MODULE_QR_FORMAT.validate("QR-1001")       # only "1001" is hex
    with pytest.raises(InvalidScan):
        MODULE_QR_FORMAT.validate("ZZZZZZZZ")


@pytest.mark.unit
def test_configurable_and_badge_formats():
    assert ScanFormat(length=12).validate("aa:bb:cc:dd:ee:ff") == "AABBCCDDEEFF"
    assert ScanFormat(length=0, hex_only=False).normalise(" Badge-007\r") == "Badge-007"
    assert BADGE_FORMAT.is_complete("OPB") and not BADGE_FORMAT.is_complete("OP")


# ---------------------------------------------------------------------------
# Scanner input field
# ---------------------------------------------------------------------------


@pytest.fixture
def field(qtbot):
    widget = ScannerLineEdit(MODULE_QR_FORMAT, settle_ms=30)
    qtbot.addWidget(widget)
    scans, rejects = [], []
    widget.scanned.connect(scans.append)
    widget.rejected.connect(rejects.append)
    return widget, scans, rejects


@pytest.mark.unit
def test_scan_burst_without_enter_completes_by_itself(field, qtbot):
    widget, scans, _ = field
    qtbot.keyClicks(widget, "PRICOL/0012AB34CD56")      # scanner burst, no suffix
    qtbot.waitUntil(lambda: scans == ["AB34CD56"], timeout=1_000)
    assert widget.text() == ""                           # ready for the next module


@pytest.mark.unit
def test_scanner_enter_suffix_gives_exactly_one_scan(field, qtbot):
    widget, scans, rejects = field
    qtbot.keyClicks(widget, "1a2b3c4d")
    qtbot.keyClick(widget, "\r")
    qtbot.wait(80)
    assert scans == ["1A2B3C4D"] and rejects == []


@pytest.mark.unit
def test_partial_manual_entry_is_rejected_on_enter(field, qtbot):
    widget, scans, rejects = field
    qtbot.keyClicks(widget, "AB12")
    qtbot.keyClick(widget, "\r")
    assert scans == [] and rejects == ["AB12"]


@pytest.mark.unit
def test_non_hex_keystrokes_never_appear(field, qtbot):
    widget, _, _ = field
    widget.setText("qr-12")
    assert widget.text() == "12"


@pytest.mark.unit
def test_badge_completes_on_idle_or_enter(qtbot):
    widget = ScannerLineEdit(BADGE_FORMAT, settle_ms=30, idle_complete=True)
    qtbot.addWidget(widget)
    scans = []
    widget.scanned.connect(scans.append)

    qtbot.keyClicks(widget, "OPBADGE001")
    qtbot.waitUntil(lambda: scans == ["OPBADGE001"], timeout=1_000)

    qtbot.keyClicks(widget, "SUP-42")
    qtbot.keyClick(widget, "\r")
    assert scans == ["OPBADGE001", "SUP-42"]


# ---------------------------------------------------------------------------
# Service layer and login screen
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_controller_normalises_and_validates_scans(tmp_path):
    from stationapp.data.base import Base
    from stationapp.data.database import create_session_factory, create_station_engine
    from stationapp.data.production_repository import SqlProductionStore
    from stationapp.domain.batch import BatchState, ProgrammingMode
    from stationapp.domain.mac import MacPurpose
    from stationapp.services.mac_cache import MacCacheService
    from stationapp.services.production import ProductionController, ProductionError, SlotPort
    from stationapp.services.simulation import (
        SimulatedFunctionalTester,
        SimulatedGoldenRig,
        SimulatedMacAllocationGateway,
        SimulatedProgrammer,
    )

    engine = create_station_engine(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    MacCacheService(session_factory=factory, station_id="S", jig_id="J", jig_positions=4, threshold=8, gateway=SimulatedMacAllocationGateway(), request_size=8).ensure(MacPurpose.STOCK_RF_TEST)
    none = frozenset()
    controller = ProductionController(
        station_id="S", jig_id="J", slot_count=4, store=SqlProductionStore(factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=none),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=none),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=none),
        qr_format=MODULE_QR_FORMAT,
    )
    controller.start_batch(occupied=(1, 2), ports={n: SlotPort(n, f"U{n}", f"COM{n}") for n in (1, 2)}, mode=ProgrammingMode.STOCK_ONLY)
    while controller.batch.state is not BatchState.QR_SCANNING:
        controller.advance()

    with pytest.raises(ProductionError, match="8 hexadecimal"):
        controller.scan_qr("QR-1001")
    assert controller.scan_qr("sn:1a2b3c4d") == 1
    assert controller.batch.slots[1].module_qr == "1A2B3C4D"
    with pytest.raises(ProductionError, match="already bound"):
        controller.scan_qr("1A2B3C4D")
    engine.dispose()


@pytest.mark.unit
def test_supervisor_badge_scan_fills_user_id(qtbot, tmp_path):
    from stationapp.data.base import Base
    from stationapp.data.database import create_session_factory, create_station_engine
    from stationapp.domain.session import StationConfiguration
    from stationapp.drivers.lan.client import LanServerClient
    from stationapp.services.access import StationAccessService
    from stationapp.services.remote_auth import ServerAuthenticationService
    from stationapp.services.test_users import TestAuthServer, TestUser
    from stationapp.ui.widgets.login_page import LoginPage

    engine = create_station_engine(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    users = (TestUser("SUPERVISOR1", "Test Supervisor", "SUPERVISOR", "Sup@1234", badge_id="SUPBADGE01"),)
    client = LanServerClient(base_url="http://lan", api_key=None, station_id="S", transport=TestAuthServer(users).transport())
    auth = ServerAuthenticationService(client=client, session_factory=factory, station_id="S", jig_id="J", mode_description="test", test_users=True)
    access = StationAccessService(session_factory=factory, station_id="S", authentication=auth, configuration_provider=lambda: StationConfiguration("J", 4, ((1, "U1"),), "R", "a" * 64))
    page = LoginPage(access, station_label="S / J")
    qtbot.addWidget(page)

    qtbot.keyClicks(page.badge_input, "SUPBADGE01")
    qtbot.keyClick(page.badge_input, "\r")

    assert access.supervisor is None                      # badge alone is not enough
    assert page.user_input.text() == "SUPERVISOR1"
    assert "enter your password" in page.error_label.text()

    qtbot.keyClicks(page.password_input, "Sup@1234")
    qtbot.keyClick(page.password_input, "\r")
    assert access.supervisor is not None and access.supervisor.user_id == "SUPERVISOR1"
    engine.dispose()


# ---------------------------------------------------------------------------
# Debounce: a repeated read of the same code is discarded
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.unit
def test_debouncer_drops_consecutive_repeat_within_window():
    from stationapp.drivers.qr_scanner import ScanDebouncer

    clock = FakeClock()
    debouncer = ScanDebouncer(2.0, clock=clock)
    assert debouncer.accept("1A2B3C4D")
    clock.now += 0.3
    assert not debouncer.accept("1A2B3C4D")      # double read
    clock.now += 1.9                               # held trigger: window restarts
    assert not debouncer.accept("1A2B3C4D")
    clock.now += 2.1                               # deliberate re-scan later
    assert debouncer.accept("1A2B3C4D")


@pytest.mark.unit
def test_debouncer_only_drops_consecutive_repeats():
    from stationapp.drivers.qr_scanner import ScanDebouncer

    clock = FakeClock()
    debouncer = ScanDebouncer(2.0, clock=clock)
    assert debouncer.accept("AAAAAAAA")
    assert debouncer.accept("BBBBBBBB")
    assert debouncer.accept("AAAAAAAA")           # A, B, A: not consecutive
    debouncer.forget()
    assert debouncer.accept("AAAAAAAA")           # refused scan may be retried
    assert ScanDebouncer(0, clock=clock).accept("X") and ScanDebouncer(0, clock=clock).accept("X")


@pytest.mark.unit
def test_field_records_double_read_once(qtbot):
    widget = ScannerLineEdit(MODULE_QR_FORMAT, settle_ms=20, debounce_ms=2000)
    qtbot.addWidget(widget)
    scans, repeats = [], []
    widget.scanned.connect(scans.append)
    widget.repeated.connect(repeats.append)

    for _ in range(2):                              # same label read twice
        qtbot.keyClicks(widget, "1a2b3c4d")
        qtbot.keyClick(widget, "\r")
    qtbot.keyClicks(widget, "5E6F7A8B")
    qtbot.keyClick(widget, "\r")

    assert scans == ["1A2B3C4D", "5E6F7A8B"]
    assert repeats == ["1A2B3C4D"]

    widget.forget_last_scan()
    qtbot.keyClicks(widget, "5E6F7A8B")
    qtbot.keyClick(widget, "\r")
    assert scans[-1] == "5E6F7A8B"
