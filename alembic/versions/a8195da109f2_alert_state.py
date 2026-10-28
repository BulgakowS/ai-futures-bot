"""Persist alert transitions across API restarts."""

import sqlalchemy as sa

from alembic import op

revision = "a8195da109f2"
down_revision = "d7d5a8b4c601"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alert_state",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("subject", sa.String(64), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("alert_state")
