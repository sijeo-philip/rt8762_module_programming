"""Integration tests for Lesson 6A manufacturing event repository."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stationapp.data.base import Base
from stationapp.data.database import (
    create_session_factory,
    create_station_engine,
)
from stationapp.data.errors import DuplicateManufacturingEvent
from stationapp.data.event_repository import ManufacturingEventRepository
from stationapp.data.models import (
    BatchModel,
    ManufacturingEventModel,
)
from stationapp.domain.events import (
    EventType,
    ManufacturingEvent,
)


@pytest.fixture
def event_persistence(tmp_path: Path):
    """Create one isolated Lesson 6A database."""

    database_file = tmp_path / "event_repository.db"

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    Base.metadata.create_all(engine)

    session_factory = create_session_factory(engine)

    try:
        yield engine, session_factory

    finally:
        engine.dispose()


def create_batch(
    session,
    *,
    batch_id: str,
) -> None:
    """Insert the parent batch required by the event FK."""

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


def make_event(
    *,
    event_id: str,
    batch_id: str = "BATCH-001",
    event_type: EventType = EventType.BATCH_CREATED,
    slot_number: int | None = None,
    module_qr: str | None = None,
    occurred_at: datetime | None = None,
    payload: dict | None = None,
) -> ManufacturingEvent:

    return ManufacturingEvent(
        event_id=event_id,
        event_type=event_type,
        batch_id=batch_id,
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_number=slot_number,
        module_qr=module_qr,
        payload=payload or {},
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )


@pytest.mark.integration
def test_append_and_get_event(
    event_persistence,
) -> None:

    _, session_factory = event_persistence

    with session_factory() as session:
        create_batch(
            session,
            batch_id="BATCH-001",
        )

        repository = ManufacturingEventRepository(
            session
        )

        original = make_event(
            event_id="EVENT-001",
            event_type=EventType.STOCK_MAC_RESERVED,
            slot_number=1,
            payload={
                "mac": "AABBCCDDEE01",
            },
        )

        repository.append(original)

        session.commit()

    # Use another session to prove the event really came back from SQLite,
    # rather than from SQLAlchemy's identity map.

    with session_factory() as session:
        repository = ManufacturingEventRepository(
            session
        )

        loaded = repository.get(
            "EVENT-001"
        )

        assert loaded is not None

        assert loaded.event_id == "EVENT-001"
        assert loaded.event_type == EventType.STOCK_MAC_RESERVED
        assert loaded.batch_id == "BATCH-001"
        assert loaded.station_id == "STATION-01"
        assert loaded.jig_id == "JIG-01"
        assert loaded.slot_number == 1
        assert loaded.payload == {
            "mac": "AABBCCDDEE01",
        }


@pytest.mark.integration
def test_events_are_returned_in_chronological_order(
    event_persistence,
) -> None:

    _, session_factory = event_persistence

    base_time = datetime(
        2026,
        10,
        1,
        10,
        0,
        0,
        tzinfo=timezone.utc,
    )

    with session_factory() as session:
        create_batch(
            session,
            batch_id="BATCH-001",
        )

        repository = ManufacturingEventRepository(
            session
        )

        # Deliberately append them OUT of chronological order.
        repository.append(
            make_event(
                event_id="EVENT-003",
                occurred_at=base_time + timedelta(seconds=3),
            )
        )

        repository.append(
            make_event(
                event_id="EVENT-001",
                occurred_at=base_time + timedelta(seconds=1),
            )
        )

        repository.append(
            make_event(
                event_id="EVENT-002",
                occurred_at=base_time + timedelta(seconds=2),
            )
        )

        session.commit()

    with session_factory() as session:
        repository = ManufacturingEventRepository(
            session
        )

        events = repository.list_for_batch(
            "BATCH-001"
        )

        assert [
            event.event_id
            for event in events
        ] == [
            "EVENT-001",
            "EVENT-002",
            "EVENT-003",
        ]


@pytest.mark.integration
def test_retry_creates_second_event(
    event_persistence,
) -> None:

    _, session_factory = event_persistence

    with session_factory() as session:
        create_batch(
            session,
            batch_id="BATCH-001",
        )

        repository = ManufacturingEventRepository(
            session
        )

        first_attempt = make_event(
            event_id="EVENT-FAIL-001",
            event_type=EventType.STOCK_PROGRAMMING_UNCERTAIN,
            slot_number=1,
            payload={
                "reason": "MPTool timeout",
                "attempt": 1,
            },
        )

        retry = make_event(
            event_id="EVENT-RETRY-001",
            event_type=EventType.STOCK_PROGRAMMING_STARTED,
            slot_number=1,
            payload={
                "attempt": 2,
            },
        )

        repository.append(
            first_attempt,
            correlation_id="PROGRAM-SLOT-1",
        )

        repository.append(
            retry,
            correlation_id="PROGRAM-SLOT-1",
        )

        session.commit()

    with session_factory() as session:
        repository = ManufacturingEventRepository(
            session
        )

        events = repository.list_for_batch(
            "BATCH-001"
        )

        assert len(events) == 2

        types = {
            event.event_type
            for event in events
        }

        assert EventType.STOCK_PROGRAMMING_UNCERTAIN in types
        assert EventType.STOCK_PROGRAMMING_STARTED in types


@pytest.mark.integration
def test_duplicate_event_id_is_rejected(
    event_persistence,
) -> None:

    _, session_factory = event_persistence

    with session_factory() as session:
        create_batch(
            session,
            batch_id="BATCH-001",
        )

        repository = ManufacturingEventRepository(
            session
        )

        repository.append(
            make_event(
                event_id="EVENT-DUPLICATE",
            )
        )

        session.flush()

        with pytest.raises(
            DuplicateManufacturingEvent
        ):
            repository.append(
                make_event(
                    event_id="EVENT-DUPLICATE",
                    event_type=EventType.HOLD_PLACED,
                )
            )

        session.rollback()


@pytest.mark.integration
def test_returned_event_is_immutable(
    event_persistence,
) -> None:

    _, session_factory = event_persistence

    with session_factory() as session:
        create_batch(
            session,
            batch_id="BATCH-001",
        )

        repository = ManufacturingEventRepository(
            session
        )

        repository.append(
            make_event(
                event_id="EVENT-IMMUTABLE",
            )
        )

        session.commit()

    with session_factory() as session:
        repository = ManufacturingEventRepository(
            session
        )

        event = repository.get(
            "EVENT-IMMUTABLE"
        )

        assert event is not None

        with pytest.raises(
            FrozenInstanceError
        ):
            event.batch_id = "CHANGED"


@pytest.mark.integration
def test_batch_histories_are_isolated(
    event_persistence,
) -> None:

    _, session_factory = event_persistence

    with session_factory() as session:
        create_batch(
            session,
            batch_id="BATCH-A",
        )

        create_batch(
            session,
            batch_id="BATCH-B",
        )

        repository = ManufacturingEventRepository(
            session
        )

        repository.append(
            make_event(
                event_id="A-001",
                batch_id="BATCH-A",
            )
        )

        repository.append(
            make_event(
                event_id="A-002",
                batch_id="BATCH-A",
                event_type=EventType.PORT_BOUND,
            )
        )

        repository.append(
            make_event(
                event_id="B-001",
                batch_id="BATCH-B",
            )
        )

        session.commit()

    with session_factory() as session:
        repository = ManufacturingEventRepository(
            session
        )

        events = repository.list_for_batch(
            "BATCH-A"
        )

        assert [
            event.event_id
            for event in events
        ] == [
            "A-001",
            "A-002",
        ]


@pytest.mark.integration
def test_repository_has_no_update_or_delete_api(
    event_persistence,
) -> None:
    """Historical repositories must not expose mutation operations."""

    _, session_factory = event_persistence

    with session_factory() as session:
        repository = ManufacturingEventRepository(
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
def test_rollback_removes_uncommitted_event(
    event_persistence,
) -> None:
    """Prepare Lesson 6A repository for Unit-of-Work semantics."""

    _, session_factory = event_persistence

    with session_factory() as session:
        create_batch(
            session,
            batch_id="BATCH-001",
        )

        session.commit()

    with session_factory() as session:
        repository = ManufacturingEventRepository(
            session
        )

        repository.append(
            make_event(
                event_id="EVENT-ROLLBACK",
            )
        )

        session.rollback()

    with session_factory() as session:
        repository = ManufacturingEventRepository(
            session
        )

        event = repository.get(
            "EVENT-ROLLBACK"
        )

        assert event is None



        