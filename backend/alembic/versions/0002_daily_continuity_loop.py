"""daily continuity loop

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def owned_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    ]


def create_owned_table(name: str, *columns, constraints=()) -> None:
    op.create_table(name, *columns, *owned_columns(), *constraints)
    op.create_index(f"ix_{name}_user_id", name, ["user_id"])


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "users", sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC")
    )
    op.add_column(
        "threads", sa.Column("priority", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "commitments", sa.Column("priority", sa.Integer(), nullable=False, server_default="0")
    )

    create_owned_table(
        "daily_briefings",
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("policy_version", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("power_list", sa.JSON(), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("source_event_id", sa.String(length=36), nullable=False),
        constraints=(
            sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
            sa.ForeignKeyConstraint(["source_event_id"], ["agent_events.id"]),
            sa.UniqueConstraint(
                "user_id", "local_date", "policy_version", name="uq_daily_briefing_policy"
            ),
        ),
    )
    create_owned_table(
        "journal_entries",
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("original_content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("structured_state", sa.JSON(), nullable=True),
        sa.Column("source_message_id", sa.String(length=36), nullable=False),
        sa.Column("source_event_id", sa.String(length=36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        constraints=(
            sa.ForeignKeyConstraint(["source_message_id"], ["messages.id"]),
            sa.ForeignKeyConstraint(["source_event_id"], ["agent_events.id"]),
            sa.UniqueConstraint("user_id", "local_date", name="uq_journal_entries_user_date"),
        ),
    )
    create_owned_table(
        "reflection_prompts",
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("policy_version", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("source_event_id", sa.String(length=36), nullable=False),
        sa.Column("journal_entry_id", sa.String(length=36), nullable=True),
        constraints=(
            sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
            sa.ForeignKeyConstraint(["source_event_id"], ["agent_events.id"]),
            sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entries.id"]),
            sa.UniqueConstraint(
                "user_id", "local_date", "policy_version", name="uq_reflection_prompt_policy"
            ),
            sa.CheckConstraint("status in ('awaiting','completed')"),
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_reflection_prompts_user_id", table_name="reflection_prompts")
    op.drop_table("reflection_prompts")
    op.drop_index("ix_journal_entries_user_id", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_index("ix_daily_briefings_user_id", table_name="daily_briefings")
    op.drop_table("daily_briefings")
    with op.batch_alter_table("commitments") as batch:
        batch.drop_column("priority")
    with op.batch_alter_table("threads") as batch:
        batch.drop_column("priority")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("timezone")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("ordinal")
