"""Hardcoded test users behind a stand-in LAN server auth API.

FOR TESTING ONLY. Real stations verify every login on the LAN server,
where operators and supervisors are enrolled (docs/lan_server_api.md).
This module answers the same HTTP endpoints from a fixed user list so
the login flow can be exercised before that server exists. It is used
only when AUTH_MODE=test or in SIMULATION_MODE, and the UI shows a
TEST USERS warning whenever it is active.
"""

from __future__ import annotations

import hmac
import json
import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TestUser:
    __test__ = False  # not a pytest test class

    user_id: str
    display_name: str
    role: str
    password: str
    badge_id: str | None = None


TEST_USERS: tuple[TestUser, ...] = (
    TestUser("OPERATOR1", "Test Operator", "OPERATOR", "op1234", badge_id="OPBADGE001"),
    TestUser("SUPERVISOR1", "Test Supervisor", "SUPERVISOR", "Sup@1234"),
)

_MAX_FAILURES = 5


class TestAuthServer:
    """Implements POST /api/v1/auth/login and /api/v1/auth/badge."""

    __test__ = False  # not a pytest test class

    def __init__(self, users: tuple[TestUser, ...] = TEST_USERS) -> None:
        self._users = {user.user_id: user for user in users}
        self._badges = {user.badge_id: user for user in users if user.badge_id}
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()

    def transport(self):
        import httpx

        return httpx.MockTransport(self.handle)

    def handles(self, path: str) -> bool:
        return path in {"/api/v1/auth/login", "/api/v1/auth/badge"}

    def handle(self, request):
        import httpx

        if request.method != "POST" or not self.handles(request.url.path):
            return httpx.Response(404, json={"error": "NOT_FOUND"})
        body = json.loads(request.content or b"{}")

        with self._lock:
            if request.url.path.endswith("/badge"):
                user = self._badges.get(str(body.get("badge_id", "")).strip())
                if user is None:
                    return _refused(401, "INVALID_CREDENTIALS", "Badge not recognised")
                return _accepted(user)

            user_id = str(body.get("user_id", "")).strip().upper()
            user = self._users.get(user_id)
            if user is not None and self._failures.get(user_id, 0) >= _MAX_FAILURES:
                return _refused(423, "ACCOUNT_LOCKED", f"Account {user_id} is locked. Contact the server administrator.")
            if user is None or not hmac.compare_digest(str(body.get("password", "")), user.password):
                if user is not None:
                    self._failures[user_id] = self._failures.get(user_id, 0) + 1
                return _refused(401, "INVALID_CREDENTIALS", "Invalid user ID or password")
            self._failures.pop(user_id, None)
            return _accepted(user)


def _accepted(user: TestUser):
    import httpx

    return httpx.Response(200, json={"user_id": user.user_id, "display_name": user.display_name, "role": user.role})


def _refused(status: int, code: str, message: str):
    import httpx

    return httpx.Response(status, json={"error": code, "message": message})
