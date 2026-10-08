
"""Transport-independent and TCP communication for the Golden Rig.

The transport exchanges opaque bytes. The JSON protocol codec
is implemented separately in Lesson 9D.
"""

from __future__ import annotations

import socket
import struct
import threading

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


MAX_FRAME_SIZE = 64 * 1024
FRAME_HEADER_SIZE = 4


class GoldenRigTransportError(Exception):
    """Base error for rig communication failures."""


class GoldenRigConnectionError(GoldenRigTransportError):
    """The Golden Rig could not be reached."""


class GoldenRigTimeoutError(GoldenRigTransportError):
    """The requested operation exceeded its deadline."""


class GoldenRigFrameError(GoldenRigTransportError):
    """Invalid or incomplete network message."""


class GoldenRigCancelledError(GoldenRigTransportError):
    """Operation cancelled by the station."""


class GoldenRigTransport(Protocol):
    """Interface shared by real and simulated transports."""

    def exchange(self, payload: bytes, *, cancel_event: threading.Event | None = None) -> bytes:
        """Send one request and receive one complete response."""
        ...


@dataclass(frozen=True, slots=True)
class GoldenRigEndpoint:
    host: str
    port: int = 8762

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("Golden Rig host is required")

        if (isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535):
            raise ValueError("Invalid Golden Rig TCP port")


def encode_frame(payload: bytes) -> bytes:
    if not isinstance(payload, bytes):
        raise TypeError("Payload must be bytes")

    if not 0 < len(payload) <= MAX_FRAME_SIZE:
        raise GoldenRigFrameError(
            "Payload size is outside allowed limits"
        )

    return struct.pack("!I", len(payload)) + payload


def _check_cancel(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise GoldenRigCancelledError("Golden Rig operation cancelled")


@dataclass(slots=True)
class TcpGoldenRigTransport:
    endpoint: GoldenRigEndpoint
    connect_timeout: float = 3.0
    io_timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0 or self.io_timeout <= 0:
            raise ValueError("Timeouts must be positive")

    def exchange(self, payload: bytes, *, cancel_event: threading.Event | None = None) -> bytes:
        frame = encode_frame(payload)
        _check_cancel(cancel_event)

        try:
            connection = socket.create_connection((self.endpoint.host, self.endpoint.port), timeout=self.connect_timeout)
        except socket.timeout as exc:
            raise GoldenRigTimeoutError("Golden Rig connection timed out") from exc
        except OSError as exc:
            raise GoldenRigConnectionError("Cannot connect to Golden Rig") from exc

        with connection:
            try:
                connection.settimeout(self.io_timeout)

                _check_cancel(cancel_event)
                connection.sendall(frame)
                _check_cancel(cancel_event)

                header = self._receive_exact(connection, FRAME_HEADER_SIZE, cancel_event)

                payload_length = struct.unpack("!I", header)[0]

                if not 0 < payload_length <= MAX_FRAME_SIZE:
                    raise GoldenRigFrameError("Invalid Golden Rig response size")

                return self._receive_exact(connection, payload_length, cancel_event)

            except socket.timeout as exc:
                raise GoldenRigTimeoutError("Golden Rig communication timed out") from exc
            except OSError as exc:
                raise GoldenRigConnectionError("Golden Rig communication failed") from exc

    @staticmethod
    def _receive_exact(connection: socket.socket, count: int, cancel_event: threading.Event | None) -> bytes:
        data = bytearray()

        while len(data) < count:
            _check_cancel(cancel_event)

            chunk = connection.recv(min(4096, count - len(data)))

            if not chunk:
                raise GoldenRigFrameError("Golden Rig closed connection early")
            data.extend(chunk)
        return bytes(data)


class SimulatedGoldenRigTransport:
    """In-memory transport for tests and development."""

    def __init__(self, responder: Callable[[bytes], bytes]) -> None:
        self._responder = responder
        self.requests: list[bytes] = []

    def exchange(self, payload: bytes, *, cancel_event: threading.Event | None = None) -> bytes:
        encode_frame(payload)
        _check_cancel(cancel_event)
        self.requests.append(payload)
        response = self._responder(payload)
        _check_cancel(cancel_event)
        encode_frame(response)

        return response
