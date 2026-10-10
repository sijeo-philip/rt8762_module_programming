
from __future__ import annotations

import json

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from stationapp.domain.golden_rig import (
    GoldenRigOutcome,
    GoldenRigProtocolError,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
)
from stationapp.domain.golden_rig_binding import (GoldenRigBinding)
from stationapp.domain.mac import MacAddress
from stationapp.infrastructure.golden_rig.codec import (GoldenRigJsonCodec)
from stationapp.services.golden_rig_service import (GoldenRigInfrastructureError, GoldenRigService)


def make_binding(*, station_id="STATION-01", rig_id="GOLDEN-RIG-01", enabled=True):
    return GoldenRigBinding(
        station_id=station_id,
        rig_id=rig_id,
        host="127.0.0.1",
        port=8762,
        certificate_sha256="ab" * 32,
        approved_by="SUP-001",
        approved_at=datetime.now(timezone.utc),
        enabled=enabled,
    )


def make_targets(slots=(1, 2, 4, 7)):
    return tuple(
        GoldenRigTarget(
            slot_number=n,
            expected_mac=MacAddress.parse(
                f"AA:BB:CC:00:00:{n:02X}"
            ),
        )
        for n in slots
    )


class FakeApprovedTransport:
    """In-memory substitute for an authenticated transport."""

    def __init__(self, binding, responder):
        self.binding = binding
        self.responder = responder
        self.requests = []

    def exchange(
        self,
        payload,
        *,
        cancel_event=None,
    ):
        self.requests.append(payload)
        return self.responder(payload)


class FakeTransportProvider:
    def __init__(self, transport):
        self.transport = transport
        self.calls = 0

    def create_transport(self):
        self.calls += 1
        return self.transport


def successful_responder(payload):
    request = GoldenRigJsonCodec.decode_request(payload)

    response = GoldenRigResponse(
        request_id=request.request_id,
        rig_id="GOLDEN-RIG-01",
        results=tuple(
            GoldenRigSlotResult(
                slot_number=target.slot_number,
                expected_mac=target.expected_mac,
                reported_mac=target.expected_mac,
                connected=True,
                disconnected=True,
            )
            for target in request.targets
        ),
    )

    return GoldenRigJsonCodec.encode_response(response)


def make_service(responder=successful_responder):
    transport = FakeApprovedTransport(
        make_binding(),
        responder,
    )
    provider = FakeTransportProvider(transport)

    return GoldenRigService(provider), transport, provider


def execute(service, targets=None):
    return service.run_test(
        batch_id="BATCH-001",
        station_id="STATION-01",
        jig_id="JIG-8UP",
        targets=(
            make_targets()
            if targets is None
            else targets
        ),
    )

@pytest.mark.unit
def test_golden_rig_service_success():
    service, transport, provider = make_service()

    report = execute(service)

    assert provider.calls == 1
    assert len(transport.requests) == 1

    assert report.passed_slots == (1, 2, 4, 7)
    assert report.failed_slots == ()
    assert report.held_slots == ()
    assert report.all_passed is True

    transmitted = GoldenRigJsonCodec.decode_request(
        transport.requests[0]
    )

    assert transmitted.targets == make_targets()
    assert transmitted.station_id == "STATION-01"

@pytest.mark.unit
def test_golden_rig_service_mixed_results():

    def responder(payload):
        request = GoldenRigJsonCodec.decode_request(
            payload
        )

        results = []

        for target in request.targets:
            slot = target.slot_number

            if slot == 2:
                results.append(GoldenRigSlotResult(
                    slot_number=slot,
                    expected_mac=target.expected_mac,
                    reported_mac=None,
                    connected=False,
                    disconnected=False,
                    error_code="CONNECT_FAILED",
                ))

            elif slot == 7:
                results.append(GoldenRigSlotResult(
                    slot_number=slot,
                    expected_mac=target.expected_mac,
                    reported_mac=target.expected_mac,
                    connected=True,
                    disconnected=False,
                    error_code="DISCONNECT_FAILED",
                ))

            else:
                results.append(GoldenRigSlotResult(
                    slot_number=slot,
                    expected_mac=target.expected_mac,
                    reported_mac=target.expected_mac,
                    connected=True,
                    disconnected=True,
                ))

        return GoldenRigJsonCodec.encode_response(
            GoldenRigResponse(
                request_id=request.request_id,
                rig_id="GOLDEN-RIG-01",
                results=tuple(results),
            )
        )

    service, _, _ = make_service(responder)

    report = execute(service)

    assert report.passed_slots == (1, 4)
    assert report.failed_slots == (2,)
    assert report.held_slots == (7,)
    assert report.all_passed is False

@pytest.mark.unit
def test_wrong_request_id_is_rejected():

    def responder(payload):
        request = GoldenRigJsonCodec.decode_request(
            payload
        )

        response = GoldenRigResponse(
            request_id=str(uuid4()),
            rig_id="GOLDEN-RIG-01",
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

        return GoldenRigJsonCodec.encode_response(
            response
        )

    service, _, _ = make_service(responder)

    with pytest.raises(GoldenRigProtocolError, match="request ID mismatch"):
        execute(service)

@pytest.mark.unit
def test_unapproved_rig_response_rejected():

    def responder(payload):
        data = json.loads(
            successful_responder(payload)
        )
        data["rig_id"] = "GOLDEN-RIG-02"

        return json.dumps(data).encode("utf-8")

    service, _, _ = make_service(responder)

    with pytest.raises(GoldenRigProtocolError, match="approved rig"):
        execute(service) 


@pytest.mark.unit
def test_adapter_error_is_infrastructure_fault():

    def responder(payload):
        request = GoldenRigJsonCodec.decode_request(
            payload
        )

        results = []

        for index, target in enumerate(request.targets):
            results.append(GoldenRigSlotResult(
                slot_number=target.slot_number,
                expected_mac=target.expected_mac,
                reported_mac=None,
                connected=False,
                disconnected=False,
                error_code=(
                    "ADAPTER_ERROR"
                    if index == 0
                    else "CONNECT_FAILED"
                ),
            ))

        return GoldenRigJsonCodec.encode_response(
            GoldenRigResponse(
                request_id=request.request_id,
                rig_id="GOLDEN-RIG-01",
                results=tuple(results),
            )
        )

    service, _, _ = make_service(responder)

    with pytest.raises(GoldenRigInfrastructureError, match="ADAPTER_ERROR"):
        execute(service)

@pytest.mark.unit
def test_wrong_station_binding_rejected():
    transport = FakeApprovedTransport(
        make_binding(station_id="STATION-02"),
        successful_responder,
    )

    service = GoldenRigService(
        FakeTransportProvider(transport)
    )

    with pytest.raises(GoldenRigProtocolError):
        execute(service)

    assert transport.requests == []


@pytest.mark.unit
def test_disabled_binding_rejected():
    transport = FakeApprovedTransport(
        make_binding(enabled=False),
        successful_responder,
    )

    service = GoldenRigService(
        FakeTransportProvider(transport)
    )

    with pytest.raises(Exception, match="disabled"):
        execute(service)

    assert transport.requests == []


@pytest.mark.unit
def test_empty_targets_rejected():
    service, transport, provider = make_service()

    with pytest.raises(
        GoldenRigProtocolError,
        match="No eligible Stock MACs",
    ):
        execute(service, targets=())

    assert provider.calls == 0
    assert transport.requests == []



