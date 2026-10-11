
"""Durable Golden Rig protocol-v1 evidence receipts."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stationapp.services.golden_rig_service import (GoldenRigTestReport)

import hashlib
import json
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from stationapp.data.models import (BatchModel, ManufacturingEventModel)


EVENT_TYPE = "GOLDEN_RIG_RF_RECEIVED"

# Stable namespace for deriving evidence event identifiers.
_NAMESPACE = UUID("b1e4eb4a-c38b-4cde-89ab-269306eb95c1")


class GoldenRigEvidenceConflict(RuntimeError):
    """Same request UUID appears with different evidence."""


class GoldenRigEvidenceBatchMissing(RuntimeError):
    """RF receipt refers to a batch not persisted locally."""


def evidence_event_id(request_id: str) -> str:
    return str(uuid5(_NAMESPACE, str(UUID(request_id))))


def _report_payload(report: GoldenRigTestReport) -> dict:
    request = report.request
    response = report.response

    return {
        "schema_version": 1,
        "request": {
            "request_id": request.request_id,
            "batch_id": request.batch_id,
            "station_id": request.station_id,
            "jig_id": request.jig_id,
            "protocol_version": request.protocol_version,
            "targets": sorted(
                [
                    {
                        "slot_number": target.slot_number,
                        "expected_mac": target.expected_mac.compact,
                    }
                    for target in request.targets
                ],
                key=lambda item: item["slot_number"],
            ),
        },
        "response": {
            "request_id": response.request_id,
            "rig_id": response.rig_id,
            "protocol_version": response.protocol_version,
            "results": sorted(
                [
                    {
                        "slot_number": result.slot_number,
                        "expected_mac": result.expected_mac.compact,
                        "reported_mac": (
                            result.reported_mac.compact
                            if result.reported_mac is not None
                            else None
                        ),
                        "connected": result.connected,
                        "disconnected": result.disconnected,
                        "error_code": result.error_code,
                        "detail": result.detail,
                        "outcome": result.outcome.value,
                    }
                    for result in response.results
                ],
                key=lambda item: item["slot_number"],
            ),
        },
    }


def _digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


class GoldenRigEvidenceRepository:
    """Session-bound, append-only Golden Rig receipt storage.

    Transaction commit belongs to the surrounding UnitOfWork.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, report: GoldenRigTestReport) -> bool:
        """Store a new report.

        Return True when inserted, False for an identical replay.
        Raise if a request ID is reused with different evidence.
        """
        request = report.request
        response = report.response

        response.validate_against(request)

        event_id = evidence_event_id(request.request_id)
        payload = _report_payload(report)
        digest = _digest(payload)

        # Refuse evidence for a missing or foreign batch.
        batch = self._session.get(BatchModel, request.batch_id)

        if batch is None:
            raise GoldenRigEvidenceBatchMissing(f"Batch {request.batch_id} is not persisted")

        if (batch.station_id != request.station_id or batch.jig_id != request.jig_id):
            raise GoldenRigEvidenceConflict("RF evidence station/jig identity mismatch")

        existing = self._session.scalar(select(ManufacturingEventModel).where(ManufacturingEventModel.event_id == event_id))

        if existing is not None:
            stored = existing.payload or {}

            if (existing.event_type != EVENT_TYPE or existing.batch_id != request.batch_id or stored.get("digest") != digest or stored.get("evidence") != payload):
                raise GoldenRigEvidenceConflict(f"Conflicting RF evidence for request {request.request_id}")

            return False

        model = ManufacturingEventModel(
            event_id=event_id,
            batch_id=request.batch_id,
            slot_number=None,
            event_type=EVENT_TYPE,
            correlation_id=request.request_id,
            payload={
                "digest": digest,
                "evidence": payload,
            },
        )

        self._session.add(model)
        self._session.flush()

        return True

    def get(self, request_id: str) -> dict | None:
        """Return the stored evidence envelope."""
        event_id = evidence_event_id(request_id)

        model = self._session.scalar(
            select(ManufacturingEventModel).where(
                ManufacturingEventModel.event_id == event_id
            )
        )

        if model is None:
            return None

        if model.event_type != EVENT_TYPE:
            raise GoldenRigEvidenceConflict(
                "Evidence identifier has unexpected event type"
            )

        envelope = model.payload or {}

        if (
            not isinstance(envelope.get("evidence"), dict)
            or envelope.get("digest") != _digest(
                envelope["evidence"]
            )
        ):
            raise GoldenRigEvidenceConflict(
                "Stored RF evidence digest mismatch"
            )

        return json.loads(json.dumps(envelope))
