"""Duplicate QR prevention and threshold-driven LAN server sync."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import select

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.data.models import BatchModel, ModuleQrRegistryModel, OutboxModel, SyncRunModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState, ProgrammingMode
from stationapp.domain.mac import MacPurpose
from stationapp.drivers.lan.client import LanServerClient
from stationapp.services.mac_cache import MacCacheService
from stationapp.services.production import ProductionController, ProductionError, SlotPort
from stationapp.services.qr_registry import ModuleQrClaimService
from stationapp.services.server_sync import ServerSyncService, SyncScheduler
from stationapp.services.simulation import (
    InMemoryLanServer,
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedMacAllocationGateway,
    SimulatedProgrammer,
)

STATION = "STN-01"
JIG = "JIG-01"


@pytest.fixture
def line(tmp_path: Path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    MacCacheService(session_factory=factory, station_id=STATION, jig_id=JIG, jig_positions=8, threshold=64, gateway=SimulatedMacAllocationGateway(), request_size=64).ensure(MacPurpose.STOCK_RF_TEST)

    server = InMemoryLanServer(api_key="secret")
    client = LanServerClient(base_url="http://lan-server", api_key="secret", station_id=STATION, transport=server.transport())
    claims = ModuleQrClaimService(client=client, jig_id=JIG, offline_backoff_seconds=0.0)
    none = frozenset()
    controller = ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=none),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=none),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=none),
        qr_claims=claims,
    )
    sync = ServerSyncService(session_factory=factory, station_id=STATION, jig_id=JIG, client=client, threshold_modules=4, batches_per_package=1, records_per_package=25)
    scheduler = SyncScheduler(sync, retry_seconds=0.2)
    controller.add_commit_listener(scheduler.on_batch_committed)

    yield {"factory": factory, "server": server, "controller": controller, "sync": sync, "scheduler": scheduler}
    scheduler.shutdown()
    scheduler.wait(5)
    client.close()
    engine.dispose()


def ports(*slots):
    return {n: SlotPort(n, f"USB:{n}", f"COM{10 + n}") for n in slots}


def run_to_scanning(controller, slots):
    controller.start_batch(occupied=slots, ports=ports(*slots), mode=ProgrammingMode.STOCK_ONLY)
    for _ in range(10):
        if controller.batch.state is BatchState.QR_SCANNING:
            return
        controller.advance()
    raise AssertionError(controller.batch.state)


def complete_batch(controller, slots, qrs):
    run_to_scanning(controller, slots)
    for qr in qrs:
        controller.scan_qr(qr)
    controller.advance()
    controller.release_batch()


def registry(factory, qr):
    with factory() as session:
        return session.get(ModuleQrRegistryModel, qr)


# ---------------------------------------------------------------------------
# Duplicate QR prevention
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_qr_from_an_earlier_batch_is_rejected(line):
    controller = line["controller"]
    complete_batch(controller, (1,), ["QR-A"])

    run_to_scanning(controller, (1,))
    with pytest.raises(ProductionError, match="already bound at this station"):
        controller.scan_qr("QR-A")


@pytest.mark.integration
def test_online_claim_rejects_qr_bound_at_another_station(line):
    line["server"].register_foreign_qr("QR-X", station_id="STN-02", batch_id="B-77", slot_number=3)
    controller = line["controller"]
    run_to_scanning(controller, (1,))

    with pytest.raises(ProductionError, match="STN-02.*B-77.*slot 3"):
        controller.scan_qr("QR-X")

    assert controller.batch.next_qr_slot == 1
    controller.scan_qr("QR-Y")
    assert registry(line["factory"], "QR-Y").server_status == "CLAIMED"
    assert line["server"].claims["QR-Y"]["station_id"] == STATION


@pytest.mark.integration
def test_pulled_registry_blocks_duplicates_while_offline(line):
    server, controller = line["server"], line["controller"]
    server.register_foreign_qr("QR-OLD", station_id="STN-03")
    assert line["sync"].sync("MANUAL").success

    server.available = False
    run_to_scanning(controller, (1,))
    with pytest.raises(ProductionError, match="station STN-03"):
        controller.scan_qr("QR-OLD")


@pytest.mark.integration
def test_offline_binding_is_unconfirmed_then_conflict_is_flagged_at_sync(line):
    server, controller, sync = line["server"], line["controller"], line["sync"]
    server.available = False
    complete_batch(controller, (1, 2), ["QR-1", "QR-2"])
    assert registry(line["factory"], "QR-1").server_status == "UNCONFIRMED"

    # Meanwhile another station bound QR-2.
    server.available = True
    server.register_foreign_qr("QR-2", station_id="STN-09")

    report = sync.sync("MANUAL")

    assert report.success and report.qr_conflicts == 1
    assert registry(line["factory"], "QR-1").server_status == "SYNCED"
    conflict = registry(line["factory"], "QR-2")
    assert conflict.server_status == "CONFLICT" and "STN-09" in conflict.conflict_detail
    assert sync.status().qr_conflicts == 1
    assert any(event["event_type"] == "QR_CONFLICT_REPORTED" for event in server.events.values()) or sync.conflicts()


# ---------------------------------------------------------------------------
# Threshold-driven sync
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sync_starts_automatically_at_module_threshold(line):
    controller, server, scheduler, factory = line["controller"], line["server"], line["scheduler"], line["factory"]

    complete_batch(controller, (1, 2), ["QR-1", "QR-2"])
    scheduler.wait(5)
    assert server.packages == []                      # 2 < 4 modules
    assert line["sync"].status().pending_modules == 2

    complete_batch(controller, (1, 2), ["QR-3", "QR-4"])
    scheduler.wait(5)

    assert scheduler.last_report.success, scheduler.last_report.detail
    assert len(server.batches) == 2
    assert len(server.packages) >= 2                  # one batch per package
    assert server.events and server.macs
    assert {qr for qr in ("QR-1", "QR-2", "QR-3", "QR-4")} <= set(server.claims)

    status = line["sync"].status()
    assert status.pending_modules == 0 and status.unsynced_events == 0
    with factory() as session:
        assert {row.status for row in session.scalars(select(OutboxModel))} == {"UPLOADED"}
        assert {row.state for row in session.scalars(select(BatchModel))} == {"UPLOADED"}


@pytest.mark.integration
def test_failed_sync_retries_and_production_continues(line):
    controller, server, scheduler, factory = line["controller"], line["server"], line["scheduler"], line["factory"]
    server.available = False

    complete_batch(controller, (1, 2, 3, 4), ["QR-1", "QR-2", "QR-3", "QR-4"])
    scheduler.wait(5)
    assert not scheduler.last_report.success
    with factory() as session:
        assert session.scalars(select(OutboxModel)).first().status == "RETRY"

    # Production is not blocked while the server is down.
    complete_batch(controller, (1,), ["QR-5"])

    server.available = True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (scheduler.last_report and scheduler.last_report.success):
        time.sleep(0.05)
    scheduler.wait(5)

    assert scheduler.last_report.success
    assert len(server.batches) == 2
    with factory() as session:
        runs = [row.status for row in session.scalars(select(SyncRunModel).order_by(SyncRunModel.id))]
    assert "FAILED" in runs and runs[-1] == "SUCCESS"


@pytest.mark.integration
def test_unconfigured_server_reports_clearly(line):
    service = ServerSyncService(session_factory=line["factory"], station_id=STATION, jig_id=JIG, client=None)
    report = service.sync("MANUAL")
    assert not report.success and "not configured" in report.detail
