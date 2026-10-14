"""station settings

Supervisor-controlled production settings (programming mode, firmware
paths, enabled slots, lot), stored as JSON and audited on change.

Revision ID: f2b8c6d4e1a7
Revises: e5f17a2c9b03
Create Date: 2026-10-14 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b8c6d4e1a7'
down_revision: Union[str, Sequence[str], None] = 'e5f17a2c9b03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "station_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("station_settings")
