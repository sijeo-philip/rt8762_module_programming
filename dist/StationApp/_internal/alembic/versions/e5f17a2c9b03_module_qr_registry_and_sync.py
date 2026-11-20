"""module QR registry and LAN sync state

Adds the station-wide module QR registry (back-filled from every QR
already bound in batch_slots), sync cursors, and the sync run history.

Revision ID: e5f17a2c9b03
Revises: d93a51c0b7e4
Create Date: 2026-10-13 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f17a2c9b03'
down_revision: Union[str, Sequence[str], None] = 'd93a51c0b7e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "module_qr_registry",
        sa.Column("module_qr", sa.String(length=256), primary_key=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("station_id", sa.String(length=128), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column("slot_number", sa.Integer(), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_status", sa.String(length=16), nullable=False),
        sa.Column("conflict_detail", sa.Text(), nullable=True),
        sa.CheckConstraint("source IN ('LOCAL', 'SERVER')", name="ck_module_qr_registry_source"),
        sa.CheckConstraint(
            "server_status IN ('UNCONFIRMED', 'CLAIMED', 'SYNCED', 'CONFLICT')",
            name="ck_module_qr_registry_server_status",
        ),
    )
    op.create_index("ix_module_qr_registry_batch_id", "module_qr_registry", ["batch_id"])
    op.create_index("ix_module_qr_registry_server_status", "module_qr_registry", ["server_status"])

    # Every QR this station has already bound is known from the start.
    op.execute(
        """
        INSERT INTO module_qr_registry
            (module_qr, source, station_id, batch_id, slot_number, bound_at, server_status)
        SELECT s.module_qr, 'LOCAL', b.station_id, s.batch_id, s.slot_number,
               COALESCE(b.committed_at, b.created_at), 'UNCONFIRMED'
        FROM batch_slots s JOIN batches b ON b.batch_id = s.batch_id
        WHERE s.module_qr IS NOT NULL
        """
    )

    op.create_table(
        "sync_state",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modules", sa.Integer(), nullable=False),
        sa.Column("batches", sa.Integer(), nullable=False),
        sa.Column("events", sa.Integer(), nullable=False),
        sa.Column("audits", sa.Integer(), nullable=False),
        sa.Column("qr_conflicts", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING', 'SUCCESS', 'FAILED')", name="ck_sync_runs_status"),
    )


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("sync_state")
    op.drop_index("ix_module_qr_registry_server_status", table_name="module_qr_registry")
    op.drop_index("ix_module_qr_registry_batch_id", table_name="module_qr_registry")
    op.drop_table("module_qr_registry")
