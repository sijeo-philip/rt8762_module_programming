"""Step-by-step Golden Rig connection check.

Answers "can this PC use the Golden Rig at golden-rig-01.local?" without
running an RF test, one step at a time so a failure says where it broke:

    1. Name lookup      name -> IP (system resolver or mDNS), or IP as given
    2. Connection       TCP port open at that IP
    3. Rig certificate  TLS handshake trusted by the station CA and valid
                        for the name entered; reads the rig ID
    4. Approved binding same rig ID and certificate as the station's
                        approved binding (INFO when there is none)

Steps 2-3 are a handshake-only connection, which the rig treats as normal
(golden_rig_application.md section 3.4).
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from stationapp.domain.golden_rig_binding import GoldenRigBinding
from stationapp.infrastructure.golden_rig.resolver import DEFAULT_RESOLVER, HostResolutionError, HostResolver
from stationapp.services.golden_rig_configuration import (
    GoldenRigIdentity,
    GoldenRigIdentityError,
    GoldenRigIdentityProbe,
    RigEndpoint,
)


class RigCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class RigCheckStep:
    title: str
    status: RigCheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class RigCheckResult:
    target: str
    steps: tuple[RigCheckStep, ...]
    identity: GoldenRigIdentity | None = None

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.status is not RigCheckStatus.FAIL for step in self.steps)

    @property
    def problem(self) -> str | None:
        failed = next((step for step in self.steps if step.status is RigCheckStatus.FAIL), None)
        return f"{failed.title}: {failed.detail}" if failed else None


class _PinnedResolver:
    """Connects to already-resolved addresses and records which one answered."""

    def __init__(self, addresses: list[str]) -> None:
        self._addresses = addresses
        self.connected: str | None = None

    def connect(self, host: str, port: int, *, timeout: float) -> socket.socket:
        last_error: OSError | None = None
        for address in self._addresses:
            try:
                connection = socket.create_connection((address, port), timeout=timeout)
            except OSError as exc:
                last_error = exc
                continue
            self.connected = address
            return connection
        raise last_error or OSError(f"no address for {host}")


class GoldenRigConnectionCheck:
    def __init__(
        self,
        *,
        ca_certificate: Path,
        binding: Callable[[], GoldenRigBinding | None] = lambda: None,
        resolver: HostResolver = DEFAULT_RESOLVER,
        timeout: float = 3.0,
    ) -> None:
        self._ca_certificate = Path(ca_certificate)
        self._binding = binding
        self._resolver = resolver
        self._timeout = timeout

    def check(self, host: str, port: int) -> RigCheckResult:
        steps: list[RigCheckStep] = []

        def done(identity: GoldenRigIdentity | None = None) -> RigCheckResult:
            return RigCheckResult(target, tuple(steps), identity)

        try:
            endpoint = RigEndpoint(host, port)
        except ValueError as exc:
            target = f"{host}:{port}"
            steps.append(RigCheckStep("Address", RigCheckStatus.FAIL, str(exc)))
            return done()
        target = str(endpoint)

        # 1. Name lookup
        try:
            addresses, source = self._resolver.lookup(endpoint.host, endpoint.port)
        except (HostResolutionError, OSError) as exc:
            steps.append(RigCheckStep("Name lookup", RigCheckStatus.FAIL, str(exc)))
            return done()
        if source == "IP address":
            steps.append(RigCheckStep("Name lookup", RigCheckStatus.PASS, f"{endpoint.host} is an IP address - no lookup needed"))
        else:
            steps.append(RigCheckStep("Name lookup", RigCheckStatus.PASS, f"{endpoint.host} -> {', '.join(addresses)} (via {source})"))

        # 2-3. Connection and certificate
        pinned = _PinnedResolver(addresses)
        if not self._ca_certificate.is_file():
            try:
                pinned.connect(endpoint.host, endpoint.port, timeout=self._timeout).close()
            except OSError as exc:
                steps.append(self._connection_failed(endpoint, exc))
                return done()
            steps.append(self._connected(endpoint, pinned.connected))
            steps.append(RigCheckStep("Rig certificate", RigCheckStatus.FAIL, f"Golden Rig CA certificate not found: {self._ca_certificate}"))
            return done()

        probe = GoldenRigIdentityProbe(ca_certificate=self._ca_certificate, resolver=pinned, timeout=self._timeout)
        try:
            identity = probe.probe(endpoint)
        except GoldenRigIdentityError as exc:
            if pinned.connected is None:
                steps.append(self._connection_failed(endpoint, exc.__cause__ or exc))
            else:
                steps.append(self._connected(endpoint, pinned.connected))
                steps.append(RigCheckStep("Rig certificate", RigCheckStatus.FAIL, str(exc)))
            return done()
        steps.append(self._connected(endpoint, pinned.connected))
        steps.append(RigCheckStep(
            "Rig certificate",
            RigCheckStatus.PASS,
            f"{identity.rig_id}, trusted by the station CA ({identity.issuer}), valid for {endpoint.host} until {identity.valid_until}",
        ))

        # 4. Approved binding
        binding = self._binding()
        if binding is None or not binding.enabled:
            steps.append(RigCheckStep(
                "Approved binding",
                RigCheckStatus.INFO,
                "This station has no approved Golden Rig. RF tests need a Supervisor to approve one "
                "in the station app (Station configuration -> Golden Rig binding).",
            ))
        elif (identity.rig_id, identity.certificate_sha256) != (binding.rig_id, binding.certificate_sha256):
            steps.append(RigCheckStep(
                "Approved binding",
                RigCheckStatus.FAIL,
                f"This is {identity.rig_id} (SHA-256 {identity.certificate_sha256[:16]}...), but the approved rig is "
                f"{binding.rig_id} (SHA-256 {binding.certificate_sha256[:16]}...). Re-approve only if the rig's certificate was renewed on purpose.",
            ))
        else:
            detail = f"Matches the approved rig {binding.rig_id} (SHA-256 {binding.certificate_sha256[:16]}...)"
            bound = RigEndpoint(binding.host, binding.port)
            if bound != endpoint:
                detail += f"; note: RF tests use the approved address {bound}"
            steps.append(RigCheckStep("Approved binding", RigCheckStatus.PASS, detail))
        return done(identity)

    @staticmethod
    def _connected(endpoint: RigEndpoint, address: str | None) -> RigCheckStep:
        return RigCheckStep("Connection", RigCheckStatus.PASS, f"TCP port {endpoint.port} open at {address}")

    @staticmethod
    def _connection_failed(endpoint: RigEndpoint, exc: BaseException) -> RigCheckStep:
        return RigCheckStep(
            "Connection",
            RigCheckStatus.FAIL,
            f"No answer on TCP port {endpoint.port} ({exc}). Is the Golden Rig application running, "
            "and is the port open in the rig's firewall?",
        )


def simulated_rig_check(host: str, port: int) -> RigCheckResult:
    """Simulation mode: no network is used."""
    return RigCheckResult(
        f"{host}:{port}",
        (RigCheckStep("Simulated Golden Rig", RigCheckStatus.PASS, "simulation - no network check is made"),),
    )
