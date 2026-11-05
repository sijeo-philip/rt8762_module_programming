"""The session's running Batch Code (SDK 7.0 batch_id).

The Supervisor sets the threshold when starting the production session
(StationAccessService.start_session). The Operator enters the starting
series once per session, before the first tray; later Operators carry
on with the running code. Only a Supervisor may enter a new series once
one is running. Every entry is audited.

The ProductionController asks allocate() for the codes of a tray when it
commits the tray, and saves the new state in the commit's transaction.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.batch_code_repository import load_batch_code, save_batch_code
from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.audit import AuditRecord
from stationapp.domain.batch_code import BatchCodeError, BatchCodeState
from stationapp.domain.users import AuthenticatedUser, AuthorizationError

logger = logging.getLogger(__name__)


class BatchCodeService:
    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, access) -> None:
        self._session_factory = session_factory
        self._station_id = station_id
        self._access = access
        self._listeners: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def notify(self) -> None:
        """A tray was committed or a session changed: refresh indicators."""
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - an indicator must not stop production
                logger.exception("Batch Code listener failed")

    # -- reading -----------------------------------------------------------

    def current(self) -> BatchCodeState | None:
        """The open session's Batch Code state; None without a session or
        when the session was opened without a threshold."""
        session = self._access.session
        if session is None:
            return None
        return self.for_session(session.session_id)

    def for_session(self, session_id: str | None) -> BatchCodeState | None:
        with self._session_factory() as db:
            state = load_batch_code(db)
        if state is None or session_id is None or state.session_id != session_id:
            return None
        return state

    def problem(self, session_id: str | None = None) -> str | None:
        """Why a tray may not start now, or None."""
        if session_id is None and self._access.session is not None:
            session_id = self._access.session.session_id
        state = self.for_session(session_id)
        if state is None:
            return "No Batch Code threshold for this session. A Supervisor must close the session and start it again with a threshold."
        if not state.ready:
            return "Enter the Batch Code starting series before the first tray."
        return None

    def allocate(self, session_id: str | None, passed: Sequence[bool]) -> tuple[tuple[str, ...], BatchCodeState]:
        """Codes for a committed tray's modules (slot order) and the state
        after it. Nothing is saved: the caller saves the state with the tray.
        session_id None: the open session."""
        if session_id is None and self._access.session is not None:
            session_id = self._access.session.session_id
        state = self.for_session(session_id)
        if state is None or not state.ready:
            raise BatchCodeError(self.problem(session_id) or "No Batch Code")
        return state.allocate(passed)

    # -- entering the series -------------------------------------------------

    def start_series(self, code: str, *, supervisor: AuthenticatedUser | None = None, reason: str | None = None) -> BatchCodeState:
        """The Operator's starting series, or a Supervisor's new series.

        Refused while a tray is in progress. Once a series is running only
        a Supervisor may start another, with a reason.
        """
        session = self._access.session
        if session is None:
            raise AuthorizationError("No production session is open")
        if self._access.batch_in_progress():
            raise AuthorizationError("Finish the current tray before changing the Batch Code")
        # The logged-in Supervisor counts as one; else the one who approved.
        user = supervisor or self._access.supervisor or self._access.operator
        if user is None:
            raise AuthorizationError("Log in before entering the Batch Code")
        if supervisor is not None and not supervisor.is_supervisor:
            raise AuthorizationError("Supervisor authorization is required")
        actor, as_supervisor = user.user_id, user.is_supervisor

        with self._lock, UnitOfWork(self._session_factory) as uow:
            previous = load_batch_code(uow.session)
            if previous is None or previous.session_id != session.session_id:
                raise BatchCodeError(self.problem(session.session_id) or "No Batch Code threshold")
            if previous.ready and not as_supervisor:
                raise AuthorizationError(f"Batch Code {previous.code} is already running. Only a Supervisor can start a new series.")
            if previous.ready and not (reason or "").strip():
                raise BatchCodeError("A reason is required to change the running Batch Code")
            state = previous.started(code)
            save_batch_code(uow.session, state, actor)
            uow.audit.append(AuditRecord(
                user_id=actor,
                station_id=self._station_id,
                record_type="BATCH_CODE",
                record_id=session.session_id,
                action="BATCH_CODE_SERIES_CHANGED" if previous.ready else "BATCH_CODE_SERIES_STARTED",
                reason=(reason or "").strip() or "Batch Code starting series entered",
                previous_value=previous.to_dict(),
                new_value=state.to_dict(),
            ))
            uow.commit()

        logger.info("Batch Code series %s started by %s (threshold %d)", state.code, actor, state.threshold)
        self.notify()
        return state
