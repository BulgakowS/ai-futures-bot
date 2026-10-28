"""Offline versioned announcement journal and mutable scan coverage."""

import sqlalchemy as sa

from alembic import op

revision = "a4e7d9c20b61"
down_revision = "f7c6482e5a01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "announcement_version",
        sa.Column("version_id", sa.Uuid(), primary_key=True),
        sa.Column("canonical_id", sa.String(64), nullable=False),
        sa.Column("canonical_url", sa.String(1024), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("post_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("observation_kind", sa.String(16), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.UniqueConstraint(
            "canonical_id", "version_number", name="uq_announcement_version_sequence"
        ),
        sa.CheckConstraint("version_number >= 1", name="ck_announcement_version_positive_sequence"),
        sa.CheckConstraint(
            "observation_kind IN ('forward', 'backfill')",
            name="ck_announcement_version_observation_kind",
        ),
    )
    op.create_index(
        "ix_announcement_version_canonical_id", "announcement_version", ["canonical_id"]
    )
    op.create_table(
        "announcement_coverage",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("language", sa.String(16), primary_key=True),
        sa.Column("scan_id", sa.String(64), nullable=False),
        sa.Column("reported_total_pages", sa.Integer(), nullable=False),
        sa.Column("seen_pages", sa.JSON(), nullable=False),
        sa.Column("page_hashes", sa.JSON(), nullable=False),
        sa.Column("internal_gap_pages", sa.JSON(), nullable=False),
        sa.Column("unseen_page_count", sa.Integer(), nullable=False),
        sa.Column("page_shift_detected", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.Integer(), nullable=True),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("reported_total_pages >= 1", name="ck_announcement_coverage_pages"),
        sa.CheckConstraint("unseen_page_count >= 0", name="ck_announcement_coverage_unseen"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_announcement_version BEFORE UPDATE OR DELETE "
            "ON announcement_version FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_announcement_version_{action.lower()} "
                f"BEFORE {action} ON announcement_version BEGIN "
                "SELECT RAISE(ABORT, 'immutable announcement version'); END"
            )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER immutable_announcement_version_{action}")
    op.drop_table("announcement_coverage")
    op.drop_index("ix_announcement_version_canonical_id", table_name="announcement_version")
    op.drop_table("announcement_version")
