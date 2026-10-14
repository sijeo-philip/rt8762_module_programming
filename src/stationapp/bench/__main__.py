"""Stock firmware bench tool.

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
    """RF tester for the station's approved Golden Rig, if any."""
    if settings is None:
        return None, "no station configuration"
    try:
        from stationapp.infrastructure.golden_rig.sqlite_binding_store import SqliteGoldenRigBindingStore
        from stationapp.services.golden_rig_authorization import GoldenRigAuthorizationService
        from stationapp.services.golden_rig_secure_binding import SecureGoldenRigBindingService
        from stationapp.services.golden_rig_service import GoldenRigService

        store = SqliteGoldenRigBindingStore(settings.database_url)
        binding = store.load(settings.station_id)
        if binding is None or not binding.enabled:
            return None, "none approved - bind it in the station app"
        if not Path(settings.golden_rig_ca_certificate).is_file():
            return None, f"CA certificate missing: {settings.golden_rig_ca_certificate}"
        bindings = SecureGoldenRigBindingService(settings.station_id, store)
        tester = GoldenRigService(GoldenRigAuthorizationService(bindings, settings.golden_rig_ca_certificate))
        return tester, f"{binding.rig_id} at {binding.host}:{binding.port}"
    except Exception as exc:  # noqa: BLE001 - e.g. database not yet created
        logger.warning("Golden Rig unavailable for bench: %s", exc)
        return None, "not available"


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
    parser = argparse.ArgumentParser(prog="python -m stationapp.bench", description="Stock firmware bench tool")
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

    if args.simulate:
        from stationapp.bench.simulated import SimulatedBenchHardware
        from stationapp.services.simulation import SimulatedGoldenRig

        fail = frozenset(port.strip().upper() for port in args.fail_ports.split(",") if port.strip())
        hardware_factory = lambda _settings: SimulatedBenchHardware(delay_s=0.6, fail_ports=fail)  # noqa: E731
        rf_tester, rig_status = SimulatedGoldenRig(delay_seconds=0.5, failures=frozenset()), "simulated"
        port_lister = lambda: [(f"COM{100 + n}", "simulated") for n in range(1, jig_positions + 1)]  # noqa: E731
        slot_ports = {n: f"COM{100 + n}" for n in range(1, jig_positions + 1)}
        import tempfile

        placeholder = Path(tempfile.gettempdir()) / "stationapp_bench_simulated_firmware.bin"
        placeholder.write_bytes(b"SIMULATED STOCK FIRMWARE")
    else:
        hardware_factory = MpCliBenchHardware
        rf_tester, rig_status = _golden_rig(settings)
        port_lister = _ports
        slot_ports = _bound_ports(settings)

    runner = StockBenchRunner(
        hardware_factory=hardware_factory,
        mac_pool=pool,
        results_dir=results_dir,
        rf_tester=rf_tester,
        station_id=settings.station_id if settings else "BENCH",
        jig_id=settings.jig_id if settings else "BENCH",
    )
    context = BenchContext(
        runner=runner,
        mac_pool=pool,
        jig_positions=jig_positions,
        results_dir=results_dir,
        mpcli_path=Path(settings.mpcli_path) if settings else None,
        firmware=placeholder if args.simulate else (settings.stock_firmware_path if settings else None),
        product_id=(settings.mpcli_product_id or "") if settings else "",
        secret_key=settings.mpcli_secret_key.get_secret_value() if settings and settings.mpcli_secret_key else "",
        golden_rig_status=rig_status,
        port_lister=port_lister,
        slot_ports=slot_ports,
        simulation=args.simulate,
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Stock Firmware Bench")
    window = BenchWindow(context)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
