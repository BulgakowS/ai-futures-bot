"""Independent append-only GKG candidate observations."""

import sqlalchemy as sa

from alembic import op

revision = "f2a6c1d9e8b0"
down_revision = "e5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gkg_scan",
        sa.Column("scan_id", sa.Uuid(), primary_key=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("batch_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("manifest_bytes", sa.Integer(), nullable=False),
        sa.Column("archive_bytes", sa.Integer(), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("status IN ('ok','repeat','error')", name="ck_gkg_scan_status"),
    )
    op.create_table(
        "gkg_slot_observation",
        sa.Column("observation_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_id", sa.Uuid(), nullable=False),
        sa.Column("slot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.UniqueConstraint("scan_id", "slot_at", name="uq_gkg_scan_slot"),
        sa.CheckConstraint(
            "status IN ('ok','unknown','error','repeat')", name="ck_gkg_slot_status"
        ),
    )
    op.create_index("ix_gkg_slot_asof", "gkg_slot_observation", ["slot_at", "observed_at"])
    op.create_index("ix_gkg_slot_observation_scan_id", "gkg_slot_observation", ["scan_id"])
    op.create_table(
        "gkg_release_revision",
        sa.Column("revision_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_id", sa.Uuid(), nullable=False),
        sa.Column("batch_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("btc_rows", sa.Integer(), nullable=False),
        sa.Column("mean_btc_document_tone", sa.String(80)),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_md5", sa.String(32), nullable=False),
        sa.Column("manifest_size", sa.Integer(), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.UniqueConstraint("batch_at", "revision", name="uq_gkg_batch_revision"),
        sa.CheckConstraint(
            "revision > 0 AND total_rows > 0 AND btc_rows >= 0 AND btc_rows <= total_rows",
            name="ck_gkg_counts",
        ),
    )
    op.create_index("ix_gkg_batch_asof", "gkg_release_revision", ["batch_at", "observed_at"])
    op.create_index("ix_gkg_release_revision_scan_id", "gkg_release_revision", ["scan_id"])
    for table in ("gkg_scan", "gkg_slot_observation", "gkg_release_revision"):
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
            )
        elif op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'immutable GKG journal'); END"
                )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for table in ("gkg_scan", "gkg_slot_observation", "gkg_release_revision"):
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER immutable_{table}_{action}")
    op.drop_index("ix_gkg_release_revision_scan_id", table_name="gkg_release_revision")
    op.drop_index("ix_gkg_batch_asof", table_name="gkg_release_revision")
    op.drop_table("gkg_release_revision")
    op.drop_index("ix_gkg_slot_observation_scan_id", table_name="gkg_slot_observation")
    op.drop_index("ix_gkg_slot_asof", table_name="gkg_slot_observation")
    op.drop_table("gkg_slot_observation")
    op.drop_table("gkg_scan")
