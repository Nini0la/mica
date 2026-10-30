"""Durable conversational proposals, distinct reminder plans and input timestamps."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = depends_on = None


def owned():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def source():
    return sa.ForeignKeyConstraint(
        ["user_id", "source_message_id"], ["messages.user_id", "messages.id"]
    )


def upgrade():
    for table in ("reflection_prompts", "vocabulary_attempts"):
        op.add_column(
            table,
            sa.Column("workflow_state", sa.String(20), nullable=False, server_default="active"),
        )
        op.add_column(table, sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.execute("UPDATE reflection_prompts SET workflow_state='completed' WHERE status='completed'")
    op.execute(
        "UPDATE vocabulary_attempts SET workflow_state='completed' WHERE user_answer IS NOT NULL AND correct IS NOT NULL"
    )
    op.add_column("telegram_inputs", sa.Column("message_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE telegram_inputs SET message_at=created_at")
    with op.batch_alter_table("commitments") as batch:
        batch.drop_constraint("ck_commitment_followup_policy", type_="check")
        batch.create_check_constraint(
            "ck_commitment_followup_policy",
            "followup_minutes between 1 and 10080 and max_followups between 0 and 10",
        )
    op.create_table(
        "conversation_proposals",
        *owned(),
        sa.Column("tool_name", sa.String(60), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.String(36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.JSON()),
        source(),
        sa.CheckConstraint(
            "state in ('proposed','needs_clarification','paused','executed','cancelled')"
        ),
    )
    op.create_table(
        "reminder_plans",
        *owned(),
        sa.Column("topic", sa.String(500), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True)),
        sa.Column("interval_seconds", sa.Integer()),
        sa.Column("max_occurrences", sa.Integer(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("respect_quiet_hours", sa.Boolean(), nullable=False),
        sa.Column("availability", sa.JSON(), nullable=False),
        sa.Column("source_message_id", sa.String(36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        source(),
        sa.UniqueConstraint("user_id", "id", name="uq_reminder_plan_owner"),
        sa.CheckConstraint("mode in ('once','recurring')"),
        sa.CheckConstraint("state in ('active','paused','completed','cancelled')"),
        sa.CheckConstraint("max_occurrences between 1 and 100 and occurrence_count >= 0"),
    )
    for table in ("conversation_proposals", "reminder_plans"):
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    with op.batch_alter_table("scheduled_actions") as batch:
        batch.add_column(sa.Column("reminder_plan_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_action_reminder_plan",
            "reminder_plans",
            ["user_id", "reminder_plan_id"],
            ["user_id", "id"],
        )


def downgrade():
    # New plan actions cannot run meaningfully in the old application; preserve old action rows.
    op.execute(
        "DELETE FROM telegram_deliveries WHERE action_id IN (SELECT id FROM scheduled_actions WHERE reminder_plan_id IS NOT NULL)"
    )
    op.execute("DELETE FROM scheduled_actions WHERE reminder_plan_id IS NOT NULL")
    with op.batch_alter_table("scheduled_actions") as batch:
        batch.drop_constraint("fk_action_reminder_plan", type_="foreignkey")
        batch.drop_column("reminder_plan_id")
    op.drop_table("reminder_plans")
    op.drop_table("conversation_proposals")
    op.drop_column("telegram_inputs", "message_at")
    for table in ("reflection_prompts", "vocabulary_attempts"):
        op.drop_column(table, "workflow_state")
        op.drop_column(table, "version")
    op.execute("UPDATE commitments SET followup_minutes=15 WHERE followup_minutes < 15")
    with op.batch_alter_table("commitments") as batch:
        batch.drop_constraint("ck_commitment_followup_policy", type_="check")
        batch.create_check_constraint(
            "ck_commitment_followup_policy",
            "followup_minutes between 15 and 10080 and max_followups between 0 and 10",
        )
