
from __future__ import annotations

from pathlib import Path

import pytest

from sqlalchemy import event

from stationapp.domain.golden_rig_binding import (
    GoldenRigAuthorizationError,
    GoldenRigNotBoundError,
)

from stationapp.infrastructure.golden_rig.sqlite_binding_store import (
    SqliteGoldenRigBindingStore,
)

from stationapp.services.golden_rig_secure_binding import (
    AuthenticatedSupervisor,
    SecureGoldenRigBindingService,
)


FINGERPRINT = "ab" * 32


def make_service(tmp_path: Path):
    repository = SqliteGoldenRigBindingStore(
        f"sqlite:///{(tmp_path / 'station.db').as_posix()}"
    )
    repository.create_schema_for_tests()

    service = SecureGoldenRigBindingService(
        station_id="STATION-01",
        repository=repository,
    )

    return service, repository


def approve(service):
    return service.approve(
        supervisor=AuthenticatedSupervisor("SUP-001"),
        rig_id="GOLDEN-RIG-01",
        host="golden-rig-01.factory.lan",
        port=8762,
        certificate_sha256=FINGERPRINT,
        reason="Initial commissioning",
    )


@pytest.mark.unit
def test_binding_and_audit_saved_together(tmp_path):
    service, repository = make_service(tmp_path)

    audit = approve(service)
    binding = service.require_binding()

    history = repository.list_audits("STATION-01")

    assert binding.rig_id == "GOLDEN-RIG-01"
    assert len(history) == 1
    assert history[0]["audit_id"] == audit.audit_id
    assert history[0]["action"] == "GOLDEN_RIG_BOUND"


@pytest.mark.unit
def test_binding_survives_service_restart(tmp_path):
    service, repository = make_service(tmp_path)
    approve(service)

    restarted = SecureGoldenRigBindingService(
        "STATION-01",
        repository,
    )

    assert restarted.require_binding().rig_id == "GOLDEN-RIG-01"


@pytest.mark.unit
def test_invalid_supervisor_identity_rejected(tmp_path):
    service, repository = make_service(tmp_path)

    with pytest.raises(GoldenRigAuthorizationError):
        service.approve(
            supervisor=None,
            rig_id="GOLDEN-RIG-01",
            host="golden-rig-01.factory.lan",
            port=8762,
            certificate_sha256=FINGERPRINT,
            reason="Invalid request",
        )

    assert repository.load("STATION-01") is None
    assert repository.list_audits("STATION-01") == []


@pytest.mark.unit
def test_disabled_binding_blocks_rf(tmp_path):
    service, repository = make_service(tmp_path)

    approve(service)

    service.disable(
        supervisor=AuthenticatedSupervisor("SUP-001"),
        reason="Rig maintenance",
    )

    with pytest.raises(GoldenRigNotBoundError):
        service.require_binding()

    assert len(repository.list_audits("STATION-01")) == 2


@pytest.mark.unit
def test_database_rollback_on_audit_failure(tmp_path):
    service, repository = make_service(tmp_path)

    @event.listens_for(
        repository._engine,
        "before_cursor_execute",
    )
    def fail_audit_insert(
        conn, cursor, statement, parameters,
        context, executemany,
    ):
        if (
            statement.lstrip().upper().startswith("INSERT INTO")
            and "golden_rig_binding_audit" in statement.lower()
        ):
            raise RuntimeError("Simulated audit database failure")

    try:
        with pytest.raises(
            RuntimeError,
            match="Simulated audit database failure",
        ):
            approve(service)
    finally:
        event.remove(
            repository._engine,
            "before_cursor_execute",
            fail_audit_insert,
        )

    # Both tables must remain unchanged.
    assert repository.load("STATION-01") is None
    assert repository.list_audits("STATION-01") == []
