
"""Strict JSON codec for the Golden Module Rig protocol v1."""

from __future__ import annotations

import json

from stationapp.domain.golden_rig import (
    GOLDEN_RIG_PROTOCOL_VERSION,
    GoldenRigProtocolError,
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
)
from stationapp.domain.mac import MacAddress
from stationapp.infrastructure.golden_rig.transport import MAX_FRAME_SIZE


class GoldenRigJsonCodec:
    """Convert typed protocol objects to and from JSON bytes."""

    @staticmethod
    def _encode(data: dict) -> bytes:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
        if not 0 < len(raw) <= MAX_FRAME_SIZE:
            raise GoldenRigProtocolError("JSON message exceeds frame limit")
        return raw

    @staticmethod
    def _pairs_no_duplicates(pairs):
        result = {}

        for key, value in pairs:
            if key in result:
                raise GoldenRigProtocolError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    @classmethod
    def _decode(cls, raw: bytes) -> dict:
        if not isinstance(raw, bytes):
            raise GoldenRigProtocolError(
                "Protocol payload must be bytes"
            )

        if not 0 < len(raw) <= MAX_FRAME_SIZE:
            raise GoldenRigProtocolError(
                "Invalid JSON payload size"
            )

        try:
            value = json.loads(
                raw.decode(
                    "utf-8",
                    errors="strict",
                ),
                object_pairs_hook=cls._pairs_no_duplicates,
                parse_constant=cls._reject_constant,
            )

        except GoldenRigProtocolError:
            # Preserve specific protocol validation errors,
            # including duplicate JSON keys and invalid constants.
            raise

        except (UnicodeDecodeError, ValueError) as exc:
            raise GoldenRigProtocolError(
                "Malformed Golden Rig JSON"
            ) from exc

        if not isinstance(value, dict):
            raise GoldenRigProtocolError(
                "JSON root must be an object"
            )

        return value

    @staticmethod
    def _reject_constant(value):
        raise GoldenRigProtocolError(f"Nonstandard JSON constant: {value}")

    @staticmethod
    def _fields(data: dict, expected: set[str], optional: set[str] | None = None) -> None:
        optional = optional or set()
        actual = set(data)

        missing = expected - actual
        unknown = actual - expected - optional

        if missing or unknown:
            raise GoldenRigProtocolError(
                f"Invalid JSON fields: "
                f"missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}"
            )

    @staticmethod
    def _integer(value, name: str) -> int:
        if type(value) is not int:
            raise GoldenRigProtocolError(f"{name} must be an integer")
        return value

    @staticmethod
    def _string(value, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GoldenRigProtocolError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _mac(value, name: str) -> MacAddress:
        if not isinstance(value, str):
            raise GoldenRigProtocolError(f"{name} must be a MAC string")
        try:
            return MacAddress.parse(value)
        except ValueError as exc:
            raise GoldenRigProtocolError(f"Invalid {name}") from exc

    @classmethod
    def encode_request(cls, request: GoldenRigRequest) -> bytes:

        if not isinstance(request, GoldenRigRequest):
            raise GoldenRigProtocolError("Expected GoldenRigRequest")

        return cls._encode({
            "protocol_version": request.protocol_version,
            "message_type": "RF_TEST_REQUEST",
            "request_id": request.request_id,
            "batch_id": request.batch_id,
            "station_id": request.station_id,
            "jig_id": request.jig_id,
            "targets": [
                {
                    "slot_number": target.slot_number,
                    "expected_mac": str(target.expected_mac),
                }
                for target in request.targets
            ],
        })

    @classmethod
    def decode_request(cls, raw: bytes) -> GoldenRigRequest:

        data = cls._decode(raw)

        cls._fields(data, {
            "protocol_version",
            "message_type",
            "request_id",
            "batch_id",
            "station_id",
            "jig_id",
            "targets",
        })

        if data["message_type"] != "RF_TEST_REQUEST":
            raise GoldenRigProtocolError("Unexpected request message type")
        version = cls._integer(data["protocol_version"],"protocol_version")
        if version != GOLDEN_RIG_PROTOCOL_VERSION:
            raise GoldenRigProtocolError("Unsupported protocol version")

        items = data["targets"]

        if not isinstance(items, list):
            raise GoldenRigProtocolError("targets must be a JSON array")

        targets = []

        for item in items:
            if not isinstance(item, dict):
                raise GoldenRigProtocolError("Invalid target object")

            cls._fields(item, {"slot_number", "expected_mac"})

            targets.append(GoldenRigTarget(slot_number=cls._integer(item["slot_number"], "slot_number"),
                expected_mac=cls._mac(item["expected_mac"], "expected_mac"),
            ))

        return GoldenRigRequest(request_id=cls._string(data["request_id"], "request_id"),
            batch_id=cls._string(data["batch_id"], "batch_id"),
            station_id=cls._string(data["station_id"], "station_id"),
            jig_id=cls._string(data["jig_id"], "jig_id"),
            targets=tuple(targets),
            protocol_version=version,
        )

    @classmethod
    def encode_response(cls, response: GoldenRigResponse) -> bytes:

        if not isinstance(response, GoldenRigResponse):
            raise GoldenRigProtocolError("Expected GoldenRigResponse")

        return cls._encode({
            "protocol_version": response.protocol_version,
            "message_type": "RF_TEST_RESPONSE",
            "request_id": response.request_id,
            "rig_id": response.rig_id,
            "results": [
                {
                    "slot_number": item.slot_number,
                    "expected_mac": str(item.expected_mac),
                    "reported_mac": (
                        str(item.reported_mac)
                        if item.reported_mac is not None
                        else None
                    ),
                    "connected": item.connected,
                    "disconnected": item.disconnected,
                    "error_code": item.error_code,
                    "detail": item.detail,
                }
                for item in response.results
            ],
        })

    @classmethod
    def decode_response(cls, raw: bytes, *, request: GoldenRigRequest, approved_rig_id: str) -> GoldenRigResponse:
        data = cls._decode(raw)
        cls._fields(data, {
            "protocol_version",
            "message_type",
            "request_id",
            "rig_id",
            "results",
        })

        if data["message_type"] != "RF_TEST_RESPONSE":
            raise GoldenRigProtocolError("Unexpected response message type")
        version = cls._integer(data["protocol_version"],"protocol_version")

        if version != GOLDEN_RIG_PROTOCOL_VERSION:
            raise GoldenRigProtocolError("Unsupported response protocol version")

        rig_id = cls._string(data["rig_id"], "rig_id")
        if rig_id != approved_rig_id:
            raise GoldenRigProtocolError("Response rig ID does not match approved rig")

        items = data["results"]
        if not isinstance(items, list):
            raise GoldenRigProtocolError("results must be a JSON array")
        results = []
        for item in items:
            if not isinstance(item, dict):
                raise GoldenRigProtocolError("Invalid result object")

            cls._fields(item, {
                "slot_number",
                "expected_mac",
                "reported_mac",
                "connected",
                "disconnected",
                "error_code",
                "detail",
            })

            reported = item["reported_mac"]
            error_code = item["error_code"]

            if reported is not None:
                reported = cls._mac(reported, "reported_mac")

            if (error_code is not None and not isinstance(error_code, str)):
                raise GoldenRigProtocolError("error_code must be a string or null")

            if type(item["connected"]) is not bool:
                raise GoldenRigProtocolError("connected must be Boolean")

            if type(item["disconnected"]) is not bool:
                raise GoldenRigProtocolError("disconnected must be Boolean")

            if not isinstance(item["detail"], str):
                raise GoldenRigProtocolError("detail must be a string")

            results.append(GoldenRigSlotResult(slot_number=cls._integer(item["slot_number"], "slot_number"),
                expected_mac=cls._mac(item["expected_mac"], "expected_mac"),
                reported_mac=reported,
                connected=item["connected"],
                disconnected=item["disconnected"],
                error_code=error_code,
                detail=item["detail"],
            ))

        response = GoldenRigResponse(request_id=cls._string(data["request_id"], "request_id"),
            rig_id=rig_id,
            results=tuple(results),
            protocol_version=version,
        )

        # Validate the entire response before anybody
        # processes individual slot results.
        response.validate_against(request)

        return response
