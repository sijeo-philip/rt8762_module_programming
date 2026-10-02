"""SQLAlchemy Unit of Work for station production transactions."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.audit_repository import AuditRepository
from stationapp.data.event_repository import ManufacturingEventRepository
from stationapp.data.operation_log_repository import (
    OperationLogRepository,
)


class UnitOfWork:
    """Own one database transaction for one logical station operation.

    A UnitOfWork creates exactly one SQLAlchemy Session and passes that
    same session to every repository participating in the transaction.

    Nothing is committed until commit() is explicitly called.

    If:
        * an exception occurs,
        * commit() is never called, or
        * commit() itself fails,

    the transaction is rolled back.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

        self._session: Session | None = None

        self.events: ManufacturingEventRepository
        self.audit: AuditRepository
        self.operations: OperationLogRepository

        self._committed = False
        self._entered = False

    def __enter__(self) -> Self:
        """Open one session and construct session-bound repositories."""

        if self._entered:
            raise RuntimeError("UnitOfWork cannot be entered more than once")

        self._session = self._session_factory()
        self.events = ManufacturingEventRepository(self._session)
        self.audit = AuditRepository(self._session )
        self.operations = OperationLogRepository(self._session )
        self._committed = False
        self._entered = True
        return self

    def commit(self) -> None:
        """Commit every pending change as one transaction."""

        session = self._require_session()

        if self._committed:
            raise RuntimeError(
                "UnitOfWork has already been committed"
            )

        try:
            session.commit()

        except Exception:
            session.rollback()
            raise

        self._committed = True

    def rollback(self) -> None:
        """Explicitly abandon all pending work."""

        session = self._require_session()
        session.rollback()
        self._committed = False

    @property
    def committed(self) -> bool:
        """Whether this Unit of Work completed a successful commit."""

        return self._committed

    @property
    def session(self) -> Session:
        """Expose the active session for infrastructure-level use.

        Application services should normally work through repositories.
        This property mainly supports incremental migration of the older
        Lesson 5 repositories into Unit-of-Work ownership.
        """

        return self._require_session()

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> bool:
        """Rollback uncommitted work and always close the session."""
        session = self._session
        if session is None:
            return False
        try:
            if exc_type is not None:
                session.rollback()
            elif not self._committed:
                # Safety rule:
                #
                # Leaving a UoW without explicitly committing means the
                # operation was not approved for persistence.
                session.rollback()

        finally:
            session.close()
            self._session = None
            self._entered = False

        # Never suppress application exceptions.
        return False

    def _require_session(self) -> Session:
        """Return active session or reject use outside the context."""

        if self._session is None:
            raise RuntimeError(
                "UnitOfWork is not active. "
                "Use it inside 'with UnitOfWork(...) as uow:'"
            )

        return self._session

    