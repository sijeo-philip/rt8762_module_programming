
"""Transactional persistence of Golden Rig evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stationapp.services.golden_rig_service import ( GoldenRigTestReport )

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.golden_rig_evidence_repository import (
    GoldenRigEvidenceRepository,
)
from stationapp.data.unit_of_work import UnitOfWork


class GoldenRigEvidenceService:

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(self, report: GoldenRigTestReport) -> bool:
        with UnitOfWork(self._session_factory) as uow:
            repository = GoldenRigEvidenceRepository(uow.session)
            inserted = repository.record(report)
            uow.commit()
            return inserted

    def get(self, request_id: str) -> dict | None:
        with UnitOfWork(self._session_factory) as uow:
            repository = GoldenRigEvidenceRepository(uow.session)
            return repository.get(request_id)
