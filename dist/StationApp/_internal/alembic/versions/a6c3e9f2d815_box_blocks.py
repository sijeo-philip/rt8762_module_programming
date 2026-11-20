"""box blocks

Blocks of MAC addresses issued to this station by the UnisemBLE box:
current, reserve and closed blocks, and the promote owed to the box.

Revision ID: a6c3e9f2d815
Revises: f2b8c6d4e1a7
Create Date: 2026-11-02 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6c3e9f2d815'
down_revision: Union[str, Sequence[str], None] = 'f2b8c6d4e1a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "box_blocks",
        sa.Column("block_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("allocation_id", sa.String(length=128), sa.ForeignKey("allocations.allocation_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("master_range", sa.String(length=128), nullable=False),
        sa.Column("first_mac", sa.String(length=12), nullable=False),
        sa.Column("last_mac", sa.String(length=12), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promote_from_block_id", sa.Integer(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('CURRENT', 'RESERVE', 'CLOSED')", name="ck_box_blocks_state"),
        sa.CheckConstraint("size > 0", name="ck_box_blocks_size"),
    )
    op.create_index("ix_box_blocks_purpose", "box_blocks", ["purpose"])


def downgrade() -> None:
    op.drop_index("ix_box_blocks_purpose", table_name="box_blocks")
    op.drop_table("box_blocks")
