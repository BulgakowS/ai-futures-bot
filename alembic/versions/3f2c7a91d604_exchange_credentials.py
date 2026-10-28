"""Encrypted exchange credentials and inert bot selection history."""

import sqlalchemy as sa

from alembic import op

revision = "3f2c7a91d604"
down_revision = "82f0d48aa123"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "exchange_credential",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("key_hint", sa.String(4), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="credential_status"),
        sa.CheckConstraint("version >= 1", name="credential_version"),
    )
    op.create_table(
        "bot_credential_selection",
        sa.Column("bot_id", sa.Uuid(), sa.ForeignKey("bot.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column(
            "credential_id", sa.Uuid(), sa.ForeignKey("exchange_credential.id"), nullable=False
        ),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.CheckConstraint("version >= 1", name="selection_version"),
        sa.CheckConstraint("credential_version >= 1", name="selected_credential_version"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_bot_credential_selection "
            "BEFORE UPDATE OR DELETE ON bot_credential_selection "
            "FOR EACH ROW EXECUTE FUNCTION paper_reject_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_bot_credential_selection_{action.lower()} "
                f"BEFORE {action} ON bot_credential_selection "
                "BEGIN SELECT RAISE(ABORT, 'immutable credential selection'); END"
            )


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER immutable_bot_credential_selection_{action}")
    op.drop_table("bot_credential_selection")
    op.drop_table("exchange_credential")
