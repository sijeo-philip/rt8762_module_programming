import json

import pytest

from stationapp.drivers.serial.types import (
    DiscoveredSerialPort,
    UsbSerialIdentity,
)
from stationapp.services.slot_binding import (
    InvalidBindingFileError,
    SerialIdentityAlreadyBoundError,
    SlotAlreadyBoundError,
    SlotBindingService,
)


def make_port(
    *,
    device: str,
    serial_number: str,
    location: str,
) -> DiscoveredSerialPort:
    return DiscoveredSerialPort(
        device=device,
        identity=UsbSerialIdentity(
            vid=0x10C4,
            pid=0xEA60,
            serial_number=serial_number,
            location=location,
        ),
        description="Test serial adapter",
        manufacturer="Test Manufacturer",
        product="USB UART",
    )


def make_service(tmp_path):
    return SlotBindingService(
        binding_file=tmp_path / "serial_bindings.json",
        station_id="STATION-01",
        jig_id="JIG-01",
        jig_positions=8,
    )


def test_missing_file_returns_empty_configuration(
    tmp_path,
):
    service = make_service(tmp_path)

    config = service.load()

    assert config.station_id == "STATION-01"
    assert config.jig_id == "JIG-01"
    assert config.jig_positions == 8
    assert config.bindings == ()
    assert config.complete is False


def test_binding_slot_is_persisted(tmp_path):
    service = make_service(tmp_path)

    port = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    service.bind(
        slot_number=1,
        port=port,
    )

    reloaded = service.load()

    binding = reloaded.get(1)

    assert binding is not None
    assert binding.slot_number == 1
    assert binding.stable_key == port.stable_key
    assert binding.serial_number == "PORT001"


def test_com_number_is_not_persisted_as_identity(
    tmp_path,
):
    service = make_service(tmp_path)

    original = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    service.bind(
        slot_number=1,
        port=original,
    )

    raw = json.loads(
        service.binding_file.read_text(
            encoding="utf-8"
        )
    )

    binding = raw["bindings"][0]

    assert binding["stable_key"] == original.stable_key

    # The runtime COM name must not become the permanent slot identity.
    assert "COM7" not in json.dumps(binding)


def test_same_binding_is_idempotent(tmp_path):
    service = make_service(tmp_path)

    port = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    first = service.bind(
        slot_number=1,
        port=port,
    )

    second = service.bind(
        slot_number=1,
        port=port,
    )

    assert first == second


def test_slot_cannot_be_changed_accidentally(
    tmp_path,
):
    service = make_service(tmp_path)

    first = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    second = make_port(
        device="COM8",
        serial_number="PORT002",
        location="1-3.2",
    )

    service.bind(
        slot_number=1,
        port=first,
    )

    with pytest.raises(
        SlotAlreadyBoundError
    ):
        service.bind(
            slot_number=1,
            port=second,
        )


def test_explicit_replace_is_allowed(tmp_path):
    service = make_service(tmp_path)

    first = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    replacement = make_port(
        device="COM8",
        serial_number="PORT002",
        location="1-3.2",
    )

    service.bind(
        slot_number=1,
        port=first,
    )

    config = service.bind(
        slot_number=1,
        port=replacement,
        replace=True,
    )

    assert config.get(1).stable_key == (
        replacement.stable_key
    )


def test_same_identity_cannot_serve_two_slots(
    tmp_path,
):
    service = make_service(tmp_path)

    port = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    service.bind(
        slot_number=1,
        port=port,
    )

    with pytest.raises(
        SerialIdentityAlreadyBoundError
    ):
        service.bind(
            slot_number=2,
            port=port,
        )


def test_invalid_slot_number_is_rejected(
    tmp_path,
):
    service = make_service(tmp_path)

    port = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    with pytest.raises(ValueError):
        service.bind(
            slot_number=9,
            port=port,
        )


def test_unbind_removes_only_selected_slot(
    tmp_path,
):
    service = make_service(tmp_path)

    port1 = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    port2 = make_port(
        device="COM8",
        serial_number="PORT002",
        location="1-3.2",
    )

    service.bind(
        slot_number=1,
        port=port1,
    )

    service.bind(
        slot_number=2,
        port=port2,
    )

    config = service.unbind(1)

    assert config.get(1) is None
    assert config.get(2) is not None


def test_binding_file_must_belong_to_current_station(
    tmp_path,
):
    binding_file = (
        tmp_path / "serial_bindings.json"
    )

    binding_file.write_text(
        json.dumps(
            {
                "format_version": 1,
                "station_id": "OTHER-STATION",
                "jig_id": "JIG-01",
                "jig_positions": 8,
                "bindings": [],
            }
        ),
        encoding="utf-8",
    )

    service = SlotBindingService(
        binding_file=binding_file,
        station_id="STATION-01",
        jig_id="JIG-01",
        jig_positions=8,
    )

    with pytest.raises(
        InvalidBindingFileError
    ):
        service.load()


def test_binding_survives_windows_com_renumbering(
    tmp_path,
):
    service = make_service(tmp_path)

    before_reboot = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    service.bind(
        slot_number=1,
        port=before_reboot,
    )

    after_reboot = make_port(
        device="COM19",
        serial_number="PORT001",
        location="1-3.1",
    )

    saved = service.load().get(1)

    assert saved is not None

    assert (
        saved.stable_key
        == after_reboot.stable_key
    )

