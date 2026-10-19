
"""Approved station-to-Golden-Rig binding."""

from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timezone

_FINGERPRINT = re.compile(r"^[0-9a-fA-F]{64}$")
_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalise_rig_host(value: str) -> str:
    """Validate a Golden Rig address: IPv4, IPv6, DNS or mDNS (.local) name.

    Returns the canonical form (names lower-cased, trailing dot removed).
    Raises ValueError with an operator-readable message.
    """
    import ipaddress

    host = (value or "").strip()
    if not host:
        raise ValueError("Enter the Golden Rig's mDNS name (e.g. golden-rig-01.local) or IP address")
    if "://" in host or "/" in host or " " in host:
        raise ValueError(f"{host!r} is not a host name or IP address (no http://, paths or spaces)")
    bracketed = host.startswith("[") and host.endswith("]")
    candidate = host[1:-1] if bracketed else host
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    if ":" in host:
        raise ValueError(f"{host!r}: enter the port separately, not as part of the address")
    name = host.rstrip(".").lower()
    labels = name.split(".")
    if len(name) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError(f"{host!r} is not a valid host name (letters, digits and '-' between dots)")
    if all(label.isdigit() for label in labels):
        raise ValueError(f"{host!r} is not a valid IP address")
    return name


def is_mdns_name(host: str) -> bool:
    return host.lower().rstrip(".").endswith(".local")

class GoldenRigBindingError(Exception):
    """Invalid or unauthorized Golden Rig binding."""

class GoldenRigNotBoundError(GoldenRigBindingError):
    """No usable approved binding exists."""

class GoldenRigAuthorizationError(GoldenRigBindingError):
    """User is not authorized to change rig bindings."""


@dataclass(frozen=True, slots=True)
class GoldenRigBinding:
    station_id: str
    rig_id: str
    host: str
    port: int
    certificate_sha256: str
    approved_by: str
    approved_at: datetime
    enabled: bool = True

    def __post_init__(self) -> None:

        for name in ("station_id", "rig_id", "host", "approved_by"):
            value = getattr(self, name)

            if not isinstance(value, str) or not value.strip():
                raise GoldenRigBindingError(f"{name} is required")

        try:
            object.__setattr__(self, "host", normalise_rig_host(self.host))
        except ValueError as exc:
            raise GoldenRigBindingError(str(exc)) from exc

        if (isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535):
            raise GoldenRigBindingError("Invalid Golden Rig TCP port")

        if (not isinstance(self.certificate_sha256, str) or not _FINGERPRINT.fullmatch(self.certificate_sha256)):
            raise GoldenRigBindingError("Certificate SHA-256 must contain exactly 64 hexadecimal characters")

        if (not isinstance(self.approved_at, datetime) or self.approved_at.tzinfo is None):
            raise GoldenRigBindingError("Approval time must be timezone-aware")

        if not isinstance(self.enabled, bool):
            raise GoldenRigBindingError("enabled must be Boolean")

        object.__setattr__(self, "certificate_sha256", self.certificate_sha256.lower())

    def require_station(self, station_id: str) -> None:
        if self.station_id != station_id:
            raise GoldenRigNotBoundError("Golden Rig binding belongs to another station")

        if not self.enabled:
            raise GoldenRigNotBoundError("Golden Rig binding is disabled")
