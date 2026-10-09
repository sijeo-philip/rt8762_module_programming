
"""Local TLS server for Golden Rig integration testing.

Development and test use only. Not a production rig server.
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading

from collections.abc import Callable
from pathlib import Path

from stationapp.infrastructure.golden_rig.transport import (
    MAX_FRAME_SIZE,
    encode_frame,
)


def _recv_exact(connection, length: int) -> bytes:
    data = bytearray()

    while len(data) < length:
        chunk = connection.recv(length - len(data))

        if not chunk:
            raise ConnectionError("Client disconnected before frame completion")
        data.extend(chunk)
    return bytes(data)


class GoldenRigTlsTestServer:
    """One-request TLS test server running on localhost."""

    def __init__(self, *, certificate: Path, private_key: Path, responder: Callable[[bytes], bytes], host: str = "127.0.0.1") -> None:
        self.certificate = Path(certificate)
        self.private_key = Path(private_key)
        self.responder = responder
        self.host = host

        self.port: int | None = None
        self.received: list[bytes] = []
        self.errors: list[Exception] = []

        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Server already started")

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(self.certificate), keyfile=str(self.private_key))

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, 0))
        listener.listen(1)
        listener.settimeout(5.0)
        self.port = listener.getsockname()[1]
        self._listener = listener

        def serve() -> None:
            self._ready.set()

            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(3.0)
                    with context.wrap_socket(connection, server_side=True) as tls_connection:

                        header = _recv_exact(tls_connection, 4)
                        size = struct.unpack("!I", header)[0]
                        if not 0 < size <= MAX_FRAME_SIZE:
                            raise ValueError("Invalid incoming frame length")

                        payload = _recv_exact(tls_connection, size)
                        self.received.append(payload)
                        response = self.responder(payload)
                        tls_connection.sendall(encode_frame(response))

            except Exception as exc:
                self.errors.append(exc)

            finally:
                listener.close()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()

        if not self._ready.wait(timeout=3.0):
            raise RuntimeError("TLS server startup timed out")

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

        if self._thread is not None:
            self._thread.join(timeout=6.0)

            if self._thread.is_alive():
                raise RuntimeError("TLS test server did not stop")

            
