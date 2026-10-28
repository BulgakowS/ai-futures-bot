"""paper bot lifecycle, immutable configs and decision journal"""

import sqlalchemy as sa

from alembic import op

revision = "6b355247a35e"
down_revision = "c412be197e04"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("portfolio_id", sa.Uuid(), sa.ForeignKey("paper_portfolio.id"), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=True),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("last_window", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("mode = 'paper'", name="ck_bot_paper_only"),
        sa.UniqueConstraint("portfolio_id", name="uq_bot_portfolio_id"),
    )
    op.create_table(
        "bot_config",
        sa.Column("bot_id", sa.Uuid(), sa.ForeignKey("bot.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    op.create_table(
        "bot_decision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bot_id", sa.Uuid(), sa.ForeignKey("bot.id"), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), sa.ForeignKey("paper_portfolio.id"), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candle_event_id", sa.Uuid(), nullable=False),
        sa.Column("candle_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("intent_id", sa.Uuid(), nullable=True),
        sa.Column("risk_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.UniqueConstraint("bot_id", "timeframe", "window_start", name="uq_bot_decision_bot_id"),
    )
    op.create_index("ix_bot_decision_bot_id", "bot_decision", ["bot_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_bot_config BEFORE UPDATE OR DELETE ON bot_config "
            "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
        )
        op.execute("""CREATE FUNCTION bot_decision_reject_final_mutation() RETURNS trigger AS $$
          BEGIN
            IF TG_OP = 'DELETE' OR OLD.status <> 'PROCESSING' THEN
              RAISE EXCEPTION 'immutable final bot decision';
            END IF;
            RETURN NEW;
          END;
          $$ LANGUAGE plpgsql""")
        op.execute(
            "CREATE TRIGGER immutable_final_bot_decision BEFORE UPDATE OR DELETE ON bot_decision "
            "FOR EACH ROW EXECUTE FUNCTION bot_decision_reject_final_mutation()"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION bot_decision_reject_final_mutation() CASCADE")
    op.drop_index("ix_bot_decision_bot_id", table_name="bot_decision")
    op.drop_table("bot_decision")
    op.drop_table("bot_config")
    op.drop_table("bot")
