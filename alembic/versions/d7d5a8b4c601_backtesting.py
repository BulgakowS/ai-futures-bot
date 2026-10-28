"""Isolated immutable backtest input and reports."""

import sqlalchemy as sa

from alembic import op

revision = "d7d5a8b4c601"
down_revision = "89a1e19e9d07"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "backtest_dataset",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("checksum", sa.String(64), nullable=False, unique=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    op.create_table(
        "backtest_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("backtest_dataset.id"), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("report_checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("mode = 'backtest'", name="ck_backtest_run_backtest_only"),
    )
    op.create_index("ix_backtest_run_dataset_id", "backtest_run", ["dataset_id"])


def downgrade():
    op.drop_index("ix_backtest_run_dataset_id", table_name="backtest_run")
    op.drop_table("backtest_run")
    op.drop_table("backtest_dataset")
