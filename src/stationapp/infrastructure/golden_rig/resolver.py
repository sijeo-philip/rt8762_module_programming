"""Resolve and connect to a Golden Rig by IP address or (mDNS) host name.

Order of resolution:
    1. IPv4 / IPv6 literal  -> used as is;
    2. system resolver      -> DNS, hosts file, and on Windows 10+/macOS
                               (or Linux with nss-mdns) also ".local" names;
    3. ".local" names only  -> a direct one-shot mDNS query (RFC 6762
                               section 5.1 / 6.7 "legacy unicast"): sent from an
                               ephemeral port to 224.0.0.251:5353; responders
                               reply straight to that port. No extra library
                               or service is needed on the station PC.

The caller connects to the resolved IP but must keep verifying the TLS
certificate against the *name* the Supervisor bound (server_hostname),
so binding by name keeps the same security as binding by IP.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import struct
import time
from collections.abc import Callable

from stationapp.domain.golden_rig_binding import is_mdns_name

logger = logging.getLogger(__name__)

MDNS_GROUP = ("224.0.0.251", 5353)
_TYPE_A = 1
_CLASS_IN = 1
_QU_BIT = 0x8000


class HostResolutionError(OSError):
    """The rig's name could not be resolved to an address."""


# ---------------------------------------------------------------------------
# Minimal mDNS A-record query
# ---------------------------------------------------------------------------


def build_mdns_query(name: str, transaction_id: int = 0) -> bytes:
    header = struct.pack("!HHHHHH", transaction_id, 0, 1, 0, 0, 0)
    question = b"".join(
        bytes([len(label)]) + label.encode("ascii")
        for label in name.rstrip(".").split(".")
    ) + b"\x00"
    # QU bit asks for a unicast reply (RFC 6762 section 5.4).
    return header + question + struct.pack("!HH", _TYPE_A, _CLASS_IN | _QU_BIT)


def _read_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    end = None
    for _ in range(128):                       # guards against pointer loops
        if offset >= len(packet):
            raise ValueError("truncated name")
        length = packet[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:              # compression pointer
            if offset + 1 >= len(packet):
                raise ValueError("truncated pointer")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if end is None:
                end = offset + 2
            offset = pointer
            continue
        offset += 1
        labels.append(packet[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    else:
        raise ValueError("name too long")
    return ".".join(labels), (end if end is not None else offset)


def parse_mdns_a_records(packet: bytes, name: str) -> list[str]:
    """IPv4 addresses for `name` found in an mDNS response packet."""
    if len(packet) < 12:
        return []
    _, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", packet[:12])
    if not flags & 0x8000:                     # not a response
        return []
    wanted = name.rstrip(".").lower()
    offset = 12
    addresses: list[str] = []
    try:
        for _ in range(qdcount):
            _, offset = _read_name(packet, offset)
            offset += 4
        for _ in range(ancount + nscount + arcount):
            record_name, offset = _read_name(packet, offset)
            record_type, record_class, _ttl, length = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            data = packet[offset:offset + length]
            offset += length
            if (
                record_type == _TYPE_A
                and record_class & 0x7FFF == _CLASS_IN
                and length == 4
                and record_name.lower() == wanted
            ):
                address = socket.inet_ntoa(data)
                if address not in addresses:
                    addresses.append(address)
    except (ValueError, struct.error):
        logger.debug("Ignoring malformed mDNS packet")
    return addresses


def mdns_resolve(name: str, timeout: float = 1.5, *, group: tuple[str, int] = MDNS_GROUP) -> list[str]:
    """Ask the local network who has `name` (a .local host name)."""
    transaction_id = int.from_bytes(os.urandom(2), "big")
    query = build_mdns_query(name, transaction_id)
    addresses: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))
        deadline = time.monotonic() + timeout
        sock.sendto(query, group)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                packet, _ = sock.recvfrom(9000)
            except (socket.timeout, TimeoutError):
                break
            except OSError:
                break
            for address in parse_mdns_a_records(packet, name):
                if address not in addresses:
                    addresses.append(address)
            if addresses:
                break
    return addresses


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class HostResolver:
    def __init__(self, *, system: Callable = socket.getaddrinfo, mdns: Callable[[str, float], list[str]] = mdns_resolve, mdns_timeout: float = 1.5) -> None:
        self._system = system
        self._mdns = mdns
        self._mdns_timeout = mdns_timeout

    def resolve(self, host: str, port: int) -> list[str]:
        """IP addresses to try for host, best first."""
        try:
            return [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            pass

        system_error: Exception | None = None
        try:
            infos = self._system(host, port, type=socket.SOCK_STREAM)
            addresses: list[str] = []
            for info in infos:
                address = info[4][0]
                if address not in addresses:
                    addresses.append(address)
            if addresses:
                # IPv4 first: link-local IPv6 (fe80::) results often need an
                # interface scope to connect, rigs are normally on IPv4.
                return sorted(addresses, key=lambda item: ":" in item)
        except (socket.gaierror, UnicodeError, OSError) as exc:
            system_error = exc

        if is_mdns_name(host):
            try:
                addresses = self._mdns(host, self._mdns_timeout)
            except OSError as exc:
                logger.warning("mDNS query for %s failed: %s", host, exc)
                addresses = []
            if addresses:
                logger.info("Resolved %s via mDNS: %s", host, ", ".join(addresses))
                return addresses
            raise HostResolutionError(
                f"Cannot resolve {host}: no answer from the system resolver or mDNS. "
                "Check the rig is powered and on this network, and that UDP port 5353 is not blocked."
            )

        raise HostResolutionError(f"Cannot resolve {host}: {system_error}")

    def connect(self, host: str, port: int, *, timeout: float) -> socket.socket:
        """TCP connection to the first reachable address of host."""
        last_error: OSError | None = None
        for address in self.resolve(host, port):
            try:
                return socket.create_connection((address, port), timeout=timeout)
            except OSError as exc:
                last_error = exc
        raise last_error or HostResolutionError(f"No address for {host}")


DEFAULT_RESOLVER = HostResolver()
