
"""Authenticated Golden Rig TLS transport."""

from __future__ import annotations

import hashlib
import hmac
import math
import socket
import ssl
import struct
import threading
import time

from dataclasses import dataclass
from pathlib import Path

from stationapp.domain.golden_rig_binding import GoldenRigBinding

from stationapp.infrastructure.golden_rig.transport import (
    FRAME_HEADER_SIZE,
    MAX_FRAME_SIZE,
    GoldenRigCancelledError,
    GoldenRigConnectionError,
    GoldenRigFrameError,
    GoldenRigTimeoutError,
    GoldenRigTransportError,
    encode_frame,
)


class GoldenRigAuthenticationError(GoldenRigTransportError):
    """Rig identity or certificate verification failed."""


@dataclass(slots=True)
class SecureGoldenRigTransport:
    binding: GoldenRigBinding
    ca_certificate: Path

    connect_timeout: float = 3.0
    io_timeout: float = 10.0
    operation_timeout: float = 30.0
    poll_interval: float = 0.2

    def __post_init__(self) -> None:
        for value in (
            self.connect_timeout,
            self.io_timeout,
            self.operation_timeout,
            self.poll_interval,
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timeout values must be finite and positive")

    def exchange(self, payload: bytes, *, cancel_event: threading.Event | None = None) -> bytes:

        frame = encode_frame(payload)
        deadline = time.monotonic() + self.operation_timeout
        self._check(cancel_event, deadline)
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.ca_certificate))
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        try:
            with socket.create_connection((self.binding.host, self.binding.port),
                timeout=min(
                    self.connect_timeout,
                    self._remaining(deadline),
                ),
            ) as raw_socket:

                raw_socket.settimeout(
                    min(
                        self.io_timeout,
                        self._remaining(deadline),
                    )
                )

                with context.wrap_socket(raw_socket, server_hostname=self.binding.host, do_handshake_on_connect=False) as connection:
                    self._handshake(connection, cancel_event, deadline)
                    self._verify_certificate(connection)

                    # No payload is transmitted before verification.
                    self._send_all(connection, frame, cancel_event, deadline)
                    header = self._read_exact(connection, FRAME_HEADER_SIZE, cancel_event, deadline)
                    length = struct.unpack("!I", header)[0]
                    if not 0 < length <= MAX_FRAME_SIZE:
                        raise GoldenRigFrameError("Invalid Golden Rig response length")
                    return self._read_exact(connection, length, cancel_event, deadline)

        except ssl.SSLCertVerificationError as exc:
            raise GoldenRigAuthenticationError("Golden Rig TLS certificate rejected") from exc

        except ssl.SSLError as exc:
            raise GoldenRigAuthenticationError("Golden Rig TLS authentication failed") from exc

        except socket.timeout as exc:
            raise GoldenRigTimeoutError("Golden Rig operation timed out") from exc

        except OSError as exc:
            raise GoldenRigConnectionError("Golden Rig connection failed") from exc

    def _verify_certificate(self,connection: ssl.SSLSocket) -> None:
        certificate = connection.getpeercert(binary_form=True)
        if not certificate:
            raise GoldenRigAuthenticationError("Golden Rig certificate missing")
        observed = hashlib.sha256(certificate).hexdigest()
        expected = self.binding.certificate_sha256
        if not hmac.compare_digest(observed, expected):
            raise GoldenRigAuthenticationError(
                "Connected Golden Rig does not match "
                "the supervisor-approved certificate"
            )

    def _handshake(self, connection: ssl.SSLSocket, cancel_event: threading.Event | None, deadline: float) -> None:

        while True:
            self._check(cancel_event, deadline)
            connection.settimeout(self._step_timeout(deadline))

            try:
                connection.do_handshake()
                return
            except socket.timeout:
                continue

    def _send_all(self, connection: ssl.SSLSocket, data: bytes, cancel_event: threading.Event | None, deadline: float) -> None:

        view = memoryview(data)
        offset = 0
        while offset < len(view):
            self._check(cancel_event, deadline)
            connection.settimeout(self._step_timeout(deadline))
            try:
                sent = connection.send(view[offset:])
            except socket.timeout:
                continue

            if sent == 0:
                raise GoldenRigFrameError("Golden Rig closed during transmission")
            offset += sent

    def _read_exact(self,connection: ssl.SSLSocket, count: int, cancel_event: threading.Event | None, deadline: float) -> bytes:

        data = bytearray()
        while len(data) < count:
            self._check(cancel_event, deadline)
            connection.settimeout(self._step_timeout(deadline))
            try:
                chunk = connection.recv(min(4096, count - len(data)))
            except socket.timeout:
                continue

            if not chunk:
                raise GoldenRigFrameError("Golden Rig response ended early")
            data.extend(chunk)
        return bytes(data)

    def _step_timeout(self, deadline: float) -> float:
        return min(
            self.poll_interval,
            self.io_timeout,
            self._remaining(deadline),
        )

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()

        if remaining <= 0:
            raise GoldenRigTimeoutError("Golden Rig operation deadline exceeded")
        return remaining

    @staticmethod
    def _check(event: threading.Event | None, deadline: float) -> None:
        if event is not None and event.is_set():
            raise GoldenRigCancelledError("Golden Rig operation cancelled")
        SecureGoldenRigTransport._remaining(deadline)
