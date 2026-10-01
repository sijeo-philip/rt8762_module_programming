"""SQLAlchemy persistence models for the programming station.

These classes define how station state is stored locally.

They are persistence models, not domain objects. Manufacturing rules
remain in stationapp.domain. Repository code added later will translate
between domain objects and these database objects.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stationapp.data.base import Base

def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    
    return datetime.now(timezone.utc)
    
    

# ================================================================================
# Authourized MAC allocations
# ================================================================================

class AllocationModel(Base):
    """One server-issued authorisation document."""
    
    __tablename__ = "allocations"
    
    allocation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    
    station_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    
    jig_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    
    document_digest: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    
    addresses: Mapped[list["AllocatedMacModel"]] = relationship(back_populates="allocation", cascade="all, delete-orphan")
    
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('STOCK_RF_TEST', 'PRICOL_PRODUCTION')", 
            name = "ck_allocations_purpose",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_allocations_schema_version",
        ),
    )
    
    
class AllocatedMacModel(Base):
    """ One MAC address imported from an authourized allocation"""
    
    __tablename__ = "allocated_macs"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    allocation_id: Mapped[str] = mapped_column(ForeignKey( "allocations.allocation_id", ondelete="RESTRICT"), nullable=False, index=True)
    
    
    # Canonical Storage form:
    #
    #   AABBCCDDEEFF
    #
    # Keeping a single canonical representation makes the UNIQUE constraint
    # meaningful regardless of how the address was originally supplied
    
    address: Mapped[str] = mapped_column(String(12), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="AVAILABLE", index=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.batch_id", ondelete="RESTRICT"), nullable= True, index=True)
    slot_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    module_qr: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    programming_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allocation: Mapped["AllocationModel"] = relationship(back_populates="addresses")
    
    __table_args__ = (
        UniqueConstraint(
            "address",
            name="uq_allocated_macs_address",
        ),
        CheckConstraint(
            "purpose IN ('STOCK_RF_TEST', 'PRICOL_PRODUCTION')",
            name="ck_allocated_macs_purpose", 
        ),
        CheckConstraint(
            """
            status IN (
                'AVAILABLE',
                'RESERVED',
                'PROGRAMMING',
                'ISSUED',
                'CONFIRMED',
                'HOLD'
                )
            """,
            name = "ck_allocated_macs_status",
        ),
        CheckConstraint(
            "slot_number IS NULL OR slot_number > 0", 
            name="ck_allocated_macs_slot_positive",
        ),
        UniqueConstraint(
            "batch_id",
            "slot_number",
            "purpose",
            name="uq_allocated_macs_batch_slot_purpose",
        ),
        Index(
            "ix_allocated_macs_available",
            "purpose",
            "status",
        ),
    )
    
# =======================================================================================
# Production batches and physical slots
# =======================================================================================

class BatchModel(Base):
    """ One complete 4-up or 8-up jig cycle."""

    __tablename__ = "batches"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    jig_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    slot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False, default="CREATED", index=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    slots: Mapped[list["BatchSlotModel"]] = relationship(back_populates="batch", cascade="all, delete-orphan", order_by="BatchSlotModel.slot_number")
    __table_args__ = ( 
        CheckConstraint(
            "slot_count IN (4, 8)",
            name="ck_batches_slot_count", 
        ),
        Index(
            "ix_batches_station_created",
            "station_id",
            "created_at",
        ),
    )

class BatchSlotModel(Base):
    """ Persistent representation of one physical jig position"""

    __tablename__ = "batch_slots"
    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.batch_id", ondelete="CASCADE"), nullable=False, index=True)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    port_identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    device_state: Mapped[str] = mapped_column(String(64), nullable=False, default="EMPTY")
    module_qr: Mapped[str | None] = mapped_column(String(256), nullable=True, unique=True)
    pricol_app_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pricol_dfu_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch: Mapped["BatchModel"] = relationship(back_populates="slots")
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "slot_number",
            name="uq_batch_slots_batch_slot",
        ),
        UniqueConstraint(
            "batch_id",
            "port_identity",
            name="uq_batch_slots_batch_port",
        ),
        CheckConstraint(
            "slot_number > 0",
            name="ck_batch_slots_slot_positive",
        ),
    )

# ==================================================================================================
# Programming evidence
# ==================================================================================================

class ProgrammingEventModel(Base):
     
     
    """Evidence for one programming attempt."""

    __tablename__ = "programming_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.batch_id", ondelete="RESTRICT"), nullable=False, index=True)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False )
    requested_mac: Mapped[str] = mapped_column(String(12), nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    succeeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint(
           "slot_number > 0",
            name="ck_programming_events_slot_positive",
        ),
        CheckConstraint(
            "purpose IN ('STOCK_RF_TEST', 'PRICOL_PRODUCTION')",
            name="ck_programming_events_purpose",
        ),
        Index(
            "ix_programming_events_batch_slot",
            "batch_id",
            "slot_number",
        ),
    )


# =============================================================================
# Verification evidence
# =============================================================================

class VerificationEventModel(Base):

    """Evidence from the two identity verification gates."""

    __tablename__ = "verification_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.batch_id", ondelete="RESTRICT"), nullable=False, index=True)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    verification_type: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_mac: Mapped[str] = mapped_column(String(12), nullable=False)
    reported_mac: Mapped[str | None] = mapped_column(String(12), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            """
            verification_type IN (
                'STOCK_RF',
                'PRICOL_READBACK'
            )
            """,
            name="ck_verification_events_type",
        ),
        CheckConstraint(
            "slot_number > 0",
            name="ck_verification_events_slot_positive",
        ),
        Index(
            "ix_verification_events_batch_slot",
            "batch_id",
            "slot_number",
        ),
    )


# =============================================================================
# Manufacturing history
# =============================================================================


class ManufacturingEventModel(Base):
    """Append-oriented manufacturing event record.

    Lesson 6 will place the repository rules around this table so historical
    records can be inserted but not silently edited or deleted.
    """

    __tablename__ = "manufacturing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.batch_id", ondelete="RESTRICT"), nullable=True, index=True)
    slot_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    __table_args__ = (
        CheckConstraint(
            "slot_number IS NULL OR slot_number > 0",
            name="ck_manufacturing_events_slot_positive",
        ),
    )


# =============================================================================
# Audit history
# =============================================================================


class AuditRecordModel(Base):
    """Record of a controlled user/master-data change."""

    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    station_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(128), nullable=False)
    record_id: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        Index(
            "ix_audit_records_record",
            "record_type",
            "record_id",
        ),
    )


# =============================================================================
# Durable upload queue
# =============================================================================


class OutboxModel(Base):
    """A transaction waiting to be uploaded to the central server."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_transaction_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint(
            """
            status IN (
                'PENDING',
                'RETRY',
                'UPLOADED'
            )
            """,
            name="ck_outbox_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_attempt_count",
        ),
        Index(
            "ix_outbox_pending",
            "status",
            "next_attempt_at",
        ),
    )

