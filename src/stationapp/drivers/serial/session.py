"""Thread-confined serial communication session."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import serial

from stationapp.concurrency.cancellation import (
    CancellationToken,
)
from stationapp.drivers.serial.errors import (
    SerialOpenError,
    SerialReadError,
    SerialSessionClosedError,
    SerialTimeoutError,
    SerialWriteError,
    SerialDriverError,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class SerialSessionConfig:
    """Configuration for one UART session."""

    port: str
    baudrate: int = 115200

    bytesize: int = serial.EIGHTBITS
    parity: str = serial.PARITY_NONE
    stopbits: float = serial.STOPBITS_ONE

    read_timeout_seconds: float = 0.1
    write_timeout_seconds: float = 1.0

    newline: bytes = b"\r\n"

    def __post_init__(self) -> None:
        port = self.port.strip()

        if not port:
            raise ValueError("port cannot be empty")

        if self.baudrate <= 0:
            raise ValueError("baudrate must be positive")

        if self.read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")

        if self.write_timeout_seconds <= 0:
            raise ValueError("write_timeout_seconds must be positive")

        if not self.newline:
            raise ValueError("newline cannot be empty")

        object.__setattr__(self, "port", port.upper())

class SerialBackend(Protocol):
    """Minimal interface required by SerialSession."""

    @property
    def is_open(self) -> bool:
        ...

    @property
    def in_waiting(self) -> int:
        ...

    def open(self) -> None:
        ...

    def close(self) -> None:
        ...

    def read(self, size: int = 1) -> bytes:
        ...

    def write(self, data: bytes) -> int:
        ...

    def reset_input_buffer(self) -> None:
        ...

    def reset_output_buffer(self) -> None:
        ...

def create_pyserial_backend(config: SerialSessionConfig) -> serial.Serial:
    """Create a configured but unopened pyserial instance."""

    backend = serial.Serial()

    backend.port = config.port
    backend.baudrate = config.baudrate
    backend.bytesize = config.bytesize
    backend.parity = config.parity
    backend.stopbits = config.stopbits
    backend.timeout = (config.read_timeout_seconds)
    backend.write_timeout = (config.write_timeout_seconds)
    return backend


class SerialSession:
    """Owns one serial connection.

    A session must be used from one worker thread only.
    It is deliberately not shared between slot operations.
    """

    def __init__(self, *, config: SerialSessionConfig, backend: SerialBackend | None = None) -> None:

        self._config = config
        self._backend = (backend if backend is not None else create_pyserial_backend(config))

    @property
    def config(self) -> SerialSessionConfig:
        return self._config

    @property
    def is_open(self) -> bool:
        return self._backend.is_open

    def open(self) -> None:
        if self._backend.is_open:
            return

        try:
            self._backend.open()

            self._backend.reset_input_buffer()
            self._backend.reset_output_buffer()

        except (serial.SerialException, OSError) as exc:
            raise SerialOpenError(
                f"Could not open serial port "
                f"{self._config.port}: {exc}"
            ) from exc

        logger.info("Serial session opened | port=%s baud=%d", self._config.port, self._config.baudrate)

    def close(self) -> None:
        if not self._backend.is_open:
            return

        try:
            self._backend.close()

        except (
            serial.SerialException,
            OSError,
        ) as exc:
            logger.warning("Serial close failed | port=%s error=%s", self._config.port, exc)

        else:
            logger.info("Serial session closed | port=%s", self._config.port)
    

    def __enter__(self) -> "SerialSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


    def _require_open(self) -> None:
        if not self._backend.is_open:
            raise SerialSessionClosedError(
                f"Serial port "
                f"{self._config.port} "
                "is not open"
            )

    def write(self, data: bytes) -> int:

        self._require_open()
        if not data:
            raise ValueError(
                "serial write data cannot be empty"
            )
        try:
            written = self._backend.write(data)

        except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
            raise SerialWriteError(f"Serial write failed on {self._config.port}: {exc}") from exc

        if written != len(data):
            raise SerialWriteError(
                f"Short serial write on "
                f"{self._config.port}: "
                f"expected {len(data)} bytes, "
                f"wrote {written}"
            )

        logger.debug("Serial TX | port=%s bytes=%d", self._config.port, written)

        return written

    def write_line(self, text: str) -> int:
        command = text.strip()
        if not command:
            raise ValueError("serial command cannot be empty")

        payload = (command.encode("ascii") + self._config.newline)
        return self.write(payload)

    def reset_buffers(self) -> None:
        self._require_open()

        try:
            self._backend.reset_input_buffer()
            self._backend.reset_output_buffer()

        except (serial.SerialException, OSError) as exc:
            raise SerialDriverError(
                f"Could not reset serial buffers "
                f"on {self._config.port}: {exc}"
            ) from exc

    def read_until(self, *, delimiter: bytes, timeout_seconds: float, cancellation_token: (CancellationToken | None) = None, max_bytes: int = 4096) -> bytes:

        self._require_open()
        if not delimiter:
            raise ValueError("delimiter cannot be empty")

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        deadline = (time.monotonic() + timeout_seconds)

        buffer = bytearray()

        while True:

            if cancellation_token is not None:
                cancellation_token.raise_if_cancelled()

            if time.monotonic() >= deadline:
                raise SerialTimeoutError(
                    f"Timed out waiting for "
                    f"{delimiter!r} on "
                    f"{self._config.port}"
                )

            try:
                chunk = self._backend.read(1)

            except (
                serial.SerialException,
                OSError,
            ) as exc:
                raise SerialReadError(
                    f"Serial read failed on "
                    f"{self._config.port}: {exc}"
                ) from exc

            if not chunk:
                continue

            buffer.extend(chunk)

            if delimiter in buffer:
                return bytes(buffer)

            if len(buffer) >= max_bytes:
                raise SerialReadError(
                    f"Serial response exceeded "
                    f"{max_bytes} bytes on "
                    f"{self._config.port}"
                )

    def read_line(self, *, timeout_seconds: float, cancellation_token: (CancellationToken | None) = None, max_bytes: int = 4096) -> str:

        data = self.read_until(
            delimiter=self._config.newline,
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
            max_bytes=max_bytes,
        )

        return data.decode("ascii",errors="replace").strip()

    def query(self, command: str, *, timeout_seconds: float, response_delimiter: bytes | None = None, cancellation_token: (CancellationToken | None) = None) -> str:
        self._require_open()
        self._backend.reset_input_buffer()
        self.write_line(command)
        delimiter = (response_delimiter if response_delimiter is not None else self._config.newline)

        data = self.read_until(delimiter=delimiter, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)
        return data.decode("ascii", errors="replace").strip()

    def read_available(self, *, max_bytes: int = 4096) -> bytes:

        self._require_open()
        waiting = self._backend.in_waiting

        if waiting <= 0:
            return b""

        size = min(waiting, max_bytes)

        try:
            return self._backend.read(size)

        except (serial.SerialException, OSError) as exc:
            raise SerialReadError(
                f"Serial read failed on "
                f"{self._config.port}: {exc}"
            ) from exc
    
    