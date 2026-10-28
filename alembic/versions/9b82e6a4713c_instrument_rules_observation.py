"""Append-only public BTC_USDT instrument rules observations."""

import sqlalchemy as sa

from alembic import op

revision = "9b82e6a4713c"
down_revision = "d3c7a5e91f02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instrument_rules_observation",
        sa.Column("observation_id", sa.Uuid(), primary_key=True),
        sa.Column("causal_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("symbol", "version", name="uq_instrument_rules_observation_version"),
        sa.CheckConstraint("symbol = 'BTC_USDT'", name="ck_instrument_rules_observation_scope"),
        sa.CheckConstraint("version > 0", name="ck_instrument_rules_observation_positive_version"),
        sa.CheckConstraint(
            "effective_at = observed_at", name="ck_instrument_rules_observation_local_effective"
        ),
    )
    op.create_index(
        "ix_instrument_rules_observation_asof",
        "instrument_rules_observation",
        ["symbol", "observed_at"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_instrument_rules_observation "
            "BEFORE UPDATE OR DELETE ON instrument_rules_observation "
            "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_instrument_rules_observation_{action.lower()} "
                f"BEFORE {action} ON instrument_rules_observation "
                "BEGIN SELECT RAISE(ABORT, 'immutable instrument rules journal'); END"
            )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER immutable_instrument_rules_observation_{action}")
    op.drop_index("ix_instrument_rules_observation_asof", table_name="instrument_rules_observation")
    op.drop_table("instrument_rules_observation")
