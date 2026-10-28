"""Optional paper AI analyses and conservative usage reservations."""

import sqlalchemy as sa

from alembic import op

revision = "89a1e19e9d07"
down_revision = "17ac924e1cd9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_analysis",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("portfolio_id", sa.Uuid(), sa.ForeignKey("paper_portfolio.id"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("context_id", sa.String(64)),
        sa.Column("context", sa.JSON()),
        sa.Column("proposal", sa.JSON()),
        sa.Column("paper_request", sa.JSON()),
        sa.Column("position_id", sa.Uuid()),
        sa.Column("intent_id", sa.Uuid()),
        sa.Column("risk_id", sa.Uuid()),
        sa.Column("order_id", sa.Uuid()),
        sa.Column("risk_reason", sa.String(64)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    op.create_index("ix_ai_analysis_portfolio_id", "ai_analysis", ["portfolio_id"])
    op.create_table(
        "ai_usage",
        sa.Column("analysis_id", sa.Uuid(), sa.ForeignKey("ai_analysis.id"), primary_key=True),
        sa.Column("portfolio_id", sa.Uuid(), sa.ForeignKey("paper_portfolio.id"), nullable=False),
        sa.Column("attempted", sa.Boolean(), nullable=False),
        sa.Column("reserved_usd", sa.String(96), nullable=False),
        sa.Column("estimated_usd", sa.String(96)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    op.create_index("ix_ai_usage_portfolio_id", "ai_usage", ["portfolio_id"])


def downgrade():
    op.drop_index("ix_ai_usage_portfolio_id", table_name="ai_usage")
    op.drop_table("ai_usage")
    op.drop_index("ix_ai_analysis_portfolio_id", table_name="ai_analysis")
    op.drop_table("ai_analysis")
