"""foundation"""

import sqlalchemy as sa

from alembic import op

revision = "6ea272857462"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.Column("config_version", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode = 'paper'", name=op.f("ck_app_state_paper_only")),
        sa.CheckConstraint("id = 1", name=op.f("ck_app_state_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_state")),
        sa.UniqueConstraint("instance_id", name=op.f("uq_app_state_instance_id")),
    )
    op.create_table(
        "audit_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_audit_event")),
    )
    op.create_index(
        op.f("ix_audit_event_correlation_id"), "audit_event", ["correlation_id"], unique=False
    )
    op.create_table(
        "outbox_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(length=128), nullable=False),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox_event")),
    )
    op.create_index(
        op.f("ix_outbox_event_correlation_id"), "outbox_event", ["correlation_id"], unique=False
    )
    op.create_table(
        "worker_heartbeat",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_worker_heartbeat")),
    )


def downgrade():
    op.drop_table("worker_heartbeat")
    op.drop_index(op.f("ix_outbox_event_correlation_id"), table_name="outbox_event")
    op.drop_table("outbox_event")
    op.drop_index(op.f("ix_audit_event_correlation_id"), table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_table("app_state")
