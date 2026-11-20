"""add operation log

Revision ID: 6897c860425c
Revises: 9ef5d63e2281
Create Date: 2026-10-02 16:52:48.605415

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6897c860425c'
down_revision: Union[str, Sequence[str], None] = '9ef5d63e2281'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operation_logs",

        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
        ),

        sa.Column(
            "operation_id",
            sa.String(length=64),
            nullable=False,
        ),

        sa.Column(
            "station_id",
            sa.String(length=128),
            nullable=False,
        ),

        sa.Column(
            "level",
            sa.String(length=16),
            nullable=False,
        ),

        sa.Column(
            "category",
            sa.String(length=64),
            nullable=False,
        ),

        sa.Column(
            "event_type",
            sa.String(length=128),
            nullable=False,
        ),

        sa.Column(
            "message",
            sa.Text(),
            nullable=False,
        ),

        sa.Column(
            "batch_id",
            sa.String(length=64),
            nullable=True,
        ),

        sa.Column(
            "slot_number",
            sa.Integer(),
            nullable=True,
        ),

        sa.Column(
            "correlation_id",
            sa.String(length=64),
            nullable=True,
        ),

        sa.Column(
            "user_id",
            sa.String(length=128),
            nullable=True,
        ),

        sa.Column(
            "detail",
            sa.JSON(),
            nullable=True,
        ),

        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.CheckConstraint(
            """
            level IN (
                'DEBUG',
                'INFO',
                'WARNING',
                'ERROR',
                'CRITICAL'
            )
            """,
            name="ck_operation_logs_level",
        ),

        sa.CheckConstraint(
            "slot_number IS NULL OR slot_number > 0",
            name="ck_operation_logs_slot_positive",
        ),

        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batches.batch_id"],
            ondelete="RESTRICT",
        ),

        sa.PrimaryKeyConstraint(
            "id",
        ),

        sa.UniqueConstraint(
            "operation_id",
        ),
    )

    op.create_index(
        "ix_operation_logs_station_id",
        "operation_logs",
        ["station_id"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_level",
        "operation_logs",
        ["level"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_category",
        "operation_logs",
        ["category"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_event_type",
        "operation_logs",
        ["event_type"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_batch_id",
        "operation_logs",
        ["batch_id"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_correlation_id",
        "operation_logs",
        ["correlation_id"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_user_id",
        "operation_logs",
        ["user_id"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_occurred_at",
        "operation_logs",
        ["occurred_at"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_station_time",
        "operation_logs",
        ["station_id", "occurred_at"],
        unique=False,
    )

    op.create_index(
        "ix_operation_logs_batch_slot",
        "operation_logs",
        ["batch_id", "slot_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operation_logs_batch_slot",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_station_time",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_occurred_at",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_user_id",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_correlation_id",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_batch_id",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_event_type",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_category",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_level",
        table_name="operation_logs",
    )

    op.drop_index(
        "ix_operation_logs_station_id",
        table_name="operation_logs",
    )

    op.drop_table(
        "operation_logs"
    )


