"""Station Configuration -> Golden Rig Binding (README section 1A).

1. A logged-in Supervisor discovers candidate rigs or enters host/IP.
2. The station requests the rig's identity: a TLS handshake verified
   against the station's Golden Rig CA (the trust configuration). The
   rig ID is the certificate common name; the certificate SHA-256 is
   the identity that gets pinned.
3. The identity must also appear in the approved rig inventory, when
   one is configured.
4. The Supervisor approves; the binding and its audit record are stored
   atomically by SqliteGoldenRigBindingStore.
5. Before each session the bound rig is probed again and must present
   exactly the approved certificate.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stationapp.domain.audit import AuditRecord
from stationapp.domain.golden_rig_binding import normalise_rig_host
from stationapp.infrastructure.golden_rig.resolver import DEFAULT_RESOLVER, HostResolutionError, HostResolver
from stationapp.domain.golden_rig_binding import GoldenRigBinding
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import AuthorizationError
from stationapp.services.access import StationAccessService
from stationapp.services.golden_rig_secure_binding import (
    AuthenticatedSupervisor,
    SecureGoldenRigBindingService,
)

logger = logging.getLogger(__name__)


class GoldenRigIdentityError(Exception):
    """The rig could not be reached or did not prove a trusted identity."""


@dataclass(frozen=True, slots=True)
class RigEndpoint:
    host: str
    port: int

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not 1 <= int(self.port) <= 65535:
            raise ValueError("Golden Rig port must be 1-65535")
        # IPv4, IPv6, DNS or mDNS (.local) name.
        object.__setattr__(self, "host", normalise_rig_host(self.host))
        object.__setattr__(self, "port", int(self.port))

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_endpoints(spec: str) -> tuple[RigEndpoint, ...]:
    """'host:port,host:port' -> endpoints."""
    endpoints = []
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        host, _, port = item.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"Invalid Golden Rig endpoint {item!r}; expected host:port")
        endpoints.append(RigEndpoint(host, int(port)))
    return tuple(endpoints)


@dataclass(frozen=True, slots=True)
class GoldenRigIdentity:
    endpoint: RigEndpoint
    rig_id: str
    certificate_sha256: str
    issuer: str
    valid_until: str
    in_inventory: bool
    address: str | None = None       # IP the name resolved to

    @property
    def endpoint_label(self) -> str:
        if self.address and self.address != self.endpoint.host:
            return f"{self.endpoint} ({self.address})"
        return str(self.endpoint)

    @property
    def approvable(self) -> bool:
        return self.in_inventory


class GoldenRigIdentityProbe:
    """Request a rig's authenticated identity over TLS."""

    def __init__(self, *, ca_certificate: Path, inventory: Iterable[str] = (), timeout: float = 3.0, resolver: HostResolver = DEFAULT_RESOLVER) -> None:
        self._resolver = resolver
        self._ca_certificate = Path(ca_certificate)
        self._inventory = frozenset(item.strip().upper() for item in inventory if item.strip())
        self._timeout = timeout

    @property
    def inventory(self) -> frozenset[str]:
        return self._inventory

    def probe(self, endpoint: RigEndpoint) -> GoldenRigIdentity:
        if not self._ca_certificate.is_file():
            raise GoldenRigIdentityError(f"Golden Rig CA certificate not found: {self._ca_certificate}")

        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self._ca_certificate))
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        try:
            with self._resolver.connect(endpoint.host, endpoint.port, timeout=self._timeout) as raw:
                address = raw.getpeername()[0]
                # Certificate checked against the name entered, not the IP.
                with context.wrap_socket(raw, server_hostname=endpoint.host) as connection:
                    details = connection.getpeercert()
                    der = connection.getpeercert(binary_form=True)
        except HostResolutionError as exc:
            raise GoldenRigIdentityError(str(exc)) from exc
        except ssl.SSLCertVerificationError as exc:
            raise GoldenRigIdentityError(
                f"{endpoint} presented a certificate that is not trusted by this station: {exc.verify_message}"
            ) from exc
        except ssl.SSLError as exc:
            raise GoldenRigIdentityError(f"TLS negotiation with {endpoint} failed: {exc}") from exc
        except OSError as exc:
            raise GoldenRigIdentityError(f"Cannot reach {endpoint}: {exc}") from exc

        if not der or not details:
            raise GoldenRigIdentityError(f"{endpoint} did not present a certificate")

        rig_id = _name_field(details.get("subject", ()), "commonName")
        if not rig_id:
            raise GoldenRigIdentityError(f"{endpoint} certificate has no rig ID (common name)")

        return GoldenRigIdentity(
            endpoint=endpoint,
            rig_id=rig_id,
            certificate_sha256=hashlib.sha256(der).hexdigest(),
            issuer=_name_field(details.get("issuer", ()), "commonName") or "?",
            valid_until=str(details.get("notAfter", "?")),
            in_inventory=not self._inventory or rig_id.upper() in self._inventory,
            address=address,
        )


def _name_field(name: Any, field: str) -> str | None:
    for rdn in name:
        for key, value in rdn:
            if key == field:
                return value
    return None


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    endpoint: RigEndpoint
    identity: GoldenRigIdentity | None
    error: str | None = None


class GoldenRigConfigurationService:
    """Supervisor-facing Golden Rig binding operations."""

    def __init__(self, *, station_id: str, store, binding_service: SecureGoldenRigBindingService, probe: GoldenRigIdentityProbe, access: StationAccessService, candidates: tuple[RigEndpoint, ...] = ()) -> None:
        self._station_id = station_id
        self._store = store
        self._bindings = binding_service
        self._probe = probe
        self._access = access
        self._candidates = candidates

    @property
    def inventory(self) -> frozenset[str]:
        return self._probe.inventory

    def current_binding(self) -> GoldenRigBinding | None:
        return self._store.load(self._station_id)

    def history(self) -> list[dict[str, Any]]:
        return list(self._store.list_audits(self._station_id))

    def discover(self) -> tuple[DiscoveryResult, ...]:
        """Probe the configured candidate rigs and the bound rig."""
        self._access.require_supervisor()
        endpoints = list(self._candidates)
        binding = self.current_binding()
        if binding is not None:
            bound = RigEndpoint(binding.host, binding.port)
            if bound not in endpoints:
                endpoints.insert(0, bound)

        results = []
        for endpoint in endpoints:
            try:
                results.append(DiscoveryResult(endpoint, self._probe.probe(endpoint)))
            except GoldenRigIdentityError as exc:
                results.append(DiscoveryResult(endpoint, None, str(exc)))
        return tuple(results)

    def request_identity(self, endpoint: RigEndpoint) -> GoldenRigIdentity:
        self._access.require_supervisor()
        return self._probe.probe(endpoint)

    def approve(self, identity: GoldenRigIdentity, *, reason: str) -> AuditRecord:
        supervisor = self._access.ensure_configuration_change_allowed()
        if not reason.strip():
            raise ValueError("An approval reason is required")
        if not identity.in_inventory:
            raise AuthorizationError(f"Rig {identity.rig_id} is not in the approved Golden Rig inventory")

        # Approve only what is live now, not a stale identity on screen.
        live = self._probe.probe(identity.endpoint)
        if (live.rig_id, live.certificate_sha256) != (identity.rig_id, identity.certificate_sha256):
            raise GoldenRigIdentityError("The rig's identity changed since it was requested; request it again")

        audit = self._bindings.approve(
            supervisor=AuthenticatedSupervisor(supervisor.user_id),
            rig_id=live.rig_id,
            host=live.endpoint.host,
            port=live.endpoint.port,
            certificate_sha256=live.certificate_sha256,
            reason=reason,
        )
        logger.info("Golden Rig approved | rig_id=%s endpoint=%s by=%s", live.rig_id, live.endpoint, supervisor.user_id)
        self._reconfirm_session(f"Golden Rig binding changed: {reason}")
        return audit

    def disable(self, *, reason: str) -> AuditRecord:
        supervisor = self._access.ensure_configuration_change_allowed()
        audit = self._bindings.disable(supervisor=AuthenticatedSupervisor(supervisor.user_id), reason=reason)
        logger.warning("Golden Rig binding disabled | by=%s reason=%s", supervisor.user_id, reason)
        self._reconfirm_session(f"Golden Rig binding disabled: {reason}")
        return audit

    def verify_bound_rig(self, configuration: StationConfiguration) -> tuple[str, ...]:
        """Session-start check: the bound rig presents the approved identity."""
        binding = self.current_binding()
        if binding is None or not binding.enabled:
            return ("No enabled Golden Rig binding",)
        try:
            live = self._probe.probe(RigEndpoint(binding.host, binding.port))
        except GoldenRigIdentityError as exc:
            return (str(exc),)
        problems = []
        if live.certificate_sha256 != binding.certificate_sha256:
            problems.append(f"Rig at {binding.host}:{binding.port} presents a different certificate than the approved one")
        if live.rig_id != binding.rig_id:
            problems.append(f"Rig ID {live.rig_id} does not match approved {binding.rig_id}")
        if configuration.golden_rig_fingerprint != binding.certificate_sha256:
            problems.append("Approved binding changed during review")
        return tuple(problems)

    def _reconfirm_session(self, reason: str) -> None:
        # Binding changes are themselves Supervisor-authorized, so an open
        # session adopts them with an audit record. A disabled binding
        # leaves the session unable to run RF tests until a rig is bound.
        if self._access.session is not None:
            self._access.reconfirm_configuration(reason)


def live_rig_configuration(store, station_id: str) -> tuple[str | None, str | None]:
    binding = store.load(station_id)
    if binding is None or not binding.enabled:
        return None, None
    return binding.rig_id, binding.certificate_sha256
