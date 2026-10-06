import pytest

from stationapp.drivers.serial.errors import (
    SerialSessionClosedError,
    SerialTimeoutError,
)
from stationapp.drivers.serial.session import (
    SerialSession,
    SerialSessionConfig,
)

from stationapp.concurrency.cancellation import (
    CancellationToken,
    OperationCancelled,
)



class FakeSerialBackend:

    def __init__(self) -> None:
        self._is_open = False

        self.rx = bytearray()
        self.tx = bytearray()

        self.input_reset_count = 0
        self.output_reset_count = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def in_waiting(self) -> int:
        return len(self.rx)

    def open(self) -> None:
        self._is_open = True

    def close(self) -> None:
        self._is_open = False

    def read(
        self,
        size: int = 1,
    ) -> bytes:

        if not self.rx:
            return b""

        count = min(
            size,
            len(self.rx),
        )

        result = bytes(
            self.rx[:count]
        )

        del self.rx[:count]

        return result

    def write(
        self,
        data: bytes,
    ) -> int:

        self.tx.extend(data)

        return len(data)

    def reset_input_buffer(
        self,
    ) -> None:
        self.input_reset_count += 1
        self.rx.clear()

    def reset_output_buffer(
        self,
    ) -> None:
        self.output_reset_count += 1
        self.tx.clear()

def test_config_normalises_port_name():
    config = SerialSessionConfig(
        port="com17",
    )

    assert config.port == "COM17"

def test_session_open_and_close():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    )

    assert session.is_open is False

    session.open()

    assert session.is_open is True

    session.close()

    assert session.is_open is False

def test_open_and_close_are_idempotent():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    )

    session.open()
    session.open()

    assert session.is_open is True

    session.close()
    session.close()

    assert session.is_open is False

def test_context_manager_closes_session():

    backend = FakeSerialBackend()

    with SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    ) as session:

        assert session.is_open is True

    assert backend.is_open is False

def test_write_rejected_when_closed():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    )

    with pytest.raises(
        SerialSessionClosedError
    ):
        session.write(b"AT\r\n")

def test_write_line_adds_configured_newline():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7",
            newline=b"\r\n",
        ),
        backend=backend,
    )

    session.open()

    session.write_line(
        "AT+TEST"
    )

    assert bytes(backend.tx) == (
        b"AT+TEST\r\n"
    )

class ReplyingFakeSerialBackend(
    FakeSerialBackend
):

    def __init__(
        self,
        reply: bytes,
    ) -> None:

        super().__init__()

        self.reply = reply

    def write(
        self,
        data: bytes,
    ) -> int:

        written = super().write(
            data
        )

        self.rx.extend(
            self.reply
        )

        return written

def test_query_writes_command_and_reads_reply():

    backend = ReplyingFakeSerialBackend(
        b"OK\r\n"
    )

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    )

    session.open()

    response = session.query(
        "AT",
        timeout_seconds=1.0,
    )

    assert response == "OK"

    assert bytes(backend.tx) == (
        b"AT\r\n"
    )

def test_read_timeout_is_reported():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7",
            read_timeout_seconds=0.01,
        ),
        backend=backend,
    )

    session.open()

    with pytest.raises(
        SerialTimeoutError
    ):
        session.read_until(
            delimiter=b"\r\n",
            timeout_seconds=0.02,
        )

def test_read_line_returns_text():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    )

    session.open()

    backend.rx.extend(
        b"+VERSION:1.0\r\n"
    )

    result = session.read_line(
        timeout_seconds=1.0,
    )

    assert result == "+VERSION:1.0"

def test_read_available_returns_buffered_data():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    )

    session.open()

    backend.rx.extend(
        b"ABCDEF"
    )

    result = session.read_available()

    assert result == b"ABCDEF"

def test_read_observes_cancellation():

    backend = FakeSerialBackend()

    session = SerialSession(
        config=SerialSessionConfig(
            port="COM7"
        ),
        backend=backend,
    )

    session.open()

    token = CancellationToken()

    token.cancel()

    with pytest.raises(
        OperationCancelled
    ):
        session.read_until(
            delimiter=b"\r\n",
            timeout_seconds=1.0,
            cancellation_token=token,
        )


