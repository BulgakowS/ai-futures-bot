"""Append-only Hour4 REST candle versions for GKG as-of research."""

import sqlalchemy as sa

from alembic import op

revision = "d3c7a5e91f02"
down_revision = "f2a6c1d9e8b0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "market_candle_observation",
        sa.Column("observation_id", sa.Uuid(), primary_key=True),
        sa.Column("causal_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("close", sa.String(80), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "symbol", "timeframe", "window_start", "version", name="uq_market_candle_version"
        ),
        sa.CheckConstraint(
            "symbol = 'BTC_USDT' AND timeframe = '4h'", name="ck_market_candle_scope"
        ),
        sa.CheckConstraint("version > 0", name="ck_market_candle_version"),
        sa.CheckConstraint("source = 'rest'", name="ck_market_candle_rest"),
    )
    op.create_index(
        "ix_market_candle_asof",
        "market_candle_observation",
        ["symbol", "timeframe", "window_start", "observed_at"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_market_candle_observation "
            "BEFORE UPDATE OR DELETE ON market_candle_observation "
            "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_market_candle_observation_{action.lower()} "
                f"BEFORE {action} ON market_candle_observation "
                "BEGIN SELECT RAISE(ABORT, 'immutable market candle journal'); END"
            )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER immutable_market_candle_observation_{action}")
    op.drop_index("ix_market_candle_asof", table_name="market_candle_observation")
    op.drop_table("market_candle_observation")
