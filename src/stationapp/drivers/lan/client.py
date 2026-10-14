"""HTTP client for the LAN Server (contract: docs/lan_server_api.md).

Three operations:

    claim_module_qr     reserve a module QR for this station at scan time
    fetch_module_qr     pull QR codes bound anywhere on the line
    upload_sync_package push completed batches, history, MACs, audits

Network faults and 5xx responses raise LanServerUnavailable (retry
later, production continues). A 4xx other than the documented 409
raises LanServerRejected: the request itself is wrong and retrying the
same data will not help.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API = "/api/v1"


class LanServerError(Exception):
    """Base error for LAN server communication."""


class LanServerUnavailable(LanServerError):
    """Server unreachable, timed out, or failed internally (retryable)."""


class LanServerRejected(LanServerError):
    """Server refused the request as invalid (not retryable as-is)."""


class LanAuthRejected(LanServerError):
    """The server refused a login: wrong credentials, disabled or locked."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RemoteUser:
    """Identity and role as enrolled on the LAN server."""

    user_id: str
    display_name: str
    role: str


@dataclass(frozen=True, slots=True)
class QrOwner:
    station_id: str
    batch_id: str | None
    slot_number: int | None
    bound_at: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "QrOwner":
        return cls(
            station_id=str(data.get("station_id", "?")),
            batch_id=data.get("batch_id"),
            slot_number=data.get("slot_number"),
            bound_at=data.get("bound_at"),
        )

    def describe(self) -> str:
        where = f"station {self.station_id}"
        if self.batch_id:
            where += f", batch {self.batch_id}"
        if self.slot_number:
            where += f", slot {self.slot_number}"
        return where


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claimed: bool
    owner: QrOwner | None = None


@dataclass(frozen=True, slots=True)
class KnownQr:
    module_qr: str
    owner: QrOwner


@dataclass(frozen=True, slots=True)
class SyncAck:
    qr_conflicts: tuple[tuple[str, QrOwner], ...] = ()


class LanServerClient:
    def __init__(self, *, base_url: str, api_key: str | None, station_id: str, timeout_seconds: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"X-Station-Id": station_id, "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._station_id = station_id
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------

    def claim_module_qr(self, module_qr: str, *, jig_id: str, batch_id: str, slot_number: int, timeout_seconds: float | None = None) -> ClaimResult:
        response = self._request(
            "POST",
            f"{API}/module-qr/claims",
            json={
                "module_qr": module_qr,
                "station_id": self._station_id,
                "jig_id": jig_id,
                "batch_id": batch_id,
                "slot_number": slot_number,
            },
            timeout=timeout_seconds,
            allowed={409},
        )
        if response.status_code == 409:
            return ClaimResult(False, QrOwner.from_json(self._json(response).get("owner", {})))
        return ClaimResult(True)

    def fetch_module_qr(self, *, since: str | None, limit: int = 5000) -> tuple[tuple[KnownQr, ...], str | None]:
        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        data = self._json(self._request("GET", f"{API}/module-qr", params=params))
        try:
            codes = tuple(
                KnownQr(str(item["module_qr"]), QrOwner.from_json(item))
                for item in data.get("codes", [])
            )
        except (KeyError, TypeError) as exc:
            raise LanServerRejected(f"Malformed module QR list from server: {exc}") from exc
        return codes, data.get("next_cursor")

    def upload_sync_package(self, package: dict[str, Any]) -> SyncAck:
        data = self._json(self._request("POST", f"{API}/stations/{self._station_id}/sync", json=package))
        if data.get("status") != "ACCEPTED":
            raise LanServerRejected(f"Server did not accept sync package: {data.get('status')!r}")
        try:
            conflicts = tuple(
                (str(item["module_qr"]), QrOwner.from_json(item.get("owner", {})))
                for item in data.get("qr_conflicts", [])
            )
        except (KeyError, TypeError) as exc:
            raise LanServerRejected(f"Malformed sync acknowledgement: {exc}") from exc
        return SyncAck(qr_conflicts=conflicts)

    def login_password(self, user_id: str, password: str, *, jig_id: str) -> RemoteUser:
        return self._login(f"{API}/auth/login", {"user_id": user_id, "password": password, "station_id": self._station_id, "jig_id": jig_id})

    def login_badge(self, badge_id: str, *, jig_id: str) -> RemoteUser:
        return self._login(f"{API}/auth/badge", {"badge_id": badge_id, "station_id": self._station_id, "jig_id": jig_id})

    def _login(self, url: str, body: dict[str, Any]) -> RemoteUser:
        response = self._request("POST", url, json=body, allowed={401, 403, 423})
        data = self._json(response)
        if response.status_code in {401, 403, 423}:
            raise LanAuthRejected(str(data.get("error", response.status_code)), str(data.get("message") or "Login refused by the server"))
        try:
            role = str(data["role"]).upper()
            if role not in {"OPERATOR", "SUPERVISOR"}:
                raise ValueError(role)
            return RemoteUser(user_id=str(data["user_id"]), display_name=str(data.get("display_name") or data["user_id"]), role=role)
        except (KeyError, ValueError) as exc:
            raise LanServerRejected(f"Malformed login response from server: {exc}") from exc

    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, *, allowed: set[int] = frozenset(), timeout: float | None = None, **kwargs) -> httpx.Response:
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise LanServerUnavailable(f"LAN server unreachable: {exc}") from exc

        if response.status_code in allowed or response.is_success:
            return response
        if response.status_code >= 500:
            raise LanServerUnavailable(f"LAN server error {response.status_code} on {url}")
        raise LanServerRejected(f"LAN server rejected {method} {url}: {response.status_code} {response.text[:300]}")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise LanServerRejected("LAN server returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LanServerRejected("LAN server returned an unexpected JSON shape")
        return data
