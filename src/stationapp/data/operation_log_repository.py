"""Append-only repository for station runtime activity."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stationapp.data.errors import DuplicateOperationLog
from stationapp.data.models import OperationLogModel
from stationapp.domain.operation_log import (
    OperationLevel,
    OperationLog,
)


def _as_aware_utc(value: datetime) -> datetime:
    """Restore UTC awareness after reading a SQLite datetime."""

    if value.tzinfo is None:
        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


class OperationLogRepository:
    """Append-only runtime-operation repository.

    This repository intentionally exposes no update() or delete() API.

    It uses a caller-owned SQLAlchemy Session so it can later participate
    in the Lesson 6D Unit of Work.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, record: OperationLog) -> OperationLog:
        """Append one runtime operation record.

        flush() is performed here, but commit() is deliberately left to
        the surrounding transaction.
        """

        model = OperationLogModel(
            operation_id=record.operation_id,
            station_id=record.station_id,
            level=record.level.value,
            category=record.category,
            event_type=record.event_type,
            message=record.message,
            batch_id=record.batch_id,
            slot_number=record.slot_number,
            correlation_id=record.correlation_id,
            user_id=record.user_id,
            detail=deepcopy(
                record.detail
            ),
            occurred_at=record.occurred_at,
        )

        self._session.add(model)

        try:
            self._session.flush()

        except IntegrityError as exc:
            raise DuplicateOperationLog(f"Operation log "
                f"{record.operation_id} already exists"
            ) from exc

        return record

    def get(self, operation_id: str) -> OperationLog | None:
        """Load one operation entry by unique identifier."""

        identifier = operation_id.strip()

        if not identifier:
            raise ValueError(
                "operation_id cannot be empty"
            )

        model = self._session.scalar(
            select(
                OperationLogModel
            ).where(
                OperationLogModel.operation_id
                == identifier
            )
        )

        if model is None:
            return None

        return self._to_domain(
            model
        )

    def list_for_station(self, station_id: str) -> tuple[OperationLog, ...]:

        identifier = station_id.strip()

        if not identifier:
            raise ValueError("station_id cannot be empty")

        rows = self._session.scalars(
            select(OperationLogModel)
            .where(
                OperationLogModel.station_id
                == identifier
            )
            .order_by(
                OperationLogModel.occurred_at,
                OperationLogModel.id,
            )
        ).all()

        return tuple(
            self._to_domain(row)
            for row in rows
        )

    def list_for_batch(self, batch_id: str) -> tuple[OperationLog, ...]:

        identifier = batch_id.strip()

        if not identifier:
            raise ValueError(
                "batch_id cannot be empty"
            )

        rows = self._session.scalars(
            select(
                OperationLogModel
            )
            .where(
                OperationLogModel.batch_id
                == identifier
            )
            .order_by(
                OperationLogModel.occurred_at,
                OperationLogModel.id,
            )
        ).all()

        return tuple(
            self._to_domain(row)
            for row in rows
        )

    def list_for_correlation(self, correlation_id: str) -> tuple[OperationLog, ...]:
        """Return records belonging to one logical operation."""

        identifier = correlation_id.strip()

        if not identifier:
            raise ValueError(
                "correlation_id cannot be empty"
            )

        rows = self._session.scalars(
            select(
                OperationLogModel
            )
            .where(
                OperationLogModel.correlation_id
                == identifier
            )
            .order_by(
                OperationLogModel.occurred_at,
                OperationLogModel.id,
            )
        ).all()

        return tuple(
            self._to_domain(row)
            for row in rows
        )

    def list_by_level(self, level: OperationLevel) -> tuple[OperationLog, ...]:

        rows = self._session.scalars(
            select(
                OperationLogModel
            )
            .where(
                OperationLogModel.level
                == level.value
            )
            .order_by(
                OperationLogModel.occurred_at,
                OperationLogModel.id,
            )
        ).all()

        return tuple(
            self._to_domain(row)
            for row in rows
        )

    @staticmethod
    def _to_domain(model: OperationLogModel) -> OperationLog:

        return OperationLog(
            operation_id=model.operation_id,
            station_id=model.station_id,
            level=OperationLevel(
                model.level
            ),
            category=model.category,
            event_type=model.event_type,
            message=model.message,
            batch_id=model.batch_id,
            slot_number=model.slot_number,
            correlation_id=model.correlation_id,
            user_id=model.user_id,
            detail=deepcopy(
                model.detail
            ),
            occurred_at=_as_aware_utc(
                model.occurred_at
            ),
        )


    