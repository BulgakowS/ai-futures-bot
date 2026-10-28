"""Durable public funding settlement journal and verified coverage."""

import sqlalchemy as sa

from alembic import op

revision = "e0e429e4cf31"
down_revision = "b7638c3920e4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "funding_settlement",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("settle_time", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("rate", sa.String(96), nullable=False),
        sa.Column("collect_cycle", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    op.create_table(
        "funding_coverage",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("covered_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("covered_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("gap_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_funding_settlement BEFORE UPDATE OR DELETE "
            "ON funding_settlement FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
        )


def downgrade():
    op.drop_table("funding_coverage")
    op.drop_table("funding_settlement")
