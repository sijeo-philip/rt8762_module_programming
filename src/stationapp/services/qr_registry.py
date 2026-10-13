"""Line-wide duplicate module QR prevention.

Three layers, strongest available first:

1. Local registry: every QR bound on this station, plus every QR the
   LAN server has reported from other stations. Checked on every scan;
   its primary key also makes a second local binding impossible.
2. Online claim: while the LAN server is reachable, each scan claims
   the QR on the server before it is bound. The server grants a QR to
   exactly one station, so two stations cannot bind the same code.
3. Offline fallback: if the server is unreachable, production continues
   (README: local production may continue) and the binding is marked
   UNCONFIRMED. The next sync submits it; a server-reported conflict is
   marked CONFLICT and raised to the Supervisor.

After a failed claim the server is not asked again for a short back-off
period, so a server outage does not slow every scan.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from stationapp.drivers.lan.client import LanServerClient, LanServerRejected, LanServerUnavailable

logger = logging.getLogger(__name__)

UNCONFIRMED = "UNCONFIRMED"
CLAIMED = "CLAIMED"


class DuplicateQrError(Exception):
    """The QR is already bound to another module."""


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    server_status: str
    detail: str


class ModuleQrClaimService:
    """Server claim with an offline circuit breaker."""

    def __init__(self, *, client: LanServerClient | None, jig_id: str, claim_timeout_seconds: float = 2.0, offline_backoff_seconds: float = 60.0, enabled: bool = True) -> None:
        self._client = client
        self._jig_id = jig_id
        self._timeout = claim_timeout_seconds
        self._backoff = offline_backoff_seconds
        self._enabled = enabled and client is not None
        self._offline_until = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def claim(self, module_qr: str, *, batch_id: str, slot_number: int) -> ClaimDecision:
        if not self._enabled:
            return ClaimDecision(UNCONFIRMED, "LAN server not configured")

        with self._lock:
            if time.monotonic() < self._offline_until:
                return ClaimDecision(UNCONFIRMED, "LAN server offline; QR will be confirmed at next sync")

        assert self._client is not None
        try:
            result = self._client.claim_module_qr(
                module_qr,
                jig_id=self._jig_id,
                batch_id=batch_id,
                slot_number=slot_number,
                timeout_seconds=self._timeout,
            )
        except LanServerUnavailable as exc:
            with self._lock:
                self._offline_until = time.monotonic() + self._backoff
            logger.warning("QR claim skipped, LAN server unavailable: %s", exc)
            return ClaimDecision(UNCONFIRMED, "LAN server offline; QR will be confirmed at next sync")
        except LanServerRejected as exc:
            logger.error("QR claim rejected by LAN server: %s", exc)
            return ClaimDecision(UNCONFIRMED, f"LAN server rejected the claim request: {exc}")

        if not result.claimed:
            where = result.owner.describe() if result.owner else "another station"
            raise DuplicateQrError(f"Duplicate QR: {module_qr} is already bound at {where} (LAN server)")

        with self._lock:
            self._offline_until = 0.0
        return ClaimDecision(CLAIMED, "claimed on LAN server")
