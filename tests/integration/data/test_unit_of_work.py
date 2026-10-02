"""Integration tests for Lesson 6D Unit of Work."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from stationapp.data.base import Base
from stationapp.data.database import (
    create_session_factory,
    create_station_engine,
)
from stationapp.data.models import (
    AuditRecordModel,
    BatchModel,
    ManufacturingEventModel,
    OperationLogModel,
)
from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.audit import AuditRecord
from stationapp.domain.events import (
    EventType,
    ManufacturingEvent,
)
from stationapp.domain.operation_log import (
    OperationLevel,
    OperationLog,
)


@pytest.fixture
def uow_persistence(
    tmp_path: Path,
):
    database_file = (
        tmp_path
        / "unit_of_work.db"
    )

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    Base.metadata.create_all(
        engine
    )

    session_factory = create_session_factory(
        engine
    )

    try:
        yield engine, session_factory

    finally:
        engine.dispose()


def create_batch(
    session_factory,
    *,
    batch_id: str = "BATCH-001",
) -> None:
    """Create batch parent needed by FK-backed test records."""

    with session_factory() as session:

        session.add(
            BatchModel(
                batch_id=batch_id,
                station_id="STATION-01",
                jig_id="JIG-01",
                slot_count=4,
                state="CREATED",
            )
        )

        session.commit()


def make_event() -> ManufacturingEvent:

    return ManufacturingEvent(
        event_id="EVENT-001",
        event_type=EventType.BATCH_CREATED,
        batch_id="BATCH-001",
        station_id="STATION-01",
        jig_id="JIG-01",
        payload={
            "source": "uow-test",
        },
        occurred_at=datetime.now(
            timezone.utc
        ),
    )


def make_audit() -> AuditRecord:

    return AuditRecord(
        audit_id="AUDIT-001",
        user_id="SUPERVISOR-01",
        station_id="STATION-01",
        record_type="BATCH",
        record_id="BATCH-001",
        action="CREATE",
        reason="Unit of Work integration test",
        previous_value=None,
        new_value={
            "state": "CREATED",
        },
        created_at=datetime.now(
            timezone.utc
        ),
    )


def make_operation() -> OperationLog:

    return OperationLog(
        operation_id="OP-001",
        station_id="STATION-01",
        level=OperationLevel.INFO,
        category="BATCH",
        event_type="BATCH_CREATED",
        message="Batch created successfully",
        batch_id="BATCH-001",
        correlation_id="CORR-001",
        occurred_at=datetime.now(
            timezone.utc
        ),
    )


def get_counts(
    session_factory,
) -> tuple[int, int, int]:

    with session_factory() as session:

        event_count = session.scalar(
            select(
                func.count()
            ).select_from(
                ManufacturingEventModel
            )
        )

        audit_count = session.scalar(
            select(
                func.count()
            ).select_from(
                AuditRecordModel
            )
        )

        operation_count = session.scalar(
            select(
                func.count()
            ).select_from(
                OperationLogModel
            )
        )

    return (
        int(event_count or 0),
        int(audit_count or 0),
        int(operation_count or 0),
    )


@pytest.mark.integration
def test_commit_persists_all_three_repositories(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    create_batch(
        session_factory
    )

    with UnitOfWork(
        session_factory
    ) as uow:

        uow.events.append(
            make_event()
        )

        uow.audit.append(
            make_audit()
        )

        uow.operations.append(
            make_operation()
        )

        uow.commit()

    assert get_counts(
        session_factory
    ) == (
        1,
        1,
        1,
    )


@pytest.mark.integration
def test_no_commit_rolls_everything_back(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    create_batch(
        session_factory
    )

    with UnitOfWork(
        session_factory
    ) as uow:

        uow.events.append(
            make_event()
        )

        uow.audit.append(
            make_audit()
        )

        uow.operations.append(
            make_operation()
        )

        # Deliberately no commit.

    assert get_counts(
        session_factory
    ) == (
        0,
        0,
        0,
    )


@pytest.mark.integration
def test_exception_rolls_everything_back(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    create_batch(
        session_factory
    )

    with pytest.raises(
        RuntimeError,
        match="simulated failure",
    ):

        with UnitOfWork(
            session_factory
        ) as uow:

            uow.events.append(
                make_event()
            )

            uow.audit.append(
                make_audit()
            )

            uow.operations.append(
                make_operation()
            )

            raise RuntimeError(
                "simulated failure"
            )

    assert get_counts(
        session_factory
    ) == (
        0,
        0,
        0,
    )


@pytest.mark.integration
def test_explicit_rollback_removes_everything(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    create_batch(
        session_factory
    )

    with UnitOfWork(
        session_factory
    ) as uow:

        uow.events.append(
            make_event()
        )

        uow.audit.append(
            make_audit()
        )

        uow.operations.append(
            make_operation()
        )

        uow.rollback()

    assert get_counts(
        session_factory
    ) == (
        0,
        0,
        0,
    )


@pytest.mark.integration
def test_repositories_share_same_session(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    with UnitOfWork(
        session_factory
    ) as uow:

        assert (
            uow.events._session
            is uow.audit._session
        )

        assert (
            uow.events._session
            is uow.operations._session
        )

        assert (
            uow.events._session
            is uow.session
        )


@pytest.mark.integration
def test_uow_cannot_be_used_outside_context(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    uow = UnitOfWork(
        session_factory
    )

    with pytest.raises(
        RuntimeError,
        match="not active",
    ):
        _ = uow.session


@pytest.mark.integration
def test_second_commit_is_rejected(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    create_batch(
        session_factory
    )

    with UnitOfWork(
        session_factory
    ) as uow:

        uow.events.append(
            make_event()
        )

        uow.commit()

        with pytest.raises(
            RuntimeError,
            match="already been committed",
        ):
            uow.commit()


@pytest.mark.integration
def test_uow_does_not_suppress_exceptions(
    uow_persistence,
) -> None:

    _, session_factory = uow_persistence

    with pytest.raises(
        ValueError,
        match="production error",
    ):

        with UnitOfWork(
            session_factory
        ):
            raise ValueError(
                "production error"
            )