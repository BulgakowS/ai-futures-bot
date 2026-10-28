"""Append-only N4a synthetic forecast and candidate proposal journal."""

import sqlalchemy as sa

from alembic import op

revision = "b8c9e4a01722"
down_revision = "a4e7d9c20b61"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "news_forecast",
        sa.Column("forecast_id", sa.Uuid(), primary_key=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("anchor", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("probabilities", sa.JSON(), nullable=True),
        sa.Column("model_version", sa.String(128), nullable=True),
        sa.Column("artifact_checksum", sa.String(64), nullable=True),
        sa.Column("dataset_checksum", sa.String(64), nullable=True),
        sa.Column("config_checksum", sa.String(64), nullable=False),
        sa.Column("source_versions", sa.JSON(), nullable=False),
        sa.Column("market_event_ids", sa.JSON(), nullable=False),
        sa.Column("news_version_ids", sa.JSON(), nullable=False),
        sa.Column("feature_hash", sa.String(64), nullable=True),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("approval_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "anchor", name="uq_news_forecast_window"),
        sa.CheckConstraint("horizon_hours = 4", name="ck_news_forecast_horizon_4h"),
        sa.CheckConstraint("status IN ('wait', 'candidate')", name="ck_news_forecast_status"),
        sa.CheckConstraint(
            "direction IN ('UP', 'DOWN', 'WAIT')", name="ck_news_forecast_direction"
        ),
    )
    op.create_table(
        "news_forecast_proposal",
        sa.Column("proposal_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "forecast_id",
            sa.Uuid(),
            sa.ForeignKey("news_forecast.forecast_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("entry", sa.String(96), nullable=False),
        sa.Column("stop_loss", sa.String(96), nullable=False),
        sa.Column("take_profit", sa.String(96), nullable=False),
        sa.Column("rules_version", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('LONG', 'SHORT')", name="ck_news_forecast_proposal_direction"
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for table in ("news_forecast", "news_forecast_proposal"):
            op.execute(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
            )
    elif dialect == "sqlite":
        for table in ("news_forecast", "news_forecast_proposal"):
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON "
                    f"{table} BEGIN SELECT RAISE(ABORT, 'immutable forecast journal'); END"
                )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for table in ("news_forecast_proposal", "news_forecast"):
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER immutable_{table}_{action}")
    op.drop_table("news_forecast_proposal")
    op.drop_table("news_forecast")
