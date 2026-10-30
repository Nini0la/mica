"""prospective memory and urgency

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    state_constraint = next(
        (
            item["name"]
            for item in sa.inspect(op.get_bind()).get_check_constraints("commitments")
            if "state" in item["sqltext"].lower()
        ),
        None,
    )
    op.add_column("commitments", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("commitments", sa.Column("due_granularity", sa.String(20), nullable=True))
    op.add_column("commitments", sa.Column("due_source_text", sa.Text(), nullable=True))
    op.add_column(
        "commitments", sa.Column("estimated_effort_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "commitments",
        sa.Column("importance", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "commitments", sa.Column("next_relevant_moment", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "commitments", sa.Column("last_surfaced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "commitments", sa.Column("last_touched_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Commitments had no updated_at in the M2 lineage, so created_at is the
    # deterministic best-known activity time for every legacy row.
    op.execute("UPDATE commitments SET last_touched_at = created_at")
    with op.batch_alter_table("commitments") as batch:
        batch.alter_column("last_touched_at", nullable=False)
    op.add_column(
        "commitments",
        sa.Column("ambiguity", sa.String(50), nullable=False, server_default="none"),
    )
    op.add_column(
        "commitments",
        sa.Column("dependencies", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("commitments", sa.Column("available_minutes", sa.Integer(), nullable=True))
    op.add_column("commitments", sa.Column("related_thread_id", sa.String(36), nullable=True))

    op.add_column(
        "commitments", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE commitments SET estimated_effort_minutes = effort_minutes "
        "WHERE effort_minutes IS NOT NULL"
    )
    with op.batch_alter_table("commitments") as batch:
        if state_constraint:
            batch.drop_constraint(state_constraint, type_="check")
        batch.create_foreign_key(
            "fk_commitments_related_thread_id_threads",
            "threads",
            ["related_thread_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "ck_commitments_importance", "importance between 0 and 3"
        )
        batch.create_check_constraint(
            "ck_commitments_estimated_effort",
            "estimated_effort_minutes is null or estimated_effort_minutes >= 0",
        )
        batch.create_check_constraint(
            "ck_commitments_state_m3",
            "state in ('open','in_progress','waiting','complete','completed','cancelled',"
            "'abandoned','dormant')",
        )

    op.create_table(
        "reminders",
        sa.Column("commitment_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=True),
        sa.Column("policy_version", sa.String(30), nullable=False),
        sa.Column("relevance_window", sa.String(80), nullable=False),
        sa.Column("reason_code", sa.String(50), nullable=False),
        sa.Column("reason_metadata", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("surfaced_message_id", sa.String(36), nullable=True),
        sa.Column("source_event_id", sa.String(36), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(50), nullable=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["commitment_id"], ["commitments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"]),
        sa.ForeignKeyConstraint(["surfaced_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["source_event_id"], ["agent_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "commitment_id",
            "relevance_window",
            "policy_version",
            name="uq_reminder_commitment_window_policy",
        ),
        sa.CheckConstraint(
            "state in ('pending','surfaced','suppressed','failed')",
            name="ck_reminders_state",
        ),
        sa.CheckConstraint(
            "attempt_count between 0 and 3", name="ck_reminders_attempt_count"
        ),
    )
    op.create_index("ix_reminders_user_id", "reminders", ["user_id"])
    op.create_index("ix_reminders_commitment_id", "reminders", ["commitment_id"])


def downgrade() -> None:
    op.drop_index("ix_reminders_commitment_id", table_name="reminders")
    op.drop_index("ix_reminders_user_id", table_name="reminders")
    op.drop_table("reminders")
    # M2 accepts only open/complete/abandoned/dormant. Preserve those states and
    # deterministically collapse M3-only states before restoring its constraint:
    # active work and waiting become open, completed becomes complete, and a
    # cancellation becomes abandoned. This is intentionally loss-aware: a later
    # re-upgrade cannot reconstruct the more specific M3 state.
    op.execute(
        "UPDATE commitments SET state = CASE "
        "WHEN state IN ('in_progress', 'waiting') THEN 'open' "
        "WHEN state = 'completed' THEN 'complete' "
        "WHEN state = 'cancelled' THEN 'abandoned' "
        "ELSE state END"
    )
    with op.batch_alter_table("commitments") as batch:
        batch.drop_constraint("ck_commitments_state_m3", type_="check")
        batch.drop_constraint("ck_commitments_estimated_effort", type_="check")
        batch.drop_constraint("ck_commitments_importance", type_="check")
        batch.drop_constraint(
            "fk_commitments_related_thread_id_threads", type_="foreignkey"
        )
        batch.create_check_constraint(
            "ck_commitments_state_m2",
            "state in ('open','complete','abandoned','dormant')",
        )
    for column in (
        "completed_at",
        "related_thread_id",
        "available_minutes",
        "dependencies",
        "ambiguity",
        "last_touched_at",
        "last_surfaced_at",
        "next_relevant_moment",
        "importance",
        "estimated_effort_minutes",
        "due_source_text",
        "due_granularity",
        "due_at",
    ):
        with op.batch_alter_table("commitments") as batch:
            batch.drop_column(column)
