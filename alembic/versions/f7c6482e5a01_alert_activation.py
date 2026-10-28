"""Persist pending alert activation for process-safe confirmation."""

import sqlalchemy as sa

from alembic import op

revision = "f7c6482e5a01"
down_revision = "e0e429e4cf31"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("alert_state", sa.Column("pending_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("alert_state", "pending_at")
