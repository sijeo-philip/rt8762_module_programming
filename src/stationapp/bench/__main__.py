"""Bench tool: the production flow (Stock only or Stock + Pricol) on one
PC, without the LAN server.

    python -m stationapp.bench               # real MPCLI on the jig
    python -m stationapp.bench --simulate    # no hardware

Uses the station .env for defaults when present (MPCLI path, Stock
firmware, credentials, jig size, slot-to-COM bindings, and the approved
Golden Rig for the optional RF step). Needs no LAN server, login or
production session, and writes nothing to the production database.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from stationapp.bench.mac_pool import BenchMacPool
from stationapp.bench.runner import MpCliBenchHardware, StockBenchRunner
from stationapp.bench.window import BenchContext, BenchWindow

logger = logging.getLogger(__name__)


def _settings():
    try:
        from stationapp.config import get_settings

        return get_settings()
    except Exception as exc:  # noqa: BLE001 - the bench works without a station .env
        print(f"Note: station .env not loaded ({exc.__class__.__name__}); using bench defaults", file=sys.stderr)
        return None


def _golden_rig(settings):
    """RF tester for the station's approved Golden Rig, if any, and that binding."""
    if settings is None:
        return None, "no station configuration", None
    try:
        from stationapp.infrastructure.golden_rig.sqlite_binding_store import SqliteGoldenRigBindingStore
        from stationapp.services.golden_rig_authorization import GoldenRigAuthorizationService
        from stationapp.services.golden_rig_secure_binding import SecureGoldenRigBindingService
        from stationapp.services.golden_rig_service import GoldenRigService

        store = SqliteGoldenRigBindingStore(settings.database_url)
        binding = store.load(settings.station_id)
        if binding is None or not binding.enabled:
            return None, "none approved - bind it in the station app", None
        if not Path(settings.golden_rig_ca_certificate).is_file():
            return None, f"CA certificate missing: {settings.golden_rig_ca_certificate}", binding
        bindings = SecureGoldenRigBindingService(settings.station_id, store)
        tester = GoldenRigService(GoldenRigAuthorizationService(bindings, settings.golden_rig_ca_certificate))
        return tester, f"{binding.rig_id} at {binding.host}:{binding.port}", binding
    except Exception as exc:  # noqa: BLE001 - e.g. database not yet created
        logger.warning("Golden Rig unavailable for bench: %s", exc)
        return None, "not available", None


def _default_rig_host(settings, binding) -> str:
    """Approved rig, else the first discovery candidate, else the usual name."""
    if binding is not None:
        return binding.host
    try:
        from stationapp.services.golden_rig_configuration import parse_endpoints

        candidates = parse_endpoints(getattr(settings, "golden_rig_candidates", "") or "")
        if candidates:
            return candidates[0].host
    except Exception:  # noqa: BLE001 - only a default for the input field
        pass
    return "golden-rig-01.local"


def _qr_format(settings):
    """Module QR format from the station .env (QR_CODE_LENGTH, QR_HEX_ONLY)."""
    from stationapp.drivers.qr_scanner import MODULE_QR_FORMAT, ScanFormat

    if settings is None:
        return MODULE_QR_FORMAT
    return ScanFormat(length=settings.qr_code_length, hex_only=settings.qr_hex_only)


def _ports():
    from stationapp.drivers.serial.discovery import discover_serial_ports

    return [(port.device, f"{port.description or ''} {port.stable_key if port.identity.has_usb_identity else ''}".strip()) for port in discover_serial_ports()]


def _bound_ports(settings) -> dict[int, str]:
    """Slot -> current COM port from the station's commissioned bindings."""
    if settings is None:
        return {}
    try:
        from stationapp.drivers.serial.discovery import discover_stable_usb_ports
        from stationapp.services.slot_binding import SlotBindingService

        bindings = SlotBindingService(
            binding_file=settings.serial_binding_file,
            station_id=settings.station_id,
            jig_id=settings.jig_id,
            jig_positions=settings.jig_positions,
        ).load()
        by_key = {port.stable_key: port.device for port in discover_stable_usb_ports()}
        return {item.slot_number: by_key[item.stable_key] for item in bindings.bindings if item.stable_key in by_key}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resolve slot bindings: %s", exc)
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stationapp.bench", description="Production bench tool (Stock only or Stock + Pricol, no LAN server)")
    parser.add_argument("--simulate", action="store_true", help="no MPCLI or jig; simulated programming and Golden Rig")
    parser.add_argument("--fail-ports", default="", help="simulation: comma-separated ports that fail to flash")
    parser.add_argument("--slots", type=int, choices=(4, 8), help="override jig size")
    args = parser.parse_args(argv)

    settings = _settings()
    log_dir = Path(settings.log_dir) if settings else Path("logs")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

    jig_positions = args.slots or (settings.jig_positions if settings else 8)
    results_dir = log_dir / "bench"
    pool = BenchMacPool(Path("data") / "bench_mac_state.json")
    # Separate range for Pricol test MACs (Stock + Pricol runs).
    pricol_pool = BenchMacPool(Path("data") / "bench_pricol_mac_state.json")

    if args.simulate:
        from stationapp.bench.simulated import SimulatedBenchHardware
        from stationapp.services.simulation import SimulatedGoldenRig

        fail = frozenset(port.strip().upper() for port in args.fail_ports.split(",") if port.strip())
        # Reference modules for "Auto-assign COM ports": deliberately not in
        # port order (COM101 holds slot 2's module, ...) to show the matching.
        reference = {f"COM{100 + n}": f"22:22:22:22:22:{n % jig_positions + 1:02X}" for n in range(1, jig_positions + 1)}
        hardware_factory = lambda _settings: SimulatedBenchHardware(delay_s=0.6, fail_ports=fail, reference_macs=reference)  # noqa: E731
        rf_tester, rig_status = SimulatedGoldenRig(delay_seconds=0.5, failures=frozenset()), "simulated"
        from stationapp.services.golden_rig_check import simulated_rig_check

        rig_check, rig_address, rig_host = simulated_rig_check, ("golden-rig-01.local", 8762), "golden-rig-01.local"
        port_lister = lambda: [(f"COM{100 + n}", "simulated") for n in range(1, jig_positions + 1)]  # noqa: E731
        slot_ports = {n: f"COM{100 + n}" for n in range(1, jig_positions + 1)}
        import tempfile

        placeholder = Path(tempfile.gettempdir()) / "stationapp_bench_simulated_firmware.bin"
        placeholder.write_bytes(b"SIMULATED STOCK FIRMWARE")
        pricol_placeholder = Path(tempfile.gettempdir()) / "stationapp_bench_simulated_pricol.bin"
        pricol_placeholder.write_bytes(b"SIMULATED PRICOL FIRMWARE")
        from stationapp.services.simulation import SimulatedFunctionalTester

        functional = SimulatedFunctionalTester(delay_seconds=0.5, failures=frozenset())
    else:
        hardware_factory = MpCliBenchHardware
        rf_tester, rig_status, binding = _golden_rig(settings)
        from stationapp.services.golden_rig_check import GoldenRigConnectionCheck

        ca = Path(settings.golden_rig_ca_certificate) if settings else Path("config/certificates/golden_rig_ca.pem")
        rig_check = GoldenRigConnectionCheck(ca_certificate=ca, binding=lambda: binding).check
        rig_address = (binding.host, binding.port) if binding is not None and binding.enabled else None
        rig_host = _default_rig_host(settings, binding)
        port_lister = _ports
        slot_ports = _bound_ports(settings)

    if not args.simulate:
        from stationapp.drivers.pricol.functional import PricolFunctionalTester

        functional = PricolFunctionalTester(
            baudrate=settings.pricol_uart_baudrate if settings else 115200,
            response_timeout_seconds=settings.pricol_response_timeout_seconds if settings else 3.0,
            reset_wait_seconds=settings.pricol_reset_wait_seconds if settings else 30.0,
            **(
                {"start_message": settings.pricol_start_message, "dtr": settings.pricol_uart_dtr, "rts": settings.pricol_uart_rts}
                if settings else {}
            ),
        )

    runner = StockBenchRunner(
        hardware_factory=hardware_factory,
        mac_pool=pool,
        results_dir=results_dir,
        rf_tester=rf_tester,
        station_id=settings.station_id if settings else "BENCH",
        jig_id=settings.jig_id if settings else "BENCH",
        pricol_mac_pool=pricol_pool,
        functional_tester=functional,
    )
    context = BenchContext(
        runner=runner,
        mac_pool=pool,
        jig_positions=jig_positions,
        results_dir=results_dir,
        mpcli_path=Path(settings.mpcli_path) if settings else None,
        firmware=placeholder if args.simulate else (settings.stock_firmware_path if settings else None),
        pricol_mac_pool=pricol_pool,
        chip_erase=settings.mpcli_chip_erase if settings else "full",
        reference_prefix=settings.slot_reference_mac_prefix if settings else "22:22:22:22:22",
        pricol_firmware=pricol_placeholder if args.simulate else (settings.pricol_firmware_path if settings else None),
        golden_rig_status=rig_status,
        rig_check=rig_check,
        rig_address=rig_address,
        default_rig_host=rig_host,
        qr_format=_qr_format(settings),
        qr_debounce_ms=settings.qr_debounce_ms if settings else 2000,
        use_pos_scanner=settings.qr_scanner_hid_pos if settings else True,
        port_lister=port_lister,
        slot_ports=slot_ports,
        simulation=args.simulate,
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Production Bench")
    window = BenchWindow(context)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
