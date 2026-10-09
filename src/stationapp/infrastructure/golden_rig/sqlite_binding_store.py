
"""Transactional Golden Rig binding and audit repository."""

from __future__ import annotations

import json

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)

from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from stationapp.domain.audit import AuditRecord
from stationapp.domain.golden_rig_binding import GoldenRigBinding


class Base(DeclarativeBase):
    pass


class GoldenRigBindingRow(Base):
    __tablename__ = "golden_rig_bindings"

    station_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    rig_id: Mapped[str] = mapped_column(String(128))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    certificate_sha256: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(128))
    approved_at: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean)


class GoldenRigBindingAuditRow(Base):
    __tablename__ = "golden_rig_binding_audit"

    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(128), ForeignKey("golden_rig_bindings.station_id"))
    user_id: Mapped[str] = mapped_column(String(128))
    record_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class SqliteGoldenRigBindingStore:
    """Persist binding and audit atomically."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

    def create_schema_for_tests(self) -> None:
        """Test bootstrap only; production uses Alembic."""
        Base.metadata.create_all(self._engine)

    def load(self, station_id: str) -> GoldenRigBinding | None:

        with self._session_factory() as session:
            row = session.get(GoldenRigBindingRow, station_id)

            if row is None:
                return None

            return GoldenRigBinding(
                station_id=row.station_id,
                rig_id=row.rig_id,
                host=row.host,
                port=row.port,
                certificate_sha256=row.certificate_sha256,
                approved_by=row.approved_by,
                approved_at=datetime.fromisoformat(row.approved_at),
                enabled=row.enabled,
            )

    def save_with_audit(self, binding: GoldenRigBinding, audit: AuditRecord) -> None:

        if (audit.station_id != binding.station_id or audit.record_id != binding.rig_id or audit.record_type != "GOLDEN_RIG_BINDING"):
            raise ValueError("Binding and audit identity mismatch")

        with self._session_factory.begin() as session:

            row = session.get(GoldenRigBindingRow, binding.station_id)

            if row is None:
                row = GoldenRigBindingRow(station_id=binding.station_id)
                session.add(row)

            row.rig_id = binding.rig_id
            row.host = binding.host
            row.port = binding.port
            row.certificate_sha256 = (binding.certificate_sha256)
            row.approved_by = binding.approved_by
            row.approved_at = (binding.approved_at.isoformat())
            row.enabled = binding.enabled

            audit_row = GoldenRigBindingAuditRow(
                audit_id=audit.audit_id,
                station_id=audit.station_id,
                user_id=audit.user_id,
                record_id=audit.record_id,
                action=audit.action,
                reason=audit.reason,
                previous_value=(json.dumps(audit.previous_value) if audit.previous_value is not None else None),
                new_value=(json.dumps(audit.new_value) if audit.new_value is not None else None),
                created_at=audit.created_at.isoformat(),
            )
            session.add(audit_row)

    def list_audits(self,station_id: str) -> list[dict[str, Any]]:

        with self._session_factory() as session:
            rows = session.scalars(select(GoldenRigBindingAuditRow).where(GoldenRigBindingAuditRow.station_id == station_id)
                .order_by(
                    GoldenRigBindingAuditRow.created_at,
                    GoldenRigBindingAuditRow.audit_id,
                )
            ).all()

            return [
                {
                    "audit_id": row.audit_id,
                    "action": row.action,
                    "user_id": row.user_id,
                    "reason": row.reason,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

