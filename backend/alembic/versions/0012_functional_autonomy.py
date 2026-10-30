"""Owner-enabled personal autonomy, current plans and durable action evidence.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

USER_FIELDS = [
    sa.Column("checkins_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("morning_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("evening_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("quiet_start", sa.Integer(), nullable=False, server_default="21"),
    sa.Column("quiet_end", sa.Integer(), nullable=False, server_default="8"),
    sa.Column("autonomy_version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("autonomy_updated_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("autonomy_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
]
COMMITMENT_FIELDS = [
    sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
    sa.Column("completion_criteria", sa.Text(), nullable=False, server_default=""),
    sa.Column("next_action", sa.Text(), nullable=False, server_default=""),
    sa.Column("constraints", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("availability", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("due_bound", sa.String(20), nullable=False, server_default="unspecified"),
    sa.Column("followup_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("followup_minutes", sa.Integer(), nullable=False, server_default="1440"),
    sa.Column("max_followups", sa.Integer(), nullable=False, server_default="3"),
    sa.Column("followup_count", sa.Integer(), nullable=False, server_default="0"),
]


def upgrade() -> None:
    for column in USER_FIELDS:
        op.add_column("users", column)
    with op.batch_alter_table("commitments") as batch:
        for column in COMMITMENT_FIELDS:
            batch.add_column(column)
        batch.drop_constraint("ck_commitments_state_m3", type_="check")
        batch.create_check_constraint(
            "ck_commitments_state_m3",
            "state in ('open','in_progress','waiting','complete','completed','cancelled','abandoned','dormant','paused')",
        )
        batch.create_check_constraint(
            "ck_commitment_bound", "due_bound in ('unspecified','inclusive','exclusive')"
        )
        batch.create_check_constraint(
            "ck_commitment_followup_policy",
            "followup_minutes between 15 and 10080 and max_followups between 0 and 10",
        )
    op.create_table(
        "commitment_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("commitment_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("user_id", "commitment_id", "version", name="uq_commitment_revision"),
        sa.ForeignKeyConstraint(
            ["user_id", "commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_commitment_revision_owner",
        ),
    )
    op.create_index("ix_commitment_revisions_user_id", "commitment_revisions", ["user_id"])
    op.create_table(
        "scheduled_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("commitment_id", sa.String(36), nullable=True),
        sa.Column("subject_version", sa.Integer(), nullable=False),
        sa.Column("preference_version", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(80), nullable=False, server_default="due"),
        sa.Column("decision", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_event_id", sa.String(36), nullable=True),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "action_key", name="uq_scheduled_action_key"),
        sa.CheckConstraint(
            "state in ('pending','executing','succeeded','retryable','failed','skipped','cancelled')",
            name="ck_scheduled_action_state",
        ),
        sa.CheckConstraint("attempt_count between 0 and 3", name="ck_scheduled_action_attempts"),
        sa.ForeignKeyConstraint(
            ["user_id", "commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_scheduled_commitment",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "source_event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_scheduled_event",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_scheduled_message",
        ),
    )
    op.create_index("ix_scheduled_actions_user_id", "scheduled_actions", ["user_id"])
    op.create_index("ix_scheduled_actions_due_at", "scheduled_actions", ["due_at"])
    op.create_index("ix_scheduled_due_state", "scheduled_actions", ["state", "due_at"])


def downgrade() -> None:
    op.drop_table("scheduled_actions")
    op.drop_table("commitment_revisions")
    op.execute(sa.text("UPDATE commitments SET state='waiting' WHERE state='paused'"))
    with op.batch_alter_table("commitments") as batch:
        batch.drop_constraint("ck_commitment_bound", type_="check")
        batch.drop_constraint("ck_commitment_followup_policy", type_="check")
        batch.drop_constraint("ck_commitments_state_m3", type_="check")
        batch.create_check_constraint(
            "ck_commitments_state_m3",
            "state in ('open','in_progress','waiting','complete','completed','cancelled','abandoned','dormant')",
        )
        for column in reversed(COMMITMENT_FIELDS):
            batch.drop_column(column.name)
    for column in reversed(USER_FIELDS):
        op.drop_column("users", column.name)
