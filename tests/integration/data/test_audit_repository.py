"""Integration tests for Lesson 6B audit repository."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path

import pytest

from stationapp.data.audit_repository import AuditRepository
from stationapp.data.base import Base
from stationapp.data.database import (
    create_session_factory,
    create_station_engine,
)
from stationapp.data.errors import DuplicateAuditRecord
from stationapp.domain.audit import AuditRecord


@pytest.fixture
def audit_persistence(
    tmp_path: Path,
):
    database_file = tmp_path / "audit_repository.db"

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


def make_audit_record(
    *,
    audit_id: str,
    user_id: str = "SUPERVISOR-01",
    station_id: str = "STATION-01",
    record_type: str = "BATCH",
    record_id: str = "BATCH-001",
    action: str = "RELEASE_HOLD",
    reason: str = "Quality disposition approved",
    previous_value: dict | None = None,
    new_value: dict | None = None,
    created_at: datetime | None = None,
) -> AuditRecord:

    return AuditRecord(
        audit_id=audit_id,
        user_id=user_id,
        station_id=station_id,
        record_type=record_type,
        record_id=record_id,
        action=action,
        reason=reason,
        previous_value=previous_value,
        new_value=new_value,
        created_at=(
            created_at
            or datetime.now(
                timezone.utc
            )
        ),
    )


@pytest.mark.integration
def test_append_and_get_audit_record(
    audit_persistence,
) -> None:

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        record = make_audit_record(
            audit_id="AUDIT-001",
            previous_value={
                "state": "HOLD",
                "hold_reason": "MAC mismatch",
            },
            new_value={
                "state": "READY",
                "hold_reason": None,
            },
            reason=(
                "Readback repeated and "
                "quality disposition approved"
            ),
        )

        repository.append(
            record
        )

        session.commit()

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        loaded = repository.get(
            "AUDIT-001"
        )

        assert loaded is not None

        assert loaded.audit_id == "AUDIT-001"

        assert loaded.user_id == "SUPERVISOR-01"

        assert loaded.station_id == "STATION-01"

        assert loaded.record_type == "BATCH"

        assert loaded.record_id == "BATCH-001"

        assert loaded.action == "RELEASE_HOLD"

        assert loaded.previous_value == {
            "state": "HOLD",
            "hold_reason": "MAC mismatch",
        }

        assert loaded.new_value == {
            "state": "READY",
            "hold_reason": None,
        }

        assert (
            loaded.created_at.tzinfo
            is not None
        )

        assert (
            loaded.created_at.utcoffset()
            == timedelta(0)
        )


@pytest.mark.integration
def test_audit_history_is_chronological(
    audit_persistence,
) -> None:

    _, session_factory = audit_persistence

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

        repository = AuditRepository(
            session
        )

        # Insert deliberately out of order.

        repository.append(
            make_audit_record(
                audit_id="AUDIT-003",
                created_at=(
                    start
                    + timedelta(seconds=3)
                ),
            )
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-001",
                created_at=(
                    start
                    + timedelta(seconds=1)
                ),
            )
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-002",
                created_at=(
                    start
                    + timedelta(seconds=2)
                ),
            )
        )

        session.commit()

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        records = repository.list_for_record(
            record_type="BATCH",
            record_id="BATCH-001",
        )

        assert [
            record.audit_id
            for record in records
        ] == [
            "AUDIT-001",
            "AUDIT-002",
            "AUDIT-003",
        ]


@pytest.mark.integration
def test_retry_or_second_change_creates_new_audit_record(
    audit_persistence,
) -> None:
    """An audit entry is never overwritten by a later action."""

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        first = make_audit_record(
            audit_id="AUDIT-001",
            action="PLACE_HOLD",
            previous_value={
                "state": "READY",
            },
            new_value={
                "state": "HOLD",
            },
            reason="MAC readback mismatch",
        )

        second = make_audit_record(
            audit_id="AUDIT-002",
            action="RELEASE_HOLD",
            previous_value={
                "state": "HOLD",
            },
            new_value={
                "state": "READY",
            },
            reason="Quality disposition completed",
        )

        repository.append(
            first
        )

        repository.append(
            second
        )

        session.commit()

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        records = repository.list_for_record(
            record_type="BATCH",
            record_id="BATCH-001",
        )

        assert len(records) == 2

        assert records[0].action == "PLACE_HOLD"

        assert records[1].action == "RELEASE_HOLD"


@pytest.mark.integration
def test_duplicate_audit_id_is_rejected(
    audit_persistence,
) -> None:

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-DUPLICATE",
            )
        )

        with pytest.raises(
            DuplicateAuditRecord
        ):
            repository.append(
                make_audit_record(
                    audit_id="AUDIT-DUPLICATE",
                    action="DIFFERENT_ACTION",
                )
            )

        session.rollback()


@pytest.mark.integration
def test_returned_audit_record_is_immutable(
    audit_persistence,
) -> None:

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-IMMUTABLE",
            )
        )

        session.commit()

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        record = repository.get(
            "AUDIT-IMMUTABLE"
        )

        assert record is not None

        with pytest.raises(
            FrozenInstanceError
        ):
            record.user_id = "OTHER-USER"


@pytest.mark.integration
def test_record_histories_are_isolated(
    audit_persistence,
) -> None:

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-BATCH-A",
                record_id="BATCH-A",
            )
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-BATCH-B",
                record_id="BATCH-B",
            )
        )

        session.commit()

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        records = repository.list_for_record(
            record_type="BATCH",
            record_id="BATCH-A",
        )

        assert [
            record.audit_id
            for record in records
        ] == [
            "AUDIT-BATCH-A",
        ]


@pytest.mark.integration
def test_list_for_user_is_isolated(
    audit_persistence,
) -> None:

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-USER-A",
                user_id="USER-A",
            )
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-USER-B",
                user_id="USER-B",
            )
        )

        session.commit()

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        records = repository.list_for_user(
            "USER-A"
        )

        assert [
            record.audit_id
            for record in records
        ] == [
            "AUDIT-USER-A",
        ]


@pytest.mark.integration
def test_repository_has_no_update_or_delete_api(
    audit_persistence,
) -> None:

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
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

        assert not hasattr(
            repository,
            "save",
        )


@pytest.mark.integration
def test_audit_append_can_be_rolled_back(
    audit_persistence,
) -> None:
    """Prepare repository for Lesson 6D Unit-of-Work semantics."""

    _, session_factory = audit_persistence

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        repository.append(
            make_audit_record(
                audit_id="AUDIT-ROLLBACK",
            )
        )

        session.rollback()

    with session_factory() as session:

        repository = AuditRepository(
            session
        )

        assert (
            repository.get(
                "AUDIT-ROLLBACK"
            )
            is None
        )

