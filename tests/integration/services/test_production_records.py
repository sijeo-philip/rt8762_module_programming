"""Supervisor search of the local production records."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState, ProgrammingMode
from stationapp.domain.batch_code import BatchCodeState
from stationapp.services.history import DeviceHistoryService
from stationapp.services.production import ProductionController
from stationapp.services.production_records import (
    ProductionRecordService,
    RecordFilter,
    export_csv,
    normalise_mac,
)
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


class FixedCodes:
    """Every module of the tray gets the same Batch Code."""

    def __init__(self, code: str) -> None:
        self.code = code

    def problem(self, session_id=None):
        return None

    def allocate(self, session_id, passed):
        return tuple(self.code for _ in passed), BatchCodeState(session_id=session_id or "S-1", threshold=100, code=self.code, passed=sum(passed))

    def notify(self):
        pass


def run_tray(session_factory, *, operator: str, qrs: dict[int, str], code: str, failures: str = "", mode=ProgrammingMode.STOCK_AND_PRICOL):
    failure_set = parse_failures(failures)
    controller = ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=failure_set),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=failure_set),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=failure_set),
        temporary_stock_mac_base=TEMP_BASE,
        batch_codes=FixedCodes(code),
        retry_pause_seconds=0,
    )
    slots = tuple(sorted(qrs))
    batch = controller.start_batch(occupied=slots, ports=ports(*slots), mode=mode, operator_id=operator, batch_label="LOT-7")
    run_until(controller, BatchState.QR_SCANNING)
    while batch.next_qr_slot is not None:
        controller.scan_qr(qrs[batch.next_qr_slot])
    controller.advance()  # COMPLETE BATCH
    assert batch.state in (BatchState.UPLOAD_PENDING, BatchState.COMMITTED)
    return batch


@pytest.mark.integration
def test_search_by_operator_qr_mac_code_and_date(session_factory):
    first = run_tray(session_factory, operator="OP-ANU", qrs={1: "QRA001", 2: "QRA002"}, code="S5A01", failures="rf:2")
    second = run_tray(session_factory, operator="OP-RAVI", qrs={1: "QRB001"}, code="S5B07")
    service = ProductionRecordService(session_factory)

    everything = service.search(RecordFilter())
    assert len(everything.records) == 3
    assert not everything.truncated
    assert set(service.operators()) == {"OP-ANU", "OP-RAVI"}

    by_operator = service.search(RecordFilter(operator="ravi"))
    assert [(r.batch_id, r.module_qr) for r in by_operator.records] == [(second.batch_id, "QRB001")]

    by_qr = service.search(RecordFilter(module_qr="QRA002"))
    (record,) = by_qr.records
    assert record.slot_number == 2
    assert record.operator_id == "OP-ANU"
    assert record.batch_label == "LOT-7"
    assert record.rf_outcome == "FAIL"
    assert record.result != "PASS"
    assert record.retries == 2           # RF tried three times on slot 2 only
    assert record.stock_mac is not None and record.pricol_mac is None

    good = next(r for r in service.search(RecordFilter(module_qr="QRA001")).records)
    assert good.pricol_mac is not None
    assert good.batch_code == "S5A01"
    assert good.result == "PASS"
    assert good.retries == 0

    # Full MAC in any notation, or its last bytes.
    for text in (good.pricol_mac, good.pricol_mac.replace(":", "-").lower(), normalise_mac(good.pricol_mac)[-6:]):
        found = service.search(RecordFilter(mac=text)).records
        assert [r.module_qr for r in found] == ["QRA001"], text
    # The temporary Stock MAC finds the module too.
    assert "QRA001" in [r.module_qr for r in service.search(RecordFilter(mac=good.stock_mac)).records]

    by_code = service.search(RecordFilter(batch_code="s5b07"))
    assert [r.module_qr for r in by_code.records] == ["QRB001"]
    assert service.search(RecordFilter(batch_code="S5A0")).records == ()

    today = date.today()
    assert len(service.search(RecordFilter(date_from=today, date_to=today)).records) == 3
    assert service.search(RecordFilter(date_from=today - timedelta(days=3), date_to=today - timedelta(days=1))).records == ()

    combined = service.search(RecordFilter(operator="OP-ANU", batch_code="S5A01", date_from=today, date_to=today))
    assert {r.module_qr for r in combined.records} == {"QRA001", "QRA002"}
    assert first.batch_id in {r.batch_id for r in combined.records}


@pytest.mark.integration
def test_limit_csv_export_and_history(session_factory, tmp_path):
    run_tray(session_factory, operator="OP-ANU", qrs={1: "QRC001", 2: "QRC002", 3: "QRC003"}, code="S5C01", mode=ProgrammingMode.STOCK_ONLY)
    service = ProductionRecordService(session_factory)

    limited = service.search(RecordFilter(limit=2))
    assert len(limited.records) == 2
    assert limited.truncated

    result = service.search(RecordFilter())
    path = tmp_path / "records.csv"
    assert export_csv(result.records, path) == 3
    text = path.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("Date / time,Committed,Tray ID")
    assert "QRC002" in text and "S5C01" in text

    record = result.records[0]
    history = DeviceHistoryService(session_factory).for_batch_slot(batch_id=record.batch_id, slot_number=record.slot_number)
    assert any(entry.event_type.value == "MODULE_QR_BOUND" for entry in history.events)
