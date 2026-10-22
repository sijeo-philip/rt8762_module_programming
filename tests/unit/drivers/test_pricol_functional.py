"""Pricol PRICOLAPP / PRICOLDFU functional test driver."""

from __future__ import annotations

import pytest
import serial

from stationapp.concurrency.cancellation import CancellationToken, OperationCancelled
from stationapp.drivers.pricol.functional import (
    APP_RESPONSE,
    PricolFunctionalTester,
    response_lines,
)
from stationapp.drivers.serial.session import SerialSession


START_LINE = b"### Uart demo interrupt read uart data ###\r\n"


class FakeDut:
    """Scripted UART backend: replies to a written command.

    Like the real module, it prints its start-up line after each reset;
    the reset happens while the station listens (modelled as: after open).
    """

    def __init__(self, replies: dict[bytes, list[bytes]] | None = None, *, boot_noise: bytes = b"", fail_open: bool = False, start_line: bytes = START_LINE) -> None:
        self.replies = replies or {}
        self.start_line = start_line
        self.fail_open = fail_open
        self.written: list[bytes] = []
        self._rx = bytearray(boot_noise)
        self._pending: list[bytes] = []
        self.is_open = False

    @property
    def in_waiting(self) -> int:
        if not self._rx and self._pending:
            # Deliver replies in chunks, as a real UART would.
            self._rx.extend(self._pending.pop(0))
        return len(self._rx)

    def open(self) -> None:
        if self.fail_open:
            raise serial.SerialException("could not open port 'COM9': Access is denied")
        self.is_open = True
        if self.start_line:
            # Two chunks: the station must wait for the complete line.
            half = len(self.start_line) // 2
            self._pending.extend([self.start_line[:half], self.start_line[half:]])

    def close(self) -> None:
        self.is_open = False

    def read(self, size: int = 1) -> bytes:
        data = bytes(self._rx[:size])
        del self._rx[:size]
        return data

    def write(self, data: bytes) -> int:
        self.written.append(data)
        self._pending.extend(self.replies.get(data, []))
        return len(data)

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def reset_output_buffer(self) -> None:
        pass


def make_tester(dut: FakeDut, timeout: float = 0.3, reset_wait: float = 0.5) -> PricolFunctionalTester:
    return PricolFunctionalTester(
        response_timeout_seconds=timeout,
        reset_wait_seconds=reset_wait,
        session_factory=lambda config: SerialSession(config=config, backend=dut),
        poll_seconds=0.001,
    )


APP_OK = [b"PRICOL", b"APP\r\n"]


@pytest.mark.unit
def test_app_passes_on_expected_response_split_across_reads():
    dut = FakeDut({b"PRICOLAPP\r\n": APP_OK})

    outcome = make_tester(dut).run_app_test(slot_number=1, com_port="COM9")

    assert dut.written == [b"PRICOLAPP\r\n"]
    assert outcome.passed is True
    assert outcome.response.strip() == "PRICOLAPP"


@pytest.mark.unit
def test_boot_noise_before_command_is_discarded():
    dut = FakeDut({b"PRICOLAPP\r\n": [b"garbage\r\n"]}, boot_noise=b"PRICOLAPP\r\n")

    outcome = make_tester(dut).run_app_test(slot_number=1, com_port="COM9")

    # A stale "PRICOLAPP" from before the command must not count.
    assert outcome.passed is False
    assert outcome.detail == "unexpected response"


@pytest.mark.unit
def test_dfu_passes_without_trailing_newline():
    dut = FakeDut({b"PRICOLDFU\r\n": [b"PRICOLDFU"]})

    outcome = make_tester(dut).run_dfu_test(slot_number=2, com_port="COM9")

    assert outcome.passed is True
    assert outcome.command == "PRICOLDFU"


@pytest.mark.unit
def test_wrong_response_fails():
    dut = FakeDut({b"PRICOLDFU\r\n": [b"PRICOLAPP\r\n"]})

    outcome = make_tester(dut).run_dfu_test(slot_number=2, com_port="COM9")

    assert outcome.passed is False
    assert "PRICOLAPP" in outcome.response


@pytest.mark.unit
def test_app_test_rejects_dfu_response():
    dut = FakeDut({b"PRICOLAPP\r\n": [b"PRICOLDFU\r\n"]})

    outcome = make_tester(dut).run_app_test(slot_number=1, com_port="COM9")

    assert outcome.passed is False


@pytest.mark.unit
def test_response_must_be_a_whole_line():
    dut = FakeDut({b"PRICOLAPP\r\n": [b"XPRICOLAPP-ERR\r\n"]})

    outcome = make_tester(dut).run_app_test(slot_number=1, com_port="COM9")

    assert outcome.passed is False


@pytest.mark.unit
def test_silent_dut_fails_after_timeout():
    outcome = make_tester(FakeDut(), timeout=0.1).run_app_test(slot_number=3, com_port="COM9")

    assert outcome.passed is False
    assert "no response" in outcome.detail


@pytest.mark.unit
def test_port_failure_is_unverified_not_fail():
    outcome = make_tester(FakeDut(fail_open=True)).run_app_test(slot_number=4, com_port="COM9")

    assert outcome.passed is None
    assert "Access is denied" in outcome.detail


@pytest.mark.unit
def test_cancellation_propagates():
    token = CancellationToken()
    token.cancel()

    with pytest.raises(OperationCancelled):
        make_tester(FakeDut(), timeout=1.0).run_app_test(slot_number=1, com_port="COM9", cancellation_token=token)


@pytest.mark.unit
def test_expected_line_matching_ignores_trailing_space():
    assert APP_RESPONSE in response_lines(b"  PRICOLAPP \r\n")


@pytest.mark.unit
def test_command_is_sent_only_after_the_start_up_line():
    dut = FakeDut({b"PRICOLAPP\r\n": APP_OK})
    unread_at_send = []
    original_write = dut.write

    def write(data):
        unread_at_send.append(bytes(dut._rx) + b"".join(dut._pending))
        return original_write(data)

    dut.write = write
    outcome = make_tester(dut).run_app_test(slot_number=1, com_port="COM9")

    assert outcome.passed is True
    assert unread_at_send == [b""]                              # whole start line read first
    assert "Uart demo interrupt read uart data" in outcome.start_message


@pytest.mark.unit
def test_no_start_up_line_means_module_was_not_reset():
    dut = FakeDut({b"PRICOLAPP\r\n": APP_OK}, start_line=b"")

    outcome = make_tester(dut, reset_wait=0.2).run_app_test(slot_number=1, com_port="COM9")

    assert outcome.passed is False
    assert dut.written == []                                    # nothing sent
    assert "no start-up message" in outcome.detail and "reset the module" in outcome.detail


@pytest.mark.unit
def test_response_on_the_same_line_as_the_start_up_text_passes():
    dut = FakeDut({b"PRICOLAPP\r\n": [b"PRICOLAPP### Uart demo interrupt read uart data ###\r\n"]})
    assert make_tester(dut).run_app_test(slot_number=1, com_port="COM9").passed is True


@pytest.mark.unit
def test_start_up_text_alone_is_not_a_response():
    dut = FakeDut({b"PRICOLDFU\r\n": [START_LINE]})
    outcome = make_tester(dut).run_dfu_test(slot_number=1, com_port="COM9")
    assert outcome.passed is False and outcome.detail == "unexpected response"


@pytest.mark.unit
def test_old_behaviour_without_waiting_is_still_available():
    dut = FakeDut({b"PRICOLAPP\r\n": APP_OK}, start_line=b"")
    tester = PricolFunctionalTester(
        response_timeout_seconds=0.3,
        wait_for_start=False,
        session_factory=lambda config: SerialSession(config=config, backend=dut),
        poll_seconds=0.001,
    )
    assert tester.run_app_test(slot_number=1, com_port="COM9").passed is True
