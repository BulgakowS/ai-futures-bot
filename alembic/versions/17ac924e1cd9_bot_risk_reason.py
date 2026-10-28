"""Keep strategy rationale and paper risk verdict reason separately."""

import sqlalchemy as sa

from alembic import op

revision = "17ac924e1cd9"
down_revision = "6b355247a35e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_decision", sa.Column("risk_reason", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("bot_decision", "risk_reason")
