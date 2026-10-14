"""Authentication, user administration and production sessions.

Workflow steps 1 and 2:

* The Supervisor logs in and starts a production session, confirming the
  jig's COM-port-to-slot bindings and the approved Golden Rig binding.
* The Operator logs in (user ID + PIN, or badge) once a session is open.

StationAccessService is the single place the rest of the application
asks "who may do this?". Production refuses to start, advance or scan
unless an Operator is logged in to an open session whose confirmed
configuration still matches the live station.

Every login, failed login, user change and session change is written to
the append-only audit trail.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.unit_of_work import UnitOfWork
from stationapp.data.user_repository import SessionRepository, UserRepository
from stationapp.domain.audit import AuditRecord
from stationapp.domain.session import ProductionSession, StationConfiguration
from stationapp.domain.users import (
    AccessError,
    AuthenticatedUser,
    AuthenticationFailed,
    AuthenticationMethod,
    AuthorizationError,
    StationUser,
    UserRole,
    normalise_user_id,
)
from stationapp.services.health import HealthCheck, Severity

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

MIN_SECRET_LENGTH = {
    UserRole.SUPERVISOR: 8,
    UserRole.OPERATOR: 4,
}

_INVALID_CREDENTIALS = "Invalid user ID or password"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SecretHasher:
    """Argon2id hashing for passwords and PINs."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()

    def hash(self, secret: str) -> str:
        return self._hasher.hash(secret)

    def verify(self, secret_hash: str, secret: str) -> bool:
        try:
            return self._hasher.verify(secret_hash, secret)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def _validate_secret(role: UserRole, secret: str) -> None:
    minimum = MIN_SECRET_LENGTH[role]
    if len(secret) < minimum:
        noun = "Password" if role is UserRole.SUPERVISOR else "PIN/password"
        raise ValueError(f"{noun} must be at least {minimum} characters")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class AuthenticationService:
    """Verify credentials, enforce lockout, and audit every attempt."""

    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, hasher: SecretHasher | None = None, max_failures: int = 5, lockout: timedelta = timedelta(minutes=5), clock: Clock = _utc_now) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        self._session_factory = session_factory
        self._station_id = station_id
        self._hasher = hasher or SecretHasher()
        self._max_failures = max_failures
        self._lockout = lockout
        self._clock = clock

    @property
    def hasher(self) -> SecretHasher:
        return self._hasher

    def authenticate(self, user_id: str, secret: str, *, purpose: str) -> AuthenticatedUser:
        try:
            user_id = normalise_user_id(user_id)
        except ValueError as exc:
            raise AuthenticationFailed(_INVALID_CREDENTIALS) from exc

        now = self._clock()
        with UnitOfWork(self._session_factory) as uow:
            users = UserRepository(uow.session)
            row = users.get_row(user_id)

            if row is None:
                self._append_audit(uow, user_id, "LOGIN_FAILED", f"{purpose}: unknown user")
                uow.commit()
                raise AuthenticationFailed(_INVALID_CREDENTIALS)

            locked_until = users.locked_until(row)
            if locked_until is not None and locked_until > now:
                self._append_audit(uow, user_id, "LOGIN_REFUSED", f"{purpose}: account locked")
                uow.commit()
                raise AuthenticationFailed(
                    f"Account {user_id} is locked until {locked_until.astimezone():%H:%M}. "
                    "Ask a Supervisor or wait."
                )

            if not row.active:
                self._append_audit(uow, user_id, "LOGIN_REFUSED", f"{purpose}: account inactive")
                uow.commit()
                raise AuthenticationFailed(f"Account {user_id} is disabled")

            if not self._hasher.verify(row.secret_hash, secret):
                row.failed_attempts += 1
                action = "LOGIN_FAILED"
                if row.failed_attempts >= self._max_failures:
                    row.failed_attempts = 0
                    row.locked_until = now + self._lockout
                    action = "ACCOUNT_LOCKED"
                self._append_audit(uow, user_id, action, f"{purpose}: wrong password")
                uow.commit()
                raise AuthenticationFailed(_INVALID_CREDENTIALS)

            row.failed_attempts = 0
            row.locked_until = None
            row.last_login_at = now
            self._append_audit(uow, user_id, "LOGIN", purpose)
            uow.commit()
            return AuthenticatedUser(
                user_id=row.user_id,
                display_name=row.display_name,
                role=UserRole(row.role),
                method=AuthenticationMethod.PASSWORD,
                authenticated_at=now,
            )

    def authenticate_badge(self, badge_id: str, *, purpose: str) -> AuthenticatedUser:
        """Badge-only login is limited to Operators."""
        badge = badge_id.strip()
        if not badge:
            raise AuthenticationFailed("Badge not recognised")

        now = self._clock()
        with UnitOfWork(self._session_factory) as uow:
            users = UserRepository(uow.session)
            row = users.get_row_by_badge(badge)

            if row is None or not row.active:
                self._append_audit(uow, "UNKNOWN-BADGE", "LOGIN_FAILED", f"{purpose}: badge not recognised")
                uow.commit()
                raise AuthenticationFailed("Badge not recognised")

            if row.role != UserRole.OPERATOR.value:
                self._append_audit(uow, row.user_id, "LOGIN_REFUSED", f"{purpose}: badge login not allowed for role")
                uow.commit()
                raise AuthenticationFailed("Supervisors must log in with a password")

            locked_until = users.locked_until(row)
            if locked_until is not None and locked_until > now:
                self._append_audit(uow, row.user_id, "LOGIN_REFUSED", f"{purpose}: account locked")
                uow.commit()
                raise AuthenticationFailed(f"Account {row.user_id} is locked")

            row.last_login_at = now
            self._append_audit(uow, row.user_id, "LOGIN", f"{purpose} (badge)")
            uow.commit()
            return AuthenticatedUser(
                user_id=row.user_id,
                display_name=row.display_name,
                role=UserRole.OPERATOR,
                method=AuthenticationMethod.BADGE,
                authenticated_at=now,
            )

    def record_logout(self, user: AuthenticatedUser, reason: str) -> None:
        with UnitOfWork(self._session_factory) as uow:
            self._append_audit(uow, user.user_id, "LOGOUT", reason)
            uow.commit()

    def _append_audit(self, uow: UnitOfWork, user_id: str, action: str, reason: str) -> None:
        uow.audit.append(
            AuditRecord(
                user_id=user_id,
                station_id=self._station_id,
                record_type="USER_LOGIN",
                record_id=user_id,
                action=action,
                reason=reason,
            )
        )


# ---------------------------------------------------------------------------
# User administration
# ---------------------------------------------------------------------------


class UserAdministrationService:
    """Supervisor-only management of station users."""

    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, hasher: SecretHasher) -> None:
        self._session_factory = session_factory
        self._station_id = station_id
        self._hasher = hasher

    def has_users(self) -> bool:
        with self._session_factory() as session:
            return UserRepository(session).count() > 0

    def list_users(self) -> tuple[StationUser, ...]:
        with self._session_factory() as session:
            return UserRepository(session).list()

    def create_first_supervisor(self, *, user_id: str, display_name: str, password: str) -> StationUser:
        """Initial station setup: allowed only while no user exists."""
        user = StationUser(user_id=user_id, display_name=display_name, role=UserRole.SUPERVISOR)
        _validate_secret(UserRole.SUPERVISOR, password)

        with UnitOfWork(self._session_factory) as uow:
            users = UserRepository(uow.session)
            if users.count() > 0:
                raise AuthorizationError("Station users already exist; a Supervisor must add new users")
            users.add(user, secret_hash=self._hasher.hash(password), created_by=None)
            uow.audit.append(self._audit(user.user_id, user.user_id, "USER_CREATED", "Initial station Supervisor", None, self._snapshot(user)))
            uow.commit()
        return user

    def create_user(self, actor: AuthenticatedUser, *, user_id: str, display_name: str, role: UserRole, secret: str, badge_id: str | None, reason: str) -> StationUser:
        self._require_supervisor(actor)
        user = StationUser(user_id=user_id, display_name=display_name, role=role, badge_id=(badge_id or "").strip() or None)
        _validate_secret(role, secret)
        reason = self._require_reason(reason)

        with UnitOfWork(self._session_factory) as uow:
            users = UserRepository(uow.session)
            if users.get_row(user.user_id) is not None:
                raise ValueError(f"User {user.user_id} already exists")
            try:
                users.add(user, secret_hash=self._hasher.hash(secret), created_by=actor.user_id)
            except IntegrityError as exc:
                raise ValueError("That badge is already assigned to another user") from exc
            uow.audit.append(self._audit(actor.user_id, user.user_id, "USER_CREATED", reason, None, self._snapshot(user)))
            uow.commit()
        return user

    def reset_secret(self, actor: AuthenticatedUser, *, user_id: str, secret: str, reason: str) -> None:
        self._require_supervisor(actor)
        reason = self._require_reason(reason)
        with UnitOfWork(self._session_factory) as uow:
            row = self._row(uow, user_id)
            _validate_secret(UserRole(row.role), secret)
            row.secret_hash = self._hasher.hash(secret)
            row.failed_attempts = 0
            row.locked_until = None
            # The secret itself is never written to the audit trail.
            uow.audit.append(self._audit(actor.user_id, row.user_id, "USER_SECRET_RESET", reason, None, None))
            uow.commit()

    def set_active(self, actor: AuthenticatedUser, *, user_id: str, active: bool, reason: str) -> None:
        self._require_supervisor(actor)
        reason = self._require_reason(reason)
        with UnitOfWork(self._session_factory) as uow:
            users = UserRepository(uow.session)
            row = self._row(uow, user_id)
            if not active:
                if row.user_id == actor.user_id:
                    raise AuthorizationError("You cannot disable your own account")
                if row.role == UserRole.SUPERVISOR.value:
                    supervisors = [user for user in users.list() if user.role is UserRole.SUPERVISOR and user.active]
                    if len(supervisors) <= 1:
                        raise AuthorizationError("The last active Supervisor cannot be disabled")
            previous = self._snapshot(users.to_domain(row))
            row.active = active
            row.failed_attempts = 0
            row.locked_until = None
            uow.audit.append(self._audit(actor.user_id, row.user_id, "USER_ENABLED" if active else "USER_DISABLED", reason, previous, self._snapshot(users.to_domain(row))))
            uow.commit()

    def set_badge(self, actor: AuthenticatedUser, *, user_id: str, badge_id: str | None, reason: str) -> None:
        self._require_supervisor(actor)
        reason = self._require_reason(reason)
        badge = (badge_id or "").strip() or None
        with UnitOfWork(self._session_factory) as uow:
            users = UserRepository(uow.session)
            row = self._row(uow, user_id)
            previous = self._snapshot(users.to_domain(row))
            row.badge_id = badge
            try:
                uow.session.flush()
            except IntegrityError as exc:
                raise ValueError("That badge is already assigned to another user") from exc
            uow.audit.append(self._audit(actor.user_id, row.user_id, "USER_BADGE_CHANGED", reason, previous, self._snapshot(users.to_domain(row))))
            uow.commit()

    def _row(self, uow: UnitOfWork, user_id: str):
        row = UserRepository(uow.session).get_row(normalise_user_id(user_id))
        if row is None:
            raise ValueError(f"User {user_id} does not exist")
        return row

    @staticmethod
    def _require_supervisor(actor: AuthenticatedUser) -> None:
        if not isinstance(actor, AuthenticatedUser) or not actor.is_supervisor:
            raise AuthorizationError("Supervisor authorization required")

    @staticmethod
    def _require_reason(reason: str) -> str:
        if not reason.strip():
            raise ValueError("A reason is required")
        return reason.strip()

    @staticmethod
    def _snapshot(user: StationUser) -> dict:
        return {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "role": user.role.value,
            "active": user.active,
            "badge_assigned": user.badge_id is not None,
        }

    def _audit(self, actor_id: str, user_id: str, action: str, reason: str, previous: dict | None, new: dict | None) -> AuditRecord:
        return AuditRecord(
            user_id=actor_id,
            station_id=self._station_id,
            record_type="STATION_USER",
            record_id=user_id,
            action=action,
            reason=reason,
            previous_value=previous,
            new_value=new,
        )


# ---------------------------------------------------------------------------
# Logged-in users and the production session
# ---------------------------------------------------------------------------


class StationAccessService:
    """Current Supervisor/Operator logins and the production session."""

    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, authentication: AuthenticationService, configuration_provider: Callable[[], StationConfiguration], rig_verifier: Callable[[StationConfiguration], tuple[str, ...]] | None = None, supervisor_timeout: timedelta = timedelta(minutes=15), clock: Clock = _utc_now) -> None:
        self._session_factory = session_factory
        self._station_id = station_id
        self._auth = authentication
        self._configuration_provider = configuration_provider
        self._rig_verifier = rig_verifier
        self._supervisor_timeout = supervisor_timeout
        self._clock = clock

        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._supervisor: AuthenticatedUser | None = None
        self._supervisor_seen: datetime | None = None
        self._operator: AuthenticatedUser | None = None
        self._batch_active: Callable[[], bool] = lambda: False

        # A session survives an application restart; logins do not.
        with session_factory() as session:
            self._session = SessionRepository(session).get_open(station_id)

    # -- observation -----------------------------------------------------

    @property
    def authentication(self):
        return self._auth

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def set_batch_activity_probe(self, probe: Callable[[], bool]) -> None:
        """Tells the service whether a batch is currently in progress."""
        self._batch_active = probe

    @property
    def session(self) -> ProductionSession | None:
        return self._session

    @property
    def operator(self) -> AuthenticatedUser | None:
        return self._operator

    @property
    def supervisor(self) -> AuthenticatedUser | None:
        with self._lock:
            if self._supervisor is not None and self._supervisor_expired():
                expired = self._supervisor
                self._supervisor = None
                self._supervisor_seen = None
                self._auth.record_logout(expired, "Supervisor session timed out")
            return self._supervisor

    # -- logins ----------------------------------------------------------

    def login_supervisor(self, user_id: str, password: str) -> AuthenticatedUser:
        user = self._auth.authenticate(user_id, password, purpose="Supervisor login")
        if not user.is_supervisor:
            self._auth.record_logout(user, "Refused: not a Supervisor")
            raise AuthorizationError(f"{user.user_id} is not a Supervisor")
        with self._lock:
            self._supervisor = user
            self._supervisor_seen = self._clock()
        self._notify()
        return user

    def logout_supervisor(self) -> None:
        with self._lock:
            user, self._supervisor, self._supervisor_seen = self._supervisor, None, None
        if user is not None:
            self._auth.record_logout(user, "Supervisor logout")
        self._notify()

    def require_supervisor(self) -> AuthenticatedUser:
        with self._lock:
            user = self.supervisor
            if user is None:
                raise AuthorizationError("Supervisor login required")
            self._supervisor_seen = self._clock()
            return user

    def authorize_supervisor(self, user_id: str, password: str) -> AuthenticatedUser:
        """One-off Supervisor approval (e.g. a HOLD disposition) without
        changing who is logged in."""
        user = self._auth.authenticate(user_id, password, purpose="Supervisor authorization")
        if not user.is_supervisor:
            raise AuthorizationError(f"{user.user_id} is not a Supervisor")
        return user

    def login(self, user_id: str, secret: str) -> AuthenticatedUser:
        """Single login screen: the verified role decides what happens.

        Supervisors become the station Supervisor; Operators need an open
        production session.
        """
        user = self._auth.authenticate(user_id, secret, purpose="Station login")
        return self._accept_login(user)

    def login_with_badge(self, badge_id: str) -> AuthenticatedUser:
        user = self._auth.authenticate_badge(badge_id, purpose="Station login")
        return self._accept_login(user)

    def _accept_login(self, user: AuthenticatedUser) -> AuthenticatedUser:
        if user.is_supervisor:
            with self._lock:
                self._supervisor = user
                self._supervisor_seen = self._clock()
            self._notify()
            return user
        if self._session is None:
            self._auth.record_logout(user, "Refused: no production session open")
            raise AuthorizationError("No production session is open. A Supervisor must log in and start one first.")
        return self._set_operator(user)

    def logout_current(self) -> None:
        """Log out whoever is on top: the Supervisor first, else the Operator."""
        if self.supervisor is not None:
            self.logout_supervisor()
        else:
            self.logout_operator()

    def login_operator(self, user_id: str, secret: str) -> AuthenticatedUser:
        self._require_open_session_for_login()
        user = self._auth.authenticate(user_id, secret, purpose="Operator login")
        return self._set_operator(user)

    def login_operator_badge(self, badge_id: str) -> AuthenticatedUser:
        self._require_open_session_for_login()
        user = self._auth.authenticate_badge(badge_id, purpose="Operator login")
        return self._set_operator(user)

    def logout_operator(self) -> None:
        with self._lock:
            user, self._operator = self._operator, None
        if user is not None:
            self._auth.record_logout(user, "Operator logout")
        self._notify()

    def _set_operator(self, user: AuthenticatedUser) -> AuthenticatedUser:
        with self._lock:
            previous, self._operator = self._operator, user
        if previous is not None and previous.user_id != user.user_id:
            self._auth.record_logout(previous, f"Replaced by operator {user.user_id}")
        self._notify()
        return user

    def _require_open_session_for_login(self) -> None:
        if self._session is None:
            raise AuthorizationError("No production session is open. A Supervisor must start one first.")

    # -- session ---------------------------------------------------------

    def current_configuration(self) -> StationConfiguration:
        return self._configuration_provider()

    def start_session(self, confirmed: StationConfiguration) -> ProductionSession:
        """Start production with the configuration the Supervisor reviewed."""
        supervisor = self.require_supervisor()
        with self._lock:
            if self._session is not None:
                raise AuthorizationError("A production session is already open")

            live = self._configuration_provider()
            changed = confirmed.differences(live)
            if changed:
                raise AuthorizationError("Configuration changed during review: " + "; ".join(changed) + ". Review it again.")
            if not live.slot_bindings:
                raise AuthorizationError("Bind at least one jig slot before starting a session")
            if not live.golden_rig_id or not live.golden_rig_fingerprint:
                raise AuthorizationError("No approved Golden Rig binding. Bind the Golden Rig first.")
            if self._rig_verifier is not None:
                problems = self._rig_verifier(live)
                if problems:
                    raise AuthorizationError("Golden Rig verification failed: " + "; ".join(problems))

            session = ProductionSession(
                station_id=self._station_id,
                supervisor_id=supervisor.user_id,
                configuration=live,
            )
            with UnitOfWork(self._session_factory) as uow:
                SessionRepository(uow.session).add(session)
                uow.audit.append(self._audit(supervisor.user_id, session, "SESSION_STARTED", "Supervisor started production session", None, live.to_dict()))
                uow.commit()
            self._session = session

        logger.info("Production session started | session_id=%s supervisor=%s", session.session_id, supervisor.user_id)
        self._notify()
        return session

    def ensure_configuration_change_allowed(self) -> AuthenticatedUser:
        """Supervisor present and no batch running; returns the Supervisor."""
        supervisor = self.require_supervisor()
        if self._batch_active():
            raise AuthorizationError("Finish or abort the current batch before changing the station configuration")
        return supervisor

    def reconfirm_configuration(self, reason: str) -> None:
        """Supervisor accepts a changed binding for the open session."""
        supervisor = self.require_supervisor()
        if not reason.strip():
            raise ValueError("A reason is required")
        with self._lock:
            session = self._session
            if session is None:
                return
            if self._batch_active():
                raise AuthorizationError("Finish or abort the current batch before changing the configuration")
            live = self._configuration_provider()
            if not live.slot_bindings:
                raise AuthorizationError("At least one jig slot must remain bound")
            updated = session.reconfirmed(live)
            with UnitOfWork(self._session_factory) as uow:
                SessionRepository(uow.session).update(updated)
                uow.audit.append(self._audit(supervisor.user_id, session, "SESSION_CONFIGURATION_CONFIRMED", reason, session.configuration.to_dict(), live.to_dict()))
                uow.commit()
            self._session = updated
        self._notify()

    def close_session(self, reason: str) -> None:
        supervisor = self.require_supervisor()
        with self._lock:
            session = self._session
            if session is None:
                raise AuthorizationError("No production session is open")
            if self._batch_active():
                raise AuthorizationError("Finish or abort the current batch before closing the session")
            closed = session.closed(by=supervisor.user_id, reason=reason)
            with UnitOfWork(self._session_factory) as uow:
                SessionRepository(uow.session).update(closed)
                uow.audit.append(self._audit(supervisor.user_id, session, "SESSION_CLOSED", closed.close_reason or reason, None, None))
                uow.commit()
            self._session = None
            operator, self._operator = self._operator, None

        if operator is not None:
            self._auth.record_logout(operator, "Production session closed")
        logger.info("Production session closed | session_id=%s", session.session_id)
        self._notify()

    def configuration_problems(self) -> tuple[str, ...]:
        session = self._session
        if session is None:
            return ("No production session is open",)
        try:
            live = self._configuration_provider()
        except Exception as exc:  # noqa: BLE001 - reported, never ignored
            logger.exception("Could not read live station configuration")
            return (f"Could not read station configuration: {exc}",)
        return tuple(
            f"{problem} since the session started; Supervisor must re-confirm"
            for problem in session.configuration.differences(live)
        )

    def require_production_access(self) -> tuple[str, str]:
        """(session_id, operator_id) or AuthorizationError."""
        session = self._session
        if session is None:
            raise AuthorizationError("No production session is open")
        operator = self._operator or self.supervisor
        if operator is None:
            raise AuthorizationError("Operator login required")
        problems = self.configuration_problems()
        if problems:
            raise AuthorizationError("; ".join(problems))
        return session.session_id, operator.user_id

    def session_health(self) -> HealthCheck:
        session = self._session
        if session is None:
            return HealthCheck("Session", Severity.FAIL, "No production session. Supervisor must start one.")
        problems = self.configuration_problems()
        if problems:
            return HealthCheck("Session", Severity.FAIL, "; ".join(problems))
        user = self._operator or self.supervisor
        if user is None:
            return HealthCheck("Session", Severity.FAIL, f"Session open (Supervisor {session.supervisor_id}); Operator login required")
        return HealthCheck("Session", Severity.OK, f"Session by {session.supervisor_id}; operating as {user.user_id}")

    # -- helpers ---------------------------------------------------------

    def _supervisor_expired(self) -> bool:
        return self._supervisor_seen is not None and self._clock() - self._supervisor_seen > self._supervisor_timeout

    def _audit(self, actor_id: str, session: ProductionSession, action: str, reason: str, previous: dict | None, new: dict | None) -> AuditRecord:
        return AuditRecord(
            user_id=actor_id,
            station_id=self._station_id,
            record_type="PRODUCTION_SESSION",
            record_id=session.session_id,
            action=action,
            reason=reason,
            previous_value=previous,
            new_value=new,
        )

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                logger.exception("Access listener failed")


__all__ = [
    "AccessError",
    "AuthenticationFailed",
    "AuthenticationService",
    "AuthorizationError",
    "SecretHasher",
    "StationAccessService",
    "UserAdministrationService",
]
