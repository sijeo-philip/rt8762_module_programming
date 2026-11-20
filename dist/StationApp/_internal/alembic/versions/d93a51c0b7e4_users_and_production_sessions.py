"""users and production sessions

Station users (Argon2-hashed secrets, badge IDs, lockout state), the
Supervisor-started production session with its confirmed configuration,
and the session each batch ran under.

Revision ID: d93a51c0b7e4
Revises: c7d24f8e1a35
Create Date: 2026-10-13 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd93a51c0b7e4'
down_revision: Union[str, Sequence[str], None] = 'c7d24f8e1a35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("badge_id", sa.String(length=128), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('OPERATOR', 'SUPERVISOR')", name="ck_users_role"),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_users_failed_attempts"),
        sa.UniqueConstraint("badge_id", name="uq_users_badge_id"),
    )

    op.create_table(
        "production_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("station_id", sa.String(length=128), nullable=False),
        sa.Column("supervisor_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(length=64), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_production_sessions_status"),
    )
    op.create_index("ix_production_sessions_station_id", "production_sessions", ["station_id"])
    op.create_index("ix_production_sessions_status", "production_sessions", ["status"])
    op.create_index("ix_production_sessions_station_status", "production_sessions", ["station_id", "status"])

    with op.batch_alter_table("batches") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_batches_session_id", ["session_id"])


def downgrade() -> None:
    with op.batch_alter_table("batches") as batch_op:
        batch_op.drop_index("ix_batches_session_id")
        batch_op.drop_column("session_id")

    op.drop_index("ix_production_sessions_station_status", table_name="production_sessions")
    op.drop_index("ix_production_sessions_status", table_name="production_sessions")
    op.drop_index("ix_production_sessions_station_id", table_name="production_sessions")
    op.drop_table("production_sessions")
    op.drop_table("users")
