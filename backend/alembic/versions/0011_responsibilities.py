"""Channel-neutral responsibility workflow and durable communication outbox.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def owned():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    ]


def upgrade() -> None:
    op.create_table(
        "channel_accounts",
        *owned(),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("address", sa.String(100), nullable=False),
        sa.Column("assistant_name", sa.String(60), nullable=False),
        sa.Column("on_behalf_of", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("quiet_start", sa.Integer(), nullable=False, server_default="21"),
        sa.Column("quiet_end", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("blocked_addresses", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("worker_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "kind", name="uq_channel_user_kind"),
        sa.UniqueConstraint("kind", "address", name="uq_channel_kind_address"),
        sa.UniqueConstraint("user_id", "id", name="uq_channel_user_id"),
        sa.CheckConstraint("quiet_start between 0 and 23 and quiet_end between 0 and 23"),
    )
    op.create_table(
        "responsibilities",
        *owned(),
        sa.Column("commitment_id", sa.String(36), nullable=False),
        sa.Column("channel_id", sa.String(36), nullable=False),
        sa.Column("recipient_name", sa.String(100), nullable=False),
        sa.Column("recipient_address", sa.String(100), nullable=False),
        sa.Column("expected_evidence", sa.Text(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("approved_scope", sa.JSON(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("followup_minutes", sa.Integer(), nullable=False, server_default="1440"),
        sa.Column("max_followups", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("followup_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agreement_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_complete_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_report", sa.Text(), nullable=False, server_default=""),
        sa.Column("attention_reason", sa.String(60), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("user_id", "id", name="uq_responsibility_user_id"),
        sa.UniqueConstraint("user_id", "commitment_id", name="uq_responsibility_commitment"),
        sa.ForeignKeyConstraint(
            ["user_id", "commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_resp_commitment",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "channel_id"],
            ["channel_accounts.user_id", "channel_accounts.id"],
            name="fk_resp_channel",
        ),
        sa.CheckConstraint(
            "state in ('draft','active','completion_reported','verified','escalated','paused','cancelled')",
            name="ck_responsibility_state",
        ),
        sa.CheckConstraint(
            "followup_minutes between 60 and 10080 and max_followups between 0 and 5"
        ),
        sa.CheckConstraint("followup_count >= 0"),
    )
    op.create_index("ix_responsibility_due", "responsibilities", ["state", "next_action_at"])
    op.create_table(
        "channel_messages",
        *owned(),
        sa.Column("channel_id", sa.String(36), nullable=False),
        sa.Column("responsibility_id", sa.String(36), nullable=True),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("address", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("dedupe_key", sa.String(160), nullable=False),
        sa.Column("provider_id", sa.String(160), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interpretation", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_error", sa.String(60), nullable=True),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_channel_message_dedupe"),
        sa.UniqueConstraint("user_id", "id", name="uq_channel_message_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id", "channel_id"],
            ["channel_accounts.user_id", "channel_accounts.id"],
            name="fk_message_channel",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "responsibility_id"],
            ["responsibilities.user_id", "responsibilities.id"],
            name="fk_message_resp",
        ),
        sa.CheckConstraint("direction in ('inbound','outbound')"),
        sa.CheckConstraint(
            "state in ('queued','accepted','delivered','read','failed','uncertain','received','cancelled')",
            name="ck_channel_message_state",
        ),
    )
    op.create_index(
        "ix_channel_message_dispatch", "channel_messages", ["direction", "state", "next_attempt_at"]
    )
    op.create_table(
        "responsibility_events",
        *owned(),
        sa.Column("responsibility_id", sa.String(36), nullable=False),
        sa.Column("agent_event_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(
            ["user_id", "responsibility_id"],
            ["responsibilities.user_id", "responsibilities.id"],
            name="fk_resp_event_resp",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "agent_event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_resp_event_agent",
        ),
    )
    for table in (
        "channel_accounts",
        "responsibilities",
        "channel_messages",
        "responsibility_events",
    ):
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])


def downgrade() -> None:
    for table in (
        "responsibility_events",
        "channel_messages",
        "responsibilities",
        "channel_accounts",
    ):
        op.drop_table(table)
