"""Login verification on the LAN server.

Operators and Supervisors are enrolled centrally, so any enrolled user
can log in on any jig. The station never stores passwords: each login
is sent to the server (POST /api/v1/auth/login or /auth/badge), which
checks the credentials, applies lockout and returns the user's role.

Every attempt is also written to the station's own audit trail.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.audit import AuditRecord
from stationapp.domain.users import (
    AuthenticatedUser,
    AuthenticationFailed,
    AuthenticationMethod,
    PasswordRequired,
    UserRole,
    normalise_user_id,
)
from stationapp.drivers.lan.client import (
    LanAuthRejected,
    LanServerClient,
    LanServerRejected,
    LanServerUnavailable,
    RemoteUser,
)

logger = logging.getLogger(__name__)


class ServerAuthenticationService:
    """Drop-in replacement for the local AuthenticationService."""

    def __init__(self, *, client: LanServerClient | None, session_factory: sessionmaker[Session], station_id: str, jig_id: str, mode_description: str, test_users: bool = False, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._client = client
        self._session_factory = session_factory
        self._station_id = station_id
        self._jig_id = jig_id
        self._clock = clock
        self.mode_description = mode_description
        self.test_users = test_users

    @property
    def available(self) -> bool:
        return self._client is not None

    def authenticate(self, user_id: str, secret: str, *, purpose: str) -> AuthenticatedUser:
        try:
            normalised = normalise_user_id(user_id)
        except ValueError as exc:
            raise AuthenticationFailed("Invalid user ID or password") from exc
        if not secret:
            raise AuthenticationFailed("Enter your password")
        client = self._require_client()
        return self._verified(
            normalised,
            lambda: client.login_password(normalised, secret, jig_id=self._jig_id),
            AuthenticationMethod.PASSWORD,
            purpose,
        )

    def authenticate_badge(self, badge_id: str, *, purpose: str) -> AuthenticatedUser:
        badge = badge_id.strip()
        if not badge:
            raise AuthenticationFailed("Badge not recognised")
        client = self._require_client()
        user = self._verified("BADGE", lambda: client.login_badge(badge, jig_id=self._jig_id), AuthenticationMethod.BADGE, f"{purpose} (badge)")
        if user.role is not UserRole.OPERATOR:
            # A lost badge must not open Supervisor functions.
            self._audit(user.user_id, "LOGIN_REFUSED", f"{purpose}: badge login not allowed for Supervisors")
            raise PasswordRequired(user.user_id, "Supervisor badge recognised - enter your password to log in")
        return user

    def record_logout(self, user: AuthenticatedUser, reason: str) -> None:
        self._audit(user.user_id, "LOGOUT", reason)

    # ------------------------------------------------------------------

    def _verified(self, attempted: str, call: Callable[[], RemoteUser], method: AuthenticationMethod, purpose: str) -> AuthenticatedUser:
        try:
            remote = call()
        except LanAuthRejected as exc:
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: server refused ({exc.code})")
            raise AuthenticationFailed(str(exc)) from exc
        except LanServerUnavailable as exc:
            logger.warning("Login could not be verified: %s", exc)
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: server unreachable")
            raise AuthenticationFailed("Cannot reach the LAN server to verify the login. Check the network and try again.") from exc
        except LanServerRejected as exc:
            logger.error("Login request rejected by server: %s", exc)
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: server rejected request")
            raise AuthenticationFailed(f"The LAN server rejected the login request: {exc}") from exc

        user = AuthenticatedUser(
            user_id=normalise_user_id(remote.user_id),
            display_name=remote.display_name,
            role=UserRole(remote.role),
            method=method,
            authenticated_at=self._clock(),
        )
        self._audit(user.user_id, "LOGIN", f"{purpose} as {user.role.value}" + (" [TEST USERS]" if self.test_users else ""))
        return user

    def _require_client(self) -> LanServerClient:
        if self._client is None:
            raise AuthenticationFailed(
                "Logins are verified by the LAN server, which is not configured. "
                "Set LAN_SERVER_URL (or AUTH_MODE=test for testing)."
            )
        return self._client

    def _audit(self, user_id: str, action: str, reason: str) -> None:
        try:
            with UnitOfWork(self._session_factory) as uow:
                uow.audit.append(AuditRecord(
                    user_id=user_id or "UNKNOWN",
                    station_id=self._station_id,
                    record_type="USER_LOGIN",
                    record_id=user_id or "UNKNOWN",
                    action=action,
                    reason=reason,
                ))
                uow.commit()
        except Exception:  # noqa: BLE001 - an audit failure must not block a login decision
            logger.exception("Could not write login audit record")
