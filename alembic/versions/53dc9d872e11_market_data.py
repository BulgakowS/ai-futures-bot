"""public market cache"""

import sqlalchemy as sa

from alembic import op

revision = "53dc9d872e11"
down_revision = "6ea272857462"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instrument_rules",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("validated", sa.Boolean(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    op.create_table(
        "market_snapshot",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    op.create_table(
        "candle",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("timeframe", sa.String(8), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("final", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )


def downgrade():
    op.drop_table("candle")
    op.drop_table("market_snapshot")
    op.drop_table("instrument_rules")
