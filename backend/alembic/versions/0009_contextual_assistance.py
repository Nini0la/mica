"""contextual assistance, thread revisions, and evidence-backed growth

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("context_reset_at", sa.DateTime(timezone=True), nullable=True))
    # Old transcripts may contain memories erased before this cutoff field existed.
    op.execute(sa.text("UPDATE users SET context_reset_at = CURRENT_TIMESTAMP"))
    op.add_column(
        "users", sa.Column("pet_name", sa.String(60), nullable=False, server_default="Mica")
    )
    op.add_column(
        "users", sa.Column("temperament", sa.String(20), nullable=False, server_default="calm")
    )
    with op.batch_alter_table("threads") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(20), nullable=False, server_default="thread")
        )
        batch.add_column(sa.Column("why", sa.Text(), nullable=False, server_default=""))
        # Legacy explicit records are user-stated facts, not externally verified claims.
        batch.add_column(
            sa.Column("epistemic_status", sa.String(20), nullable=False, server_default="fact")
        )
        batch.add_column(sa.Column("parent_thread_id", sa.String(36), nullable=True))
        batch.create_check_constraint("ck_threads_kind", "kind in ('thread','project','idea')")
        batch.create_check_constraint(
            "ck_threads_epistemic_status", "epistemic_status in ('fact','inference','hypothesis')"
        )
        batch.create_foreign_key(
            "fk_threads_user_parent_thread",
            "threads",
            ["user_id", "parent_thread_id"],
            ["user_id", "id"],
        )

    op.create_table(
        "thread_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "thread_id"],
            ["threads.user_id", "threads.id"],
            name="fk_thread_revisions_user_thread",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id", "thread_id", "version", name="uq_thread_revisions_user_thread_version"
        ),
    )
    op.create_index("ix_thread_revisions_user_id", "thread_revisions", ["user_id"])
    op.create_table(
        "assistance_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("related_thread_id", sa.String(36), nullable=True),
        sa.Column("related_commitment_id", sa.String(36), nullable=True),
        sa.Column("context_manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("intervention_type", sa.String(40), nullable=False),
        sa.Column("execution_status", sa.String(30), nullable=False, server_default="attempted"),
        sa.Column("observed_outcome", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("usage", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("tool_results", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_assistance_runs_user_event",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_assistance_runs_user_message",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "related_thread_id"],
            ["threads.user_id", "threads.id"],
            name="fk_assistance_runs_user_thread",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "related_commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_assistance_runs_user_commitment",
        ),
        sa.UniqueConstraint("user_id", "event_id", name="uq_assistance_runs_user_event"),
        sa.UniqueConstraint("user_id", "id", name="uq_assistance_runs_user_id_id"),
    )
    op.create_index("ix_assistance_runs_user_id", "assistance_runs", ["user_id"])
    op.create_table(
        "assistance_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("verdict", sa.String(30), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["assistance_runs.user_id", "assistance_runs.id"],
            name="fk_assistance_feedback_user_run",
        ),
        sa.UniqueConstraint("user_id", "external_id", name="uq_assistance_feedback_user_external"),
        sa.UniqueConstraint("user_id", "id", name="uq_assistance_feedback_user_id_id"),
        sa.CheckConstraint(
            "verdict in ('helpful','unhelpful','acknowledged','completed')",
            name="ck_assistance_feedback_verdict",
        ),
    )
    op.create_index("ix_assistance_feedback_user_id", "assistance_feedback", ["user_id"])
    op.create_table(
        "growth_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dimension", sa.String(30), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("assessment", sa.String(30), nullable=False, server_default="milestone"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "evidence_id"],
            ["assistance_feedback.user_id", "assistance_feedback.id"],
            name="fk_growth_events_user_evidence",
        ),
        sa.UniqueConstraint(
            "user_id", "evidence_id", "event_type", name="uq_growth_events_user_evidence_type"
        ),
        sa.CheckConstraint(
            "dimension in ('understanding','judgement','capacity','relationship',"
            "'human_empowerment')",
            name="ck_growth_events_dimension",
        ),
        sa.CheckConstraint(
            "assessment in ('milestone','demonstrated_improvement')",
            name="ck_growth_events_assessment",
        ),
    )
    op.create_index("ix_growth_events_user_id", "growth_events", ["user_id"])


def downgrade() -> None:
    op.drop_column("users", "context_reset_at")
    op.drop_index("ix_growth_events_user_id", table_name="growth_events")
    op.drop_table("growth_events")
    op.drop_index("ix_assistance_feedback_user_id", table_name="assistance_feedback")
    op.drop_table("assistance_feedback")
    op.drop_index("ix_assistance_runs_user_id", table_name="assistance_runs")
    op.drop_table("assistance_runs")
    op.drop_index("ix_thread_revisions_user_id", table_name="thread_revisions")
    op.drop_table("thread_revisions")
    with op.batch_alter_table("threads") as batch:
        batch.drop_constraint("fk_threads_user_parent_thread", type_="foreignkey")
        batch.drop_constraint("ck_threads_epistemic_status", type_="check")
        batch.drop_constraint("ck_threads_kind", type_="check")
        batch.drop_column("parent_thread_id")
        batch.drop_column("epistemic_status")
        batch.drop_column("why")
        batch.drop_column("kind")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("temperament")
        batch.drop_column("pet_name")
