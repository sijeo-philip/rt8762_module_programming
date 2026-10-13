"""Session-bound repositories for users and production sessions.

Both flush but never commit; the caller's Unit of Work owns the
transaction, so a user or session change and its audit record are
committed together.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stationapp.data.models import ProductionSessionModel, UserModel
from stationapp.domain.session import ProductionSession, SessionStatus, StationConfiguration
from stationapp.domain.users import StationUser, UserRole


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; station timestamps are always UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def count(self) -> int:
        return int(self._session.scalar(select(func.count(UserModel.user_id))) or 0)

    def get_row(self, user_id: str) -> UserModel | None:
        return self._session.get(UserModel, user_id)

    def get_row_by_badge(self, badge_id: str) -> UserModel | None:
        return self._session.scalar(select(UserModel).where(UserModel.badge_id == badge_id))

    def list(self) -> tuple[StationUser, ...]:
        rows = self._session.scalars(select(UserModel).order_by(UserModel.role.desc(), UserModel.user_id)).all()
        return tuple(self.to_domain(row) for row in rows)

    def add(self, user: StationUser, *, secret_hash: str, created_by: str | None) -> None:
        self._session.add(
            UserModel(
                user_id=user.user_id,
                display_name=user.display_name,
                role=user.role.value,
                secret_hash=secret_hash,
                badge_id=user.badge_id,
                active=user.active,
                failed_attempts=0,
                created_at=datetime.now(timezone.utc),
                created_by=created_by,
            )
        )
        self._session.flush()

    @staticmethod
    def to_domain(row: UserModel) -> StationUser:
        return StationUser(
            user_id=row.user_id,
            display_name=row.display_name,
            role=UserRole(row.role),
            active=row.active,
            badge_id=row.badge_id,
        )

    @staticmethod
    def locked_until(row: UserModel) -> datetime | None:
        return _aware(row.locked_until)


class SessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_open(self, station_id: str) -> ProductionSession | None:
        row = self._session.scalar(
            select(ProductionSessionModel).where(
                ProductionSessionModel.station_id == station_id,
                ProductionSessionModel.status == SessionStatus.OPEN.value,
            )
        )
        return self._to_domain(row) if row is not None else None

    def add(self, session: ProductionSession) -> None:
        self._session.add(
            ProductionSessionModel(
                session_id=session.session_id,
                station_id=session.station_id,
                supervisor_id=session.supervisor_id,
                status=session.status.value,
                configuration=session.configuration.to_dict(),
                started_at=session.started_at,
            )
        )
        self._session.flush()

    def update(self, session: ProductionSession) -> None:
        row = self._session.get(ProductionSessionModel, session.session_id)
        if row is None:
            raise LookupError(f"Session {session.session_id} does not exist")
        row.status = session.status.value
        row.configuration = session.configuration.to_dict()
        row.closed_at = session.closed_at
        row.closed_by = session.closed_by
        row.close_reason = session.close_reason
        self._session.flush()

    @staticmethod
    def _to_domain(row: ProductionSessionModel) -> ProductionSession:
        return ProductionSession(
            session_id=row.session_id,
            station_id=row.station_id,
            supervisor_id=row.supervisor_id,
            configuration=StationConfiguration.from_dict(row.configuration),
            started_at=_aware(row.started_at),
            status=SessionStatus(row.status),
            closed_at=_aware(row.closed_at),
            closed_by=row.closed_by,
            close_reason=row.close_reason,
        )
