"""golden rig binding tables

The Supervisor-approved station-to-Golden-Rig binding and its audit trail
(SqliteGoldenRigBindingStore). Previously created only by test bootstrap.

Revision ID: c7d24f8e1a35
Revises: b41c7e2a9d10
Create Date: 2026-10-12 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d24f8e1a35'
down_revision: Union[str, Sequence[str], None] = 'b41c7e2a9d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "golden_rig_bindings",
        sa.Column("station_id", sa.String(length=128), primary_key=True),
        sa.Column("rig_id", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("certificate_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=False),
        sa.Column("approved_at", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "golden_rig_binding_audit",
        sa.Column("audit_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "station_id",
            sa.String(length=128),
            sa.ForeignKey("golden_rig_bindings.station_id"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("golden_rig_binding_audit")
    op.drop_table("golden_rig_bindings")
