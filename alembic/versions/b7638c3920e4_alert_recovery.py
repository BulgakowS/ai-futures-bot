"""Require stable recovery before rearming an incident alert."""

import sqlalchemy as sa

from alembic import op

revision = "b7638c3920e4"
down_revision = "a8195da109f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("alert_state", sa.Column("recovered_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("alert_state", "recovered_at")
