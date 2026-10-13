"""Station users, roles and authenticated identities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from stationapp.domain.errors import DomainError

_USER_ID = re.compile(r"^[A-Za-z0-9._-]{2,64}$")


class UserRole(str, Enum):
    OPERATOR = "OPERATOR"
    SUPERVISOR = "SUPERVISOR"


class AuthenticationMethod(str, Enum):
    PASSWORD = "PASSWORD"
    BADGE = "BADGE"


class AccessError(DomainError):
    """Base class for login and authorization refusals."""


class AuthenticationFailed(AccessError):
    """Unknown user, wrong secret, inactive or locked account."""


class AuthorizationError(AccessError):
    """The authenticated user may not perform this action."""


def normalise_user_id(value: str) -> str:
    user_id = value.strip().upper()
    if not _USER_ID.fullmatch(user_id):
        raise ValueError("User ID must be 2-64 letters, digits, '.', '_' or '-'")
    return user_id


@dataclass(frozen=True, slots=True)
class StationUser:
    user_id: str
    display_name: str
    role: UserRole
    active: bool = True
    badge_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", normalise_user_id(self.user_id))
        if not self.display_name.strip():
            raise ValueError("display_name is required")
        if not isinstance(self.role, UserRole):
            raise ValueError("role must be a UserRole")
        if self.badge_id is not None and not self.badge_id.strip():
            raise ValueError("badge_id cannot be blank")


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Identity produced only by the authentication service."""

    user_id: str
    display_name: str
    role: UserRole
    method: AuthenticationMethod
    authenticated_at: datetime

    @property
    def is_supervisor(self) -> bool:
        return self.role is UserRole.SUPERVISOR

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.user_id})"
