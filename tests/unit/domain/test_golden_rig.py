
"""Unit tests for Golden Module Rig domain contracts."""

from uuid import uuid4

import pytest

from stationapp.domain import (
    GoldenRigOutcome,
    GoldenRigProtocolError,
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
    MacAddress,
)


def make_target(slot: int) -> GoldenRigTarget:
    return GoldenRigTarget(
        slot_number=slot,
        expected_mac=MacAddress.parse(
            f"AA:BB:CC:00:00:{slot:02X}"
        ),
    )


def make_request(slots: tuple[int, ...] = (1, 2, 4, 7)) -> GoldenRigRequest:
    return GoldenRigRequest(
        request_id=str(uuid4()),
        batch_id="BATCH-001",
        station_id="STATION-01",
        jig_id="JIG-8UP",
        targets=tuple(
            make_target(slot)
            for slot in slots
        ),
    )


def make_result(slot: int, *, connected: bool = True, disconnected: bool = True, reported_mac: MacAddress | None = None, error_code: str | None = None) -> GoldenRigSlotResult:
    expected = make_target(slot).expected_mac

    return GoldenRigSlotResult(
        slot_number=slot,
        expected_mac=expected,
        reported_mac=(expected if reported_mac is None and connected else reported_mac),
        connected=connected,
        disconnected=disconnected,
        error_code=error_code,
    )


@pytest.mark.unit
def test_partial_jig_request() -> None:
    request = make_request((1, 2, 4, 7))

    assert len(request.targets) == 4

    assert [item.slot_number for item in request.targets] == [1, 2, 4, 7]

@pytest.mark.unit
def test_duplicate_slot_rejected() -> None:
    with pytest.raises(GoldenRigProtocolError, match="Duplicate slot"):
        GoldenRigRequest(
            request_id=str(uuid4()),
            batch_id="BATCH-001",
            station_id="STATION-01",
            jig_id="JIG-8UP",
            targets=(
                make_target(1),
                make_target(1),
            ),
        )


@pytest.mark.unit
def test_duplicate_mac_rejected() -> None:
    mac = MacAddress.parse("AA:BB:CC:00:00:01")

    with pytest.raises(GoldenRigProtocolError, match="Duplicate MAC"):
        GoldenRigRequest(
            request_id=str(uuid4()),
            batch_id="BATCH-001",
            station_id="STATION-01",
            jig_id="JIG-8UP",
            targets=(
                GoldenRigTarget(1, mac),
                GoldenRigTarget(2, mac),
            ),
        )   


@pytest.mark.unit
def test_successful_bluetooth_connection() -> None:
    result = make_result(1)

    assert result.outcome is GoldenRigOutcome.PASS


@pytest.mark.unit
def test_bluetooth_connection_failure() -> None:
    result = make_result(
        2,
        connected=False,
        disconnected=False,
        error_code="CONNECT_FAILED",
    )

    assert result.outcome is GoldenRigOutcome.FAIL


@pytest.mark.unit
def test_disconnect_failure_is_not_pass() -> None:
    result = make_result(7,connected=True, disconnected=False, error_code="DISCONNECT_FAILED")
    assert result.outcome is GoldenRigOutcome.HOLD


@pytest.mark.unit
def test_reported_identity_mismatch() -> None:
    result = make_result(1, reported_mac=MacAddress.parse("AA:BB:CC:00:00:99"),
    )

    assert result.outcome is GoldenRigOutcome.HOLD


@pytest.mark.unit
def test_missing_peer_identity_is_not_pass() -> None:
    expected = make_target(1).expected_mac

    result = GoldenRigSlotResult(
        slot_number=1,
        expected_mac=expected,
        reported_mac=None,
        connected=True,
        disconnected=True,
    )

    assert result.outcome is not GoldenRigOutcome.PASS       

@pytest.mark.unit
def test_complete_response_matches_request() -> None:
    request = make_request((1, 2, 4, 7))

    response = GoldenRigResponse(
        request_id=request.request_id,
        rig_id="GOLDEN-RIG-01",
        results=tuple(
            make_result(slot)
            for slot in (1, 2, 4, 7)
        ),
    )

    response.validate_against(request)

    assert response.all_passed is True


@pytest.mark.unit
def test_wrong_request_id_rejected() -> None:
    request = make_request((1, 2))

    response = GoldenRigResponse(
        request_id=str(uuid4()),
        rig_id="GOLDEN-RIG-01",
        results=(
            make_result(1),
            make_result(2),
        ),
    )

    with pytest.raises(
        GoldenRigProtocolError,
        match="request ID mismatch",
    ):
        response.validate_against(request)


@pytest.mark.unit
def test_missing_slot_response_rejected() -> None:
    request = make_request((1, 2, 4))

    response = GoldenRigResponse(
        request_id=request.request_id,
        rig_id="GOLDEN-RIG-01",
        results=(
            make_result(1),
            make_result(2),
        ),
    )

    with pytest.raises(GoldenRigProtocolError, match="slot or expected MAC mismatch"):
        response.validate_against(request)


@pytest.mark.unit
def test_wrong_expected_mac_rejected() -> None:
    request = make_request((1,))

    response = GoldenRigResponse(
        request_id=request.request_id,
        rig_id="GOLDEN-RIG-01",
        results=(
            GoldenRigSlotResult(
                slot_number=1,
                expected_mac=MacAddress.parse(
                    "AA:BB:CC:00:00:99"
                ),
                reported_mac=MacAddress.parse(
                    "AA:BB:CC:00:00:99"
                ),
                connected=True,
                disconnected=True,
            ),
        ),
    )

    with pytest.raises(GoldenRigProtocolError):
        response.validate_against(request)

@pytest.mark.unit
def test_unsupported_protocol_version_rejected() -> None:
    with pytest.raises(GoldenRigProtocolError):
        GoldenRigRequest(
            request_id=str(uuid4()),
            batch_id="BATCH-001",
            station_id="STATION-01",
            jig_id="JIG-8UP",
            targets=(make_target(1),),
            protocol_version=99,
        )


@pytest.mark.unit
def test_disconnect_without_connect_rejected() -> None:
    with pytest.raises(GoldenRigProtocolError):
        GoldenRigSlotResult(
            slot_number=1,
            expected_mac=make_target(1).expected_mac,
            reported_mac=None,
            connected=False,
            disconnected=True,
        )


@pytest.mark.unit
def test_empty_request_rejected() -> None:
    with pytest.raises(GoldenRigProtocolError):
        GoldenRigRequest(
            request_id=str(uuid4()),
            batch_id="BATCH-001",
            station_id="STATION-01",
            jig_id="JIG-8UP",
            targets=(),
        )