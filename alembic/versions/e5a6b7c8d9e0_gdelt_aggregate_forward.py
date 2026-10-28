"""GDELT aggregate forward journal, separate from MEXC announcement contracts."""

import sqlalchemy as sa

from alembic import op

revision = "e5a6b7c8d9e0"
down_revision = "c5e8a21d9f40"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gdelt_volume_revision",
        sa.Column("revision_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_id", sa.Uuid(), nullable=False),
        sa.Column("point_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("value", sa.String(32), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.UniqueConstraint("point_at", "revision", name="uq_gdelt_point_revision"),
        sa.CheckConstraint("revision > 0", name="ck_gdelt_point_revision_positive"),
    )
    op.create_index("ix_gdelt_point_asof", "gdelt_volume_revision", ["point_at", "observed_at"])
    op.create_index("ix_gdelt_volume_revision_scan_id", "gdelt_volume_revision", ["scan_id"])
    op.create_table(
        "gdelt_volume_scan",
        sa.Column("scan_id", sa.Uuid(), primary_key=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column("nonzero_count", sa.Integer(), nullable=False),
        sa.Column("missing_bucket_count", sa.Integer(), nullable=False),
        sa.Column("revised_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("status IN ('ok', 'gap', 'error')", name="ck_gdelt_scan_status"),
    )
    for table in ("gdelt_volume_revision", "gdelt_volume_scan"):
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
            )
        elif op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'immutable GDELT journal'); END"
                )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for table in ("gdelt_volume_revision", "gdelt_volume_scan"):
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER immutable_{table}_{action}")
    op.drop_table("gdelt_volume_scan")
    op.drop_index("ix_gdelt_volume_revision_scan_id", table_name="gdelt_volume_revision")
    op.drop_index("ix_gdelt_point_asof", table_name="gdelt_volume_revision")
    op.drop_table("gdelt_volume_revision")
