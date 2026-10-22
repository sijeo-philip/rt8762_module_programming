"""UART line settings used for the Pricol functional test."""

from __future__ import annotations

import pytest
import serial

from stationapp.drivers.serial.session import SerialSessionConfig, create_pyserial_backend


@pytest.mark.unit
def test_115200_8n1_no_flow_control_dtr_and_rts_off():
    backend = create_pyserial_backend(SerialSessionConfig(port="COM9"))
    assert (backend.baudrate, backend.bytesize, backend.parity, backend.stopbits) == (115200, 8, serial.PARITY_NONE, 1)
    assert (backend.xonxoff, backend.rtscts, backend.dsrdtr) == (False, False, False)
    assert backend.dtr is False and backend.rts is False


@pytest.mark.unit
def test_functional_tester_and_settings_default_to_dtr_rts_off(monkeypatch):
    from stationapp.config import get_settings
    from stationapp.drivers.pricol.functional import PricolFunctionalTester

    captured = []
    tester = PricolFunctionalTester(session_factory=lambda config: captured.append(config) or (_ for _ in ()).throw(RuntimeError))
    with pytest.raises(RuntimeError):
        tester.run_app_test(slot_number=1, com_port="COM9")
    assert (captured[0].dtr, captured[0].rts) == (False, False)

    for key in ("PRICOL_UART_DTR", "PRICOL_UART_RTS"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert (settings.pricol_uart_dtr, settings.pricol_uart_rts) == (False, False)
    finally:
        get_settings.cache_clear()


@pytest.mark.unit
def test_driver_default_still_available_for_diagnosis():
    backend = create_pyserial_backend(SerialSessionConfig(port="COM9", dtr=None, rts=None))
    assert backend.dtr is True and backend.rts is True
