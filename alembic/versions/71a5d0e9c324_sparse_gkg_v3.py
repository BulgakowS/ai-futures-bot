"""Separate append-only sparse GKG v3 budget and results."""

import sqlalchemy as sa

from alembic import op

revision = "71a5d0e9c324"
down_revision = "9b82e6a4713c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sparse_gkg_budget",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cap_bytes", sa.BigInteger(), nullable=False),
        sa.Column("prior_bytes", sa.Integer(), nullable=False),
        sa.Column("budget_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_sparse_gkg_budget_singleton"),
    )
    op.create_table(
        "sparse_gkg_attempt",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_batch_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_bytes", sa.Integer(), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.UniqueConstraint("anchor_at", name="uq_sparse_gkg_anchor"),
        sa.CheckConstraint("reserved_bytes > 0", name="ck_sparse_gkg_positive_reservation"),
    )
    op.create_table(
        "sparse_gkg_result",
        sa.Column("result_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("response_bytes", sa.Integer(), nullable=False),
        sa.Column("archive_sha256", sa.String(64)),
        sa.Column("total_rows", sa.Integer()),
        sa.Column("btc_rows", sa.Integer()),
        sa.Column("mean_btc_document_tone", sa.String(80)),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("status IN ('ok','error')", name="ck_sparse_gkg_status"),
        sa.CheckConstraint("total_rows IS NULL OR total_rows > 0", name="ck_sparse_gkg_rows"),
    )
    for table in ("sparse_gkg_budget", "sparse_gkg_attempt", "sparse_gkg_result"):
        dialect = op.get_bind().dialect.name
        if dialect == "postgresql":
            op.execute(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
            )
        elif dialect == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'immutable sparse GKG journal'); END"
                )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for table in ("sparse_gkg_budget", "sparse_gkg_attempt", "sparse_gkg_result"):
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER immutable_{table}_{action}")
    op.drop_table("sparse_gkg_result")
    op.drop_table("sparse_gkg_attempt")
    op.drop_table("sparse_gkg_budget")
