"""Integration tests for Lesson 6C operation log repository."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path

import pytest

from stationapp.data.base import Base
from stationapp.data.database import (
    create_session_factory,
    create_station_engine,
)
from stationapp.data.errors import DuplicateOperationLog
from stationapp.data.models import BatchModel
from stationapp.data.operation_log_repository import (
    OperationLogRepository,
)
from stationapp.domain.operation_log import (
    OperationLevel,
    OperationLog,
)


@pytest.fixture
def operation_persistence(
    tmp_path: Path,
):

    database_file = (
        tmp_path
        / "operation_log_repository.db"
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
    session,
    batch_id: str,
) -> None:

    session.add(
        BatchModel(
            batch_id=batch_id,
            station_id="STATION-01",
            jig_id="JIG-01",
            slot_count=4,
            state="CREATED",
        )
    )

    session.flush()


def make_log(
    *,
    operation_id: str,
    station_id: str = "STATION-01",
    level: OperationLevel = OperationLevel.INFO,
    category: str = "APPLICATION",
    event_type: str = "APPLICATION_STARTED",
    message: str = "Application started",
    batch_id: str | None = None,
    slot_number: int | None = None,
    correlation_id: str | None = None,
    detail: dict | None = None,
    occurred_at: datetime | None = None,
) -> OperationLog:

    return OperationLog(
        operation_id=operation_id,
        station_id=station_id,
        level=level,
        category=category,
        event_type=event_type,
        message=message,
        batch_id=batch_id,
        slot_number=slot_number,
        correlation_id=correlation_id,
        detail=detail,
        occurred_at=(
            occurred_at
            or datetime.now(
                timezone.utc
            )
        ),
    )


@pytest.mark.integration
def test_append_and_get_operation_log(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-001",
                category="PROGRAMMING",
                event_type="MPCLI_STARTED",
                message="MPCLI programming started",
                detail={
                    "tool_version": "1.0.4.25",
                },
            )
        )

        session.commit()

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        loaded = repository.get(
            "OP-001"
        )

        assert loaded is not None

        assert (
            loaded.event_type
            == "MPCLI_STARTED"
        )

        assert loaded.detail == {
            "tool_version": "1.0.4.25",
        }

        assert (
            loaded.occurred_at.tzinfo
            is not None
        )


@pytest.mark.integration
def test_station_history_is_chronological(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    start = datetime(
        2026,
        10,
        1,
        10,
        0,
        0,
        tzinfo=timezone.utc,
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-003",
                occurred_at=(
                    start
                    + timedelta(seconds=3)
                ),
            )
        )

        repository.append(
            make_log(
                operation_id="OP-001",
                occurred_at=(
                    start
                    + timedelta(seconds=1)
                ),
            )
        )

        repository.append(
            make_log(
                operation_id="OP-002",
                occurred_at=(
                    start
                    + timedelta(seconds=2)
                ),
            )
        )

        session.commit()

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        records = repository.list_for_station(
            "STATION-01"
        )

        assert [
            record.operation_id
            for record in records
        ] == [
            "OP-001",
            "OP-002",
            "OP-003",
        ]


@pytest.mark.integration
def test_duplicate_operation_id_is_rejected(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-DUPLICATE"
            )
        )

        with pytest.raises(
            DuplicateOperationLog
        ):
            repository.append(
                make_log(
                    operation_id="OP-DUPLICATE",
                    event_type="DIFFERENT_EVENT",
                )
            )

        session.rollback()


@pytest.mark.integration
def test_batch_logs_are_isolated(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        create_batch(
            session,
            "BATCH-A",
        )

        create_batch(
            session,
            "BATCH-B",
        )

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-A",
                batch_id="BATCH-A",
            )
        )

        repository.append(
            make_log(
                operation_id="OP-B",
                batch_id="BATCH-B",
            )
        )

        session.commit()

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        logs = repository.list_for_batch(
            "BATCH-A"
        )

        assert [
            log.operation_id
            for log in logs
        ] == [
            "OP-A"
        ]


@pytest.mark.integration
def test_correlation_groups_operations(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-START",
                correlation_id="PROGRAM-SLOT-1",
                event_type="MPCLI_STARTED",
            )
        )

        repository.append(
            make_log(
                operation_id="OP-END",
                correlation_id="PROGRAM-SLOT-1",
                event_type="MPCLI_FINISHED",
            )
        )

        repository.append(
            make_log(
                operation_id="OP-OTHER",
                correlation_id="OTHER-OP",
            )
        )

        session.commit()

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        records = (
            repository.list_for_correlation(
                "PROGRAM-SLOT-1"
            )
        )

        assert {
            record.operation_id
            for record in records
        } == {
            "OP-START",
            "OP-END",
        }


@pytest.mark.integration
def test_error_level_can_be_queried(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-INFO",
                level=OperationLevel.INFO,
            )
        )

        repository.append(
            make_log(
                operation_id="OP-ERROR",
                level=OperationLevel.ERROR,
                event_type="MPCLI_TIMEOUT",
            )
        )

        session.commit()

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        records = repository.list_by_level(
            OperationLevel.ERROR
        )

        assert [
            record.operation_id
            for record in records
        ] == [
            "OP-ERROR"
        ]


@pytest.mark.integration
def test_operation_log_is_immutable(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-IMMUTABLE"
            )
        )

        session.commit()

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        record = repository.get(
            "OP-IMMUTABLE"
        )

        assert record is not None

        with pytest.raises(
            FrozenInstanceError
        ):
            record.level = OperationLevel.ERROR


@pytest.mark.integration
def test_repository_has_no_update_or_delete(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        assert not hasattr(
            repository,
            "update",
        )

        assert not hasattr(
            repository,
            "delete",
        )


@pytest.mark.integration
def test_uncommitted_operation_can_be_rolled_back(
    operation_persistence,
) -> None:

    _, session_factory = (
        operation_persistence
    )

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        repository.append(
            make_log(
                operation_id="OP-ROLLBACK"
            )
        )

        session.rollback()

    with session_factory() as session:

        repository = OperationLogRepository(
            session
        )

        assert (
            repository.get(
                "OP-ROLLBACK"
            )
            is None
        )


