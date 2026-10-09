
from pathlib import Path

import pytest

from stationapp.domain.golden_rig_binding import (
    GoldenRigAuthorizationError,
    GoldenRigNotBoundError,
)
from stationapp.infrastructure.golden_rig.binding_store import (
    GoldenRigBindingStoreError,
    JsonGoldenRigBindingStore,
)
from stationapp.services.golden_rig_binding import (
    GoldenRigBindingService,
    StationRole,
)


TEST_FINGERPRINT = "ab" * 32


def make_service(path: Path, station_id: str = "STATION-01") -> GoldenRigBindingService:
    return GoldenRigBindingService(station_id=station_id,  repository=JsonGoldenRigBindingStore(path))


def approve_rig(service: GoldenRigBindingService):
    return service.approve(
        actor_id="SUP-001",
        actor_role=StationRole.SUPERVISOR,
        rig_id="GOLDEN-RIG-01",
        host="192.168.1.50",
        port=8762,
        certificate_sha256=TEST_FINGERPRINT,
        reason="Initial Golden Rig setup",
    )


@pytest.mark.unit
def test_supervisor_can_approve_binding(tmp_path: Path) -> None:
    service = make_service(tmp_path / "golden_rig.json")
    audit = approve_rig(service)
    binding = service.require_binding()
    assert binding.station_id == "STATION-01"
    assert binding.rig_id == "GOLDEN-RIG-01"
    assert binding.host == "192.168.1.50"
    assert binding.certificate_sha256 == TEST_FINGERPRINT
    assert binding.enabled is True
    assert audit.action == "GOLDEN_RIG_BOUND"
    assert audit.user_id == "SUP-001"


@pytest.mark.unit
def test_binding_persists_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "golden_rig.json"
    first_service = make_service(path)
    approve_rig(first_service)
    restarted_service = make_service(path)
    binding = restarted_service.require_binding()
    assert binding.rig_id == "GOLDEN-RIG-01"
    assert binding.port == 8762


@pytest.mark.unit
def test_operator_cannot_change_binding(tmp_path: Path) -> None:
    service = make_service(tmp_path / "golden_rig.json")

    with pytest.raises(GoldenRigAuthorizationError):
        service.approve(
            actor_id="OP-001",
            actor_role=StationRole.OPERATOR,
            rig_id="GOLDEN-RIG-02",
            host="192.168.1.60",
            port=8762,
            certificate_sha256=TEST_FINGERPRINT,
            reason="Unauthorized change",
        )

    assert service.get_binding() is None


@pytest.mark.unit
def test_missing_binding_blocks_operation(tmp_path: Path) -> None:
    service = make_service(tmp_path / "missing.json")
    with pytest.raises(GoldenRigNotBoundError):
        service.require_binding()


@pytest.mark.unit
def test_binding_cannot_move_between_stations(tmp_path: Path) -> None:
    path = tmp_path / "golden_rig.json"
    approve_rig(make_service(path))
    another_station = make_service(path,station_id="STATION-02")

    with pytest.raises(GoldenRigNotBoundError):
        another_station.require_binding()
    with pytest.raises(GoldenRigAuthorizationError):
        approve_rig(another_station)


@pytest.mark.unit
def test_supervisor_can_disable_binding(tmp_path: Path) -> None:
    service = make_service(tmp_path / "golden_rig.json")
    approve_rig(service)
    audit = service.disable(
        actor_id="SUP-001",
        actor_role=StationRole.SUPERVISOR,
        reason="Golden Rig maintenance",
    )

    assert audit.action == "GOLDEN_RIG_DISABLED"
    with pytest.raises(GoldenRigNotBoundError):
        service.require_binding()


@pytest.mark.unit
def test_invalid_binding_file_blocks_operation(tmp_path: Path) -> None:
    path = tmp_path / "golden_rig.json"
    path.write_text("{invalid json", encoding="utf-8")
    service = make_service(path)
    with pytest.raises(GoldenRigBindingStoreError):
        service.require_binding()


@pytest.mark.unit
def test_invalid_fingerprint_rejected(tmp_path: Path) -> None:
    service = make_service(tmp_path / "golden_rig.json")

    with pytest.raises(Exception, match="Certificate SHA-256"):
        service.approve(
            actor_id="SUP-001",
            actor_role=StationRole.SUPERVISOR,
            rig_id="GOLDEN-RIG-01",
            host="192.168.1.50",
            port=8762,
            certificate_sha256="NOT-A-FINGERPRINT",
            reason="Initial setup",
        )

    assert service.get_binding() is None

    
