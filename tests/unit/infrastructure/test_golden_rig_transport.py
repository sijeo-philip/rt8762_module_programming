
import socket
import struct
import threading

import pytest

from stationapp.infrastructure.golden_rig.transport import (
    MAX_FRAME_SIZE,
    GoldenRigCancelledError,
    GoldenRigConnectionError,
    GoldenRigEndpoint,
    GoldenRigFrameError,
    SimulatedGoldenRigTransport,
    TcpGoldenRigTransport,
    encode_frame,
    GoldenRigTimeoutError,
)


@pytest.mark.unit
def test_encode_frame() -> None:
    payload = b'{"test":1}'

    frame = encode_frame(payload)

    assert len(frame) == len(payload) + 4

    size = struct.unpack("!I", frame[:4])[0]

    assert size == len(payload)
    assert frame[4:] == payload


@pytest.mark.unit
def test_empty_payload_rejected() -> None:
    with pytest.raises(GoldenRigFrameError):
        encode_frame(b"")


@pytest.mark.unit
def test_oversized_payload_rejected() -> None:
    with pytest.raises(GoldenRigFrameError):
        encode_frame(b"A" * (MAX_FRAME_SIZE + 1))


@pytest.mark.unit
def test_simulated_transport() -> None:
    transport = SimulatedGoldenRigTransport(responder=lambda payload: b"ACK:" + payload)

    response = transport.exchange(b"HELLO")

    assert response == b"ACK:HELLO"
    assert transport.requests == [b"HELLO"]


@pytest.mark.unit
def test_simulated_transport_cancellation() -> None:
    event = threading.Event()
    event.set()

    transport = SimulatedGoldenRigTransport(responder=lambda payload: b"OK")

    with pytest.raises(GoldenRigCancelledError):
        transport.exchange(b"HELLO", cancel_event=event)

    assert transport.requests == []


@pytest.mark.unit
def test_invalid_endpoint_rejected() -> None:
    with pytest.raises(ValueError):
        GoldenRigEndpoint(host="", port=8762)

    with pytest.raises(ValueError):
        GoldenRigEndpoint( host="127.0.0.1", port=70000 )



@pytest.mark.unit
def test_tcp_transport_loopback() -> None:

    ready = threading.Event()
    server_address = {}
    errors = []

    def server() -> None:
        try:
            with socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            ) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(3.0)

                server_address["port"] = (
                    listener.getsockname()[1]
                )
                ready.set()

                connection, _ = listener.accept()

                with connection:
                    connection.settimeout(3.0)

                    def read_exact(count: int) -> bytes:
                        data = bytearray()

                        while len(data) < count:
                            chunk = connection.recv(
                                count - len(data)
                            )
                            if not chunk:
                                raise RuntimeError(
                                    "Unexpected EOF"
                                )
                            data.extend(chunk)

                        return bytes(data)

                    header = read_exact(4)
                    size = struct.unpack("!I", header)[0]

                    received = read_exact(size)
                    assert received == b"PING"

                    connection.sendall(
                        encode_frame(b"PONG")
                    )

        except Exception as exc:
            errors.append(exc)
            ready.set()

    worker = threading.Thread(
        target=server,
        daemon=True,
    )
    worker.start()

    assert ready.wait(timeout=3.0)
    assert not errors

    transport = TcpGoldenRigTransport(
        endpoint=GoldenRigEndpoint(
            host="127.0.0.1",
            port=server_address["port"],
        ),
        connect_timeout=2.0,
        io_timeout=2.0,
    )

    response = transport.exchange(b"PING")

    worker.join(timeout=3.0)

    assert not worker.is_alive()
    assert not errors
    assert response == b"PONG"


@pytest.mark.unit
def test_unreachable_golden_rig_is_station_fault() -> None:
    """
    An unreachable Golden Rig may produce either:
    - Connection refused
    - Connection timeout

    Both are infrastructure failures, not DUT failures.
    """

    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    ) as listener:
        listener.bind(("127.0.0.1", 0))
        unused_port = listener.getsockname()[1]

    transport = TcpGoldenRigTransport(
        endpoint=GoldenRigEndpoint(
            host="127.0.0.1",
            port=unused_port,
        ),
        connect_timeout=1.0,
        io_timeout=1.0,
    )

    with pytest.raises(
        (
            GoldenRigConnectionError,
            GoldenRigTimeoutError,
        )
    ):
        transport.exchange(b"PING")


@pytest.mark.unit
def test_connection_refused_is_classified(
    monkeypatch,
) -> None:

    def refused(*args, **kwargs):
        raise ConnectionRefusedError(
            "Connection refused"
        )

    monkeypatch.setattr(
        socket,
        "create_connection",
        refused,
    )

    transport = TcpGoldenRigTransport(
        endpoint=GoldenRigEndpoint(
            host="127.0.0.1",
            port=8762,
        ),
    )

    with pytest.raises(
        GoldenRigConnectionError
    ):
        transport.exchange(b"PING")

@pytest.mark.unit
def test_connection_timeout_is_classified(
    monkeypatch,
) -> None:

    def timed_out(*args, **kwargs):
        raise socket.timeout(
            "Connection timed out"
        )

    monkeypatch.setattr(
        socket,
        "create_connection",
        timed_out,
    )

    transport = TcpGoldenRigTransport(
        endpoint=GoldenRigEndpoint(
            host="127.0.0.1",
            port=8762,
        ),
    )

    with pytest.raises(
        GoldenRigTimeoutError
    ):
        transport.exchange(b"PING")

