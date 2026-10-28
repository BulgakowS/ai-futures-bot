"""Append-only draft settings for a possible future Live pilot."""

import sqlalchemy as sa

from alembic import op

revision = "82f0d48aa123"
down_revision = "71a5d0e9c324"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "live_pilot_bot_config",
        sa.Column("bot_id", sa.Uuid(), sa.ForeignKey("bot.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_live_pilot_bot_config "
            "BEFORE UPDATE OR DELETE ON live_pilot_bot_config "
            "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_live_pilot_bot_config_{action.lower()} "
                f"BEFORE {action} ON live_pilot_bot_config "
                "BEGIN SELECT RAISE(ABORT, 'immutable pilot config'); END"
            )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER immutable_live_pilot_bot_config_{action}")
    op.drop_table("live_pilot_bot_config")
