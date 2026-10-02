from datetime import datetime, timezone
from pathlib import Path

import pytest

from stationapp.data.base import Base
from stationapp.data.database import (
    create_session_factory,
    create_station_engine,
)
from stationapp.data.models import (
    BatchModel,
    BatchSlotModel,
)
from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.events import (
    EventType,
    ManufacturingEvent,
)
from stationapp.services.history import (
    DeviceHistoryService,
)


@pytest.fixture
def persistence(tmp_path: Path):

    db = tmp_path / "history.db"

    engine = create_station_engine(
        f"sqlite:///{db.as_posix()}"
    )

    Base.metadata.create_all(
        engine
    )

    factory = create_session_factory(
        engine
    )

    try:
        yield factory

    finally:
        engine.dispose()


@pytest.mark.integration
def test_device_history_reconstructed_from_events(
    persistence,
) -> None:

    with persistence() as session:

        session.add(
            BatchModel(
                batch_id="BATCH-001",
                station_id="STATION-01",
                jig_id="JIG-01",
                slot_count=4,
                state="QR_BOUND",
            )
        )

        session.flush()

        session.add(
            BatchSlotModel(
                batch_id="BATCH-001",
                slot_number=1,
                device_state="QR_BOUND",
                module_qr="MODULE-0001",
            )
        )

        session.commit()

    with UnitOfWork(
        persistence
    ) as uow:

        uow.events.append(
            ManufacturingEvent(
                event_id="EV-001",
                event_type=EventType.STOCK_MAC_RESERVED,
                batch_id="BATCH-001",
                station_id="STATION-01",
                jig_id="JIG-01",
                slot_number=1,
                module_qr=None,
                payload={
                    "mac": "AABBCCDDEE01",
                },
                occurred_at=datetime.now(
                    timezone.utc
                ),
            )
        )

        uow.events.append(
            ManufacturingEvent(
                event_id="EV-002",
                event_type=EventType.MODULE_QR_BOUND,
                batch_id="BATCH-001",
                station_id="STATION-01",
                jig_id="JIG-01",
                slot_number=1,
                module_qr="MODULE-0001",
                payload={
                    "module_qr": "MODULE-0001",
                },
                occurred_at=datetime.now(
                    timezone.utc
                ),
            )
        )

        uow.commit()

    service = DeviceHistoryService(
        persistence
    )

    history = service.for_module_qr(
        "MODULE-0001"
    )

    assert history.batch_id == "BATCH-001"

    assert history.slot_number == 1

    assert history.module_qr == "MODULE-0001"

    assert [
        item.event_id
        for item in history.events
    ] == [
        "EV-001",
        "EV-002",
    ]


    