
from __future__ import annotations

from uuid import uuid4

import pytest

from stationapp.data.base import Base
from stationapp.data.database import (
    create_session_factory,
    create_station_engine,
)
from stationapp.data.models import BatchModel

from stationapp.data.golden_rig_evidence_repository import (
    GoldenRigEvidenceConflict,
    GoldenRigEvidenceBatchMissing,
)

from stationapp.domain.golden_rig import (
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
)

from stationapp.domain.mac import MacAddress

from stationapp.services.golden_rig_evidence_service import (
    GoldenRigEvidenceService,
)

from stationapp.services.golden_rig_service import (
    GoldenRigTestReport,
)


@pytest.fixture
def evidence_service(tmp_path):
    engine = create_station_engine(
        f"sqlite:///{(tmp_path / 'station.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    with factory.begin() as session:
        session.add(
            BatchModel(
                batch_id="BATCH-9H-001",
                station_id="STATION-01",
                jig_id="JIG-8UP",
                slot_count=8,
                state="STOCK_RF_TESTING",
            )
        )

    service = GoldenRigEvidenceService(factory)

    yield service

    engine.dispose()


def make_report(
    request_id: str | None = None,
    *,
    connected: bool = True,
    disconnected: bool = True,
) -> GoldenRigTestReport:
    request = GoldenRigRequest(
        request_id=request_id or str(uuid4()),
        batch_id="BATCH-9H-001",
        station_id="STATION-01",
        jig_id="JIG-8UP",
        targets=(
            GoldenRigTarget(
                slot_number=1,
                expected_mac=MacAddress.parse(
                    "AA:BB:CC:00:00:01"
                ),
            ),
        ),
    )

    response = GoldenRigResponse(
        request_id=request.request_id,
        rig_id="GOLDEN-RIG-01",
        results=(
            GoldenRigSlotResult(
                slot_number=1,
                expected_mac=MacAddress.parse(
                    "AA:BB:CC:00:00:01"
                ),
                reported_mac=(
                    MacAddress.parse("AA:BB:CC:00:00:01")
                    if connected else None
                ),
                connected=connected,
                disconnected=disconnected,
                error_code=(
                    None if connected else "CONNECT_FAILED"
                ),
            ),
        ),
    )

    return GoldenRigTestReport(
        request=request,
        response=response,
    )


@pytest.mark.unit
def test_rf_evidence_survives_new_session(evidence_service):
    report = make_report()

    assert evidence_service.record(report) is True

    stored = evidence_service.get(
        report.request.request_id
    )

    assert stored is not None
    assert len(stored["digest"]) == 64

    evidence = stored["evidence"]

    assert evidence["request"]["batch_id"] == "BATCH-9H-001"
    assert evidence["response"]["rig_id"] == "GOLDEN-RIG-01"
    assert evidence["response"]["results"][0]["connected"] is True
    assert evidence["response"]["results"][0]["disconnected"] is True
    assert evidence["response"]["results"][0]["outcome"] == "PASS"


@pytest.mark.unit
def test_identical_rf_report_is_idempotent(evidence_service):
    report = make_report()

    assert evidence_service.record(report) is True
    assert evidence_service.record(report) is False


@pytest.mark.unit
def test_conflicting_replay_is_rejected(evidence_service):
    request_id = str(uuid4())

    first = make_report(request_id)

    changed = make_report(
        request_id,
        connected=False,
        disconnected=False,
    )

    assert evidence_service.record(first)

    with pytest.raises(GoldenRigEvidenceConflict):
        evidence_service.record(changed)


@pytest.mark.unit
def test_failed_dut_evidence_is_preserved(evidence_service):
    report = make_report(
        connected=False,
        disconnected=False,
    )

    evidence_service.record(report)

    stored = evidence_service.get(
        report.request.request_id
    )

    result = stored["evidence"]["response"]["results"][0]

    assert result["outcome"] == "FAIL"
    assert result["connected"] is False
    assert result["disconnected"] is False
    assert result["error_code"] == "CONNECT_FAILED"


@pytest.mark.unit
def test_missing_batch_rejected(evidence_service):
    report = make_report()

    from dataclasses import replace

    altered_request = replace(
        report.request,
        batch_id="BATCH-DOES-NOT-EXIST",
    )

    altered_report = GoldenRigTestReport(
        request=altered_request,
        response=report.response,
    )

    with pytest.raises(GoldenRigEvidenceBatchMissing):
        evidence_service.record(altered_report)


@pytest.mark.unit
def test_unknown_request_returns_none(evidence_service):
    assert evidence_service.get(str(uuid4())) is None
