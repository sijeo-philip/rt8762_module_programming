"""batch production metadata

Adds the operator-entered batch identification, programming mode and
operator identity to batches, so a Stock-only batch can be distinguished
from a Stock + Pricol batch in the traceability record.

Revision ID: b41c7e2a9d10
Revises: 6897c860425c
Create Date: 2026-10-12 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b41c7e2a9d10'
down_revision: Union[str, Sequence[str], None] = '6897c860425c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("batches") as batch_op:
        batch_op.add_column(
            sa.Column(
                "programming_mode",
                sa.String(length=32),
                nullable=False,
                server_default="STOCK_AND_PRICOL",
            )
        )
        batch_op.add_column(sa.Column("batch_label", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("operator_id", sa.String(length=128), nullable=True))
        batch_op.create_check_constraint(
            "ck_batches_programming_mode",
            "programming_mode IN ('STOCK_ONLY', 'STOCK_AND_PRICOL')",
        )


def downgrade() -> None:
    with op.batch_alter_table("batches") as batch_op:
        batch_op.drop_constraint("ck_batches_programming_mode", type_="check")
        batch_op.drop_column("operator_id")
        batch_op.drop_column("batch_label")
        batch_op.drop_column("programming_mode")
