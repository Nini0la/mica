"""Private owner Telegram identity, inbox and outbox; no existing rows rewritten."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = depends_on = None


def owned():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def connection_fk():
    return sa.ForeignKeyConstraint(
        ["user_id", "connection_id"], ["telegram_connections.user_id", "telegram_connections.id"]
    )


def upgrade():
    op.create_table(
        "telegram_bots",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("next_update_id", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "telegram_connections",
        *owned(),
        sa.Column("bot_id", sa.String(30), sa.ForeignKey("telegram_bots.id"), nullable=False),
        sa.Column("peer_id", sa.String(30)),
        sa.Column("linked", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("code_digest", sa.String(64)),
        sa.Column("code_expires_at", sa.DateTime(timezone=True)),
        sa.Column("allow_memory_write", sa.Boolean(), nullable=False),
        sa.Column("proactive_enabled", sa.Boolean(), nullable=False),
        sa.Column("proactive_since", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_telegram_owner"),
        sa.UniqueConstraint("bot_id", "peer_id", name="uq_telegram_peer"),
        sa.UniqueConstraint("user_id", "id", name="uq_telegram_connection_owner"),
    )
    op.create_table(
        "telegram_inputs",
        *owned(),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("allow_memory_write", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.UniqueConstraint("connection_id", "update_id", name="uq_telegram_update"),
        connection_fk(),
        sa.CheckConstraint("state in ('pending','processing','succeeded','failed','skipped')"),
        sa.CheckConstraint("attempts between 0 and 3"),
    )
    op.create_table(
        "telegram_deliveries",
        *owned(),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("peer_id", sa.String(30), nullable=False),
        sa.Column("dedupe_key", sa.String(180), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_id", sa.String(36)),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("scheduled_actions.id")),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("provider_message_id", sa.String(30)),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_telegram_delivery"),
        connection_fk(),
        sa.ForeignKeyConstraint(["user_id", "message_id"], ["messages.user_id", "messages.id"]),
        sa.CheckConstraint(
            "state in ('pending','sending','sent','retryable','uncertain','cancelled','failed')"
        ),
        sa.CheckConstraint("attempts between 0 and 3"),
    )
    for table in ("telegram_connections", "telegram_inputs", "telegram_deliveries"):
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    op.create_index("ix_telegram_delivery_pending", "telegram_deliveries", ["state", "retry_at"])


def downgrade():
    for table in (
        "telegram_deliveries",
        "telegram_inputs",
        "telegram_connections",
        "telegram_bots",
    ):
        op.drop_table(table)
