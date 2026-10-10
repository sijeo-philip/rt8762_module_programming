
"""Golden Rig JSON protocol regression tests."""

import json
from uuid import uuid4

import pytest

from stationapp.domain.golden_rig import (
    GoldenRigProtocolError,
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
)
from stationapp.domain.mac import MacAddress
from stationapp.infrastructure.golden_rig.codec import (
    GoldenRigJsonCodec,
)


RIG_ID = "GOLDEN-RIG-01"


def make_request():
    return GoldenRigRequest(
        request_id=str(uuid4()),
        batch_id="BATCH-001",
        station_id="STATION-01",
        jig_id="JIG-8UP",
        targets=tuple(
            GoldenRigTarget(
                slot_number=n,
                expected_mac=MacAddress.parse(
                    f"AA:BB:CC:00:00:{n:02X}"
                ),
            )
            for n in (1, 2, 4, 7)
        ),
    )


def make_response(request):
    return GoldenRigResponse(
        request_id=request.request_id,
        rig_id=RIG_ID,
        results=tuple(
            GoldenRigSlotResult(
                slot_number=t.slot_number,
                expected_mac=t.expected_mac,
                reported_mac=t.expected_mac,
                connected=True,
                disconnected=True,
            )
            for t in request.targets
        ),
    )


def decode(request, payload):
    return GoldenRigJsonCodec.decode_response(
        payload,
        request=request,
        approved_rig_id=RIG_ID,
    )


@pytest.mark.unit
def test_request_roundtrip():
    request = make_request()

    encoded = GoldenRigJsonCodec.encode_request(request)
    decoded = GoldenRigJsonCodec.decode_request(encoded)

    assert decoded == request
    assert len(decoded.targets) == 4


@pytest.mark.unit
def test_response_roundtrip():
    request = make_request()
    response = make_response(request)

    encoded = GoldenRigJsonCodec.encode_response(response)
    decoded = decode(request, encoded)

    assert decoded == response
    assert decoded.all_passed


@pytest.mark.unit
def test_wrong_request_id_rejected():
    request = make_request()
    response = make_response(request)

    data = json.loads(
        GoldenRigJsonCodec.encode_response(response)
    )
    data["request_id"] = str(uuid4())

    with pytest.raises(
        GoldenRigProtocolError,
        match="request ID mismatch",
    ):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_unapproved_rig_id_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_response(
            make_response(request)
        )
    )
    data["rig_id"] = "GOLDEN-RIG-UNAPPROVED"

    with pytest.raises(
        GoldenRigProtocolError,
        match="approved rig",
    ):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_missing_slot_result_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_response(
            make_response(request)
        )
    )
    data["results"].pop()

    with pytest.raises(
        GoldenRigProtocolError,
        match="slot or expected MAC mismatch",
    ):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_duplicate_slot_result_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_response(
            make_response(request)
        )
    )
    data["results"][1]["slot_number"] = 1

    with pytest.raises(
        GoldenRigProtocolError,
        match="Duplicate slot",
    ):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_unexpected_expected_mac_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_response(
            make_response(request)
        )
    )
    data["results"][0]["expected_mac"] = (
        "AA:BB:CC:00:00:99"
    )

    with pytest.raises(GoldenRigProtocolError):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_boolean_slot_number_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_request(request)
    )
    data["targets"][0]["slot_number"] = True

    with pytest.raises(GoldenRigProtocolError):
        GoldenRigJsonCodec.decode_request(
            json.dumps(data).encode()
        )


@pytest.mark.unit
def test_duplicate_json_keys_rejected():
    request = make_request()

    with pytest.raises(
        GoldenRigProtocolError,
        match="Duplicate JSON key",
    ):
        decode(
            request,
            b'{"protocol_version":1,'
            b'"protocol_version":1}',
        )


@pytest.mark.unit
def test_unknown_field_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_response(
            make_response(request)
        )
    )
    data["unexpected"] = "value"

    with pytest.raises(
        GoldenRigProtocolError,
        match="Invalid JSON fields",
    ):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_malformed_json_rejected():
    request = make_request()

    with pytest.raises(GoldenRigProtocolError):
        decode(request, b'{"results":')


@pytest.mark.unit
def test_oversized_message_rejected():
    request = make_request()

    with pytest.raises(GoldenRigProtocolError):
        decode(request, b"X" * 65537)


@pytest.mark.unit
def test_invalid_connection_flags_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_response(
            make_response(request)
        )
    )
    data["results"][0]["connected"] = "true"

    with pytest.raises(GoldenRigProtocolError):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_wrong_message_type_rejected():
    request = make_request()
    data = json.loads(
        GoldenRigJsonCodec.encode_response(
            make_response(request)
        )
    )
    data["message_type"] = "RF_TEST_REQUEST"

    with pytest.raises(
        GoldenRigProtocolError,
        match="message type",
    ):
        decode(request, json.dumps(data).encode())


@pytest.mark.unit
def test_nonstandard_json_constant_rejected():
    request = make_request()

    with pytest.raises(
        GoldenRigProtocolError,
        match="Nonstandard JSON constant",
    ):
        decode(
            request,
            b'{"protocol_version":NaN}',
        )