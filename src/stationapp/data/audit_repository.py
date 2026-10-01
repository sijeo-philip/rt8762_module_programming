"""Append-only repository for controlled-change audit history."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stationapp.data.errors import DuplicateAuditRecord
from stationapp.data.models import AuditRecordModel
from stationapp.domain.audit import AuditRecord


def _as_aware_utc(value: datetime) -> datetime:
    """Restore UTC awareness to timestamps loaded from SQLite.

    SQLite does not preserve Python timezone metadata even when the
    SQLAlchemy column uses DateTime(timezone=True).

    Station timestamps are written in UTC, therefore a naive value
    returned by SQLite is interpreted as UTC.
    """

    if value.tzinfo is None:
        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


class AuditRepository:
    """Append-only repository for critical controlled changes.

    This repository deliberately exposes no update() or delete() method.

    The repository uses a caller-owned SQLAlchemy Session so audit records
    can later participate in the same Unit of Work as production-state and
    manufacturing-event changes.
    """

    def __init__(
        self,
        session: Session,
    ) -> None:
        self._session = session

    def append(
        self,
        record: AuditRecord,
    ) -> AuditRecord:
        """Append one immutable audit record.

        The method flushes but does not commit.

        Commit/rollback responsibility belongs to the surrounding
        transaction and, from Lesson 6D onward, the Unit of Work.
        """

        model = AuditRecordModel(
            audit_id=record.audit_id,
            user_id=record.user_id,
            station_id=record.station_id,
            record_type=record.record_type,
            record_id=record.record_id,
            action=record.action,
            previous_value=deepcopy(
                record.previous_value
            ),
            new_value=deepcopy(
                record.new_value
            ),
            reason=record.reason,
            created_at=record.created_at,
        )

        self._session.add(
            model
        )

        try:
            self._session.flush()

        except IntegrityError as exc:
            raise DuplicateAuditRecord(
                f"Audit record {record.audit_id} "
                "already exists"
            ) from exc

        return record

    def get(
        self,
        audit_id: str,
    ) -> AuditRecord | None:
        """Return one audit record by its unique ID."""

        identifier = audit_id.strip()

        if not identifier:
            raise ValueError(
                "audit_id cannot be empty"
            )

        model = self._session.scalar(
            select(
                AuditRecordModel
            ).where(
                AuditRecordModel.audit_id
                == identifier
            )
        )

        if model is None:
            return None

        return self._to_domain(
            model
        )

    def list_for_record(
        self,
        *,
        record_type: str,
        record_id: str,
    ) -> tuple[AuditRecord, ...]:
        """Return chronological audit history for one controlled record."""

        type_name = record_type.strip()
        identifier = record_id.strip()

        if not type_name:
            raise ValueError(
                "record_type cannot be empty"
            )

        if not identifier:
            raise ValueError(
                "record_id cannot be empty"
            )

        models = self._session.scalars(
            select(
                AuditRecordModel
            )
            .where(
                AuditRecordModel.record_type
                == type_name,
                AuditRecordModel.record_id
                == identifier,
            )
            .order_by(
                AuditRecordModel.created_at,
                AuditRecordModel.id,
            )
        ).all()

        return tuple(
            self._to_domain(model)
            for model in models
        )

    def list_for_user(
        self,
        user_id: str,
    ) -> tuple[AuditRecord, ...]:
        """Return chronological audit history produced by one user."""

        identifier = user_id.strip()

        if not identifier:
            raise ValueError(
                "user_id cannot be empty"
            )

        models = self._session.scalars(
            select(
                AuditRecordModel
            )
            .where(
                AuditRecordModel.user_id
                == identifier
            )
            .order_by(
                AuditRecordModel.created_at,
                AuditRecordModel.id,
            )
        ).all()

        return tuple(
            self._to_domain(model)
            for model in models
        )

    def list_for_station(
        self,
        station_id: str,
    ) -> tuple[AuditRecord, ...]:
        """Return chronological audit history originating at one station."""

        identifier = station_id.strip()

        if not identifier:
            raise ValueError(
                "station_id cannot be empty"
            )

        models = self._session.scalars(
            select(
                AuditRecordModel
            )
            .where(
                AuditRecordModel.station_id
                == identifier
            )
            .order_by(
                AuditRecordModel.created_at,
                AuditRecordModel.id,
            )
        ).all()

        return tuple(
            self._to_domain(model)
            for model in models
        )

    @staticmethod
    def _to_domain(
        model: AuditRecordModel,
    ) -> AuditRecord:
        """Translate the SQLAlchemy persistence object into domain data."""

        return AuditRecord(
            audit_id=model.audit_id,
            user_id=model.user_id,
            station_id=model.station_id,
            record_type=model.record_type,
            record_id=model.record_id,
            action=model.action,
            previous_value=deepcopy(
                model.previous_value
            ),
            new_value=deepcopy(
                model.new_value
            ),
            reason=model.reason,
            created_at=_as_aware_utc(
                model.created_at
            ),
        )