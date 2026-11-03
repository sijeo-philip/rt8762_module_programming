"""Login verification on the box: phone number and 6-digit PIN.

People are registered at head office and reach the box from there, so
anyone registered can log in on any station. The station stores no
PINs. The box returns the person's role (SDK 6.1): admin and supervisor
log in as Supervisors, user as an Operator.

The box keeps one person per station: every login here ends the box
session of whoever was logged in before, and reports are recorded
against the person logged in. one_person_per_station tells the access
service that a Supervisor login takes the box session from the Operator.

Every attempt is also written to the station's own audit trail.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker
from unisemble_client import User, errors

from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.audit import AuditRecord
from stationapp.domain.users import (
    AuthenticatedUser,
    AuthenticationFailed,
    AuthenticationMethod,
    PinChangeRequired,
    role_from_box,
)
from stationapp.drivers.box.gateway import BoxGateway
from stationapp.services.box_blocks import describe_box_error

logger = logging.getLogger(__name__)

_WRONG = "Phone number or PIN not recognised."


class BoxAuthenticationService:
    """Drop-in replacement for the local AuthenticationService."""

    one_person_per_station = True

    def __init__(self, *, gateway: BoxGateway | None, session_factory: sessionmaker[Session], station_id: str, mode_description: str, test_users: bool = False, before_logout: Callable[[AuthenticatedUser], None] | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._gateway = gateway
        self._session_factory = session_factory
        self._station_id = station_id
        self._before_logout = before_logout
        self._clock = clock
        self.mode_description = mode_description
        self.test_users = test_users

    @property
    def available(self) -> bool:
        return self._gateway is not None

    def set_before_logout(self, callback: Callable[[AuthenticatedUser], None]) -> None:
        self._before_logout = callback

    def authenticate(self, user_id: str, secret: str, *, purpose: str) -> AuthenticatedUser:
        """user_id is the phone number, secret the PIN."""
        phone = user_id.strip()
        if not phone or not secret:
            raise AuthenticationFailed("Enter your phone number and PIN")
        gateway = self._require_gateway()
        return self._verified(phone, lambda: gateway.login(phone, secret), purpose)

    def change_pin(self, phone: str, current_pin: str, new_pin: str, *, purpose: str) -> AuthenticatedUser:
        """Forced PIN change after PinChangeRequired: logs the person in."""
        phone = phone.strip()
        if len(new_pin) != 6 or not new_pin.isdigit():
            raise AuthenticationFailed("The new PIN must be 6 digits")
        if new_pin == current_pin:
            raise AuthenticationFailed("The new PIN must differ from the current PIN")
        gateway = self._require_gateway()

        def call() -> User:
            user = gateway.change_pin(current_pin, new_pin)
            if user is None:  # not a forced change: the person was already logged in
                user = gateway.login(phone, new_pin)
            return user

        user = self._verified(phone, call, f"{purpose} (new PIN)")
        self._audit(user.user_id, "PIN_CHANGED", f"{purpose}: PIN changed on the box")
        return user

    def record_logout(self, user: AuthenticatedUser, reason: str) -> None:
        holder = self._gateway.holder if self._gateway is not None else None
        if holder is not None and holder.user_id == user.user_id:
            if self._before_logout is not None:
                try:
                    # Guide 4.9: report finished modules before logout().
                    self._before_logout(user)
                except Exception:  # noqa: BLE001 - the logout goes ahead
                    logger.exception("Report before logout failed")
            self._gateway.logout()
        self._audit(user.user_id, "LOGOUT", reason)

    # ------------------------------------------------------------------

    def _verified(self, attempted: str, call: Callable[[], User], purpose: str) -> AuthenticatedUser:
        try:
            remote = call()
        except errors.PinChangeRequired as exc:
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: new PIN required")
            raise PinChangeRequired(attempted, "This PIN must be changed. Choose a new 6-digit PIN.") from exc
        except errors.WrongPin as exc:
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: wrong phone or PIN")
            raise AuthenticationFailed(_WRONG) from exc
        except errors.AccountLocked as exc:
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: account locked")
            raise AuthenticationFailed(f"Too many wrong PINs. Try again in {exc.retry_after_seconds // 60 + 1} minutes.") from exc
        except errors.Refused as exc:
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: refused ({exc.reason})")
            if exc.reason == "bad_request":
                raise AuthenticationFailed("The box refused the request. A PIN is 6 digits.") from exc
            raise AuthenticationFailed(f"Login refused: {describe_box_error(exc)}.") from exc
        except errors.UnisembleError as exc:
            logger.warning("Login could not be verified: %s", exc)
            self._audit(attempted, "LOGIN_FAILED", f"{purpose}: box unreachable")
            raise AuthenticationFailed(f"Cannot verify the login: {describe_box_error(exc)}. Check the network and try again.") from exc

        try:
            role = role_from_box(remote.role)
        except ValueError as exc:
            self._audit(remote.user_id, "LOGIN_REFUSED", f"{purpose}: unknown role {remote.role!r}")
            raise AuthenticationFailed(f"The box returned an unknown role {remote.role!r}. Call support.") from exc

        user = AuthenticatedUser(
            user_id=remote.user_id,
            display_name=remote.name or remote.phone,
            role=role,
            method=AuthenticationMethod.PIN,
            authenticated_at=self._clock(),
        )
        self._audit(user.user_id, "LOGIN", f"{purpose} as {user.role.value} (box role {remote.role}, phone {remote.phone})" + (" [DEMO BOX]" if self.test_users else ""))
        return user

    def _require_gateway(self) -> BoxGateway:
        if self._gateway is None:
            raise AuthenticationFailed(
                "Logins are verified by the box, which is not configured. "
                "Set BOX_URL and BOX_STATION_KEY (or AUTH_MODE=test for testing)."
            )
        return self._gateway

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
