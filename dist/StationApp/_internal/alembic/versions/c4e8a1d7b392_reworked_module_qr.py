"""reworked module QR

A re-worked module keeps its QR and gets a new MAC in a later batch, so
the same QR may now appear on more than one batch slot. The QR registry
still holds one row per QR, pointing at its latest binding.

Revision ID: c4e8a1d7b392
Revises: a6c3e9f2d815
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4e8a1d7b392'
down_revision: Union[str, Sequence[str], None] = 'a6c3e9f2d815'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The initial schema created UNIQUE (module_qr) without a name; the
# convention names it so the batch (table copy) operation can drop it.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    with op.batch_alter_table('batch_slots', recreate='always', naming_convention=NAMING) as batch_op:
        batch_op.drop_constraint('uq_batch_slots_module_qr', type_='unique')
        batch_op.create_index('ix_batch_slots_module_qr', ['module_qr'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('batch_slots', recreate='always') as batch_op:
        batch_op.drop_index('ix_batch_slots_module_qr')
        batch_op.create_unique_constraint('uq_batch_slots_module_qr', ['module_qr'])
