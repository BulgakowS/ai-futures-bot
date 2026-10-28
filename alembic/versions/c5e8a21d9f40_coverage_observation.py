"""Append-only announcement coverage observations for event-time replay."""

import sqlalchemy as sa

from alembic import op

revision = "c5e8a21d9f40"
down_revision = "b8c9e4a01722"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "announcement_coverage_observation",
        sa.Column("observation_id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.Column("scan_id", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reported_total_pages", sa.Integer(), nullable=False),
        sa.Column("seen_pages", sa.JSON(), nullable=False),
        sa.Column("page_hashes", sa.JSON(), nullable=False),
        sa.Column("internal_gap_pages", sa.JSON(), nullable=False),
        sa.Column("unseen_page_count", sa.Integer(), nullable=False),
        sa.Column("page_shift_detected", sa.Boolean(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "source", "language", "observed_at", name="uq_announcement_coverage_observed_at"
        ),
        sa.CheckConstraint("reported_total_pages >= 1", name="ck_coverage_observation_pages"),
        sa.CheckConstraint("unseen_page_count >= 0", name="ck_coverage_observation_unseen"),
        sa.CheckConstraint(
            "status IN ('ok', 'partial', 'shifted', 'rate_limited', 'error')",
            name="ck_coverage_observation_status",
        ),
    )
    op.create_index(
        "ix_coverage_observation_asof",
        "announcement_coverage_observation",
        ["source", "language", "observed_at"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_announcement_coverage_observation BEFORE UPDATE OR DELETE "
            "ON announcement_coverage_observation FOR EACH ROW "
            "EXECUTE FUNCTION paper_reject_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_announcement_coverage_observation_{action.lower()} "
                f"BEFORE {action} ON announcement_coverage_observation BEGIN "
                "SELECT RAISE(ABORT, 'immutable announcement coverage observation'); END"
            )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER immutable_announcement_coverage_observation_{action}")
    op.drop_index("ix_coverage_observation_asof", table_name="announcement_coverage_observation")
    op.drop_table("announcement_coverage_observation")
