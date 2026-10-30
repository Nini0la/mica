"""tenant-safe reminders and fair scan cursor

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_TARGET_UNIQUES = (
    ("commitments", "uq_commitments_user_id_id"),
    ("threads", "uq_threads_user_id_id"),
    ("messages", "uq_messages_user_id_id"),
    ("agent_events", "uq_agent_events_user_id_id"),
)

_REMINDER_FOREIGN_KEYS = (
    (
        "fk_reminders_user_commitment",
        "commitments",
        ["user_id", "commitment_id"],
        ["user_id", "id"],
        "CASCADE",
    ),
    (
        "fk_reminders_user_thread",
        "threads",
        ["user_id", "thread_id"],
        ["user_id", "id"],
        None,
    ),
    (
        "fk_reminders_user_surfaced_message",
        "messages",
        ["user_id", "surfaced_message_id"],
        ["user_id", "id"],
        None,
    ),
    (
        "fk_reminders_user_source_event",
        "agent_events",
        ["user_id", "source_event_id"],
        ["user_id", "id"],
        None,
    ),
)


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("reminder_scan_cursor_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("reminder_scan_cursor_id", sa.String(36), nullable=True),
    )
    for table_name, constraint_name in _TARGET_UNIQUES:
        with op.batch_alter_table(table_name) as batch:
            batch.create_unique_constraint(constraint_name, ["user_id", "id"])
    with op.batch_alter_table("reminders") as batch:
        for name, target, local_columns, remote_columns, ondelete in _REMINDER_FOREIGN_KEYS:
            batch.create_foreign_key(
                name,
                target,
                local_columns,
                remote_columns,
                ondelete=ondelete,
            )


def downgrade() -> None:
    with op.batch_alter_table("reminders") as batch:
        for name, _target, _local_columns, _remote_columns, _ondelete in reversed(
            _REMINDER_FOREIGN_KEYS
        ):
            batch.drop_constraint(name, type_="foreignkey")
    for table_name, constraint_name in reversed(_TARGET_UNIQUES):
        with op.batch_alter_table(table_name) as batch:
            batch.drop_constraint(constraint_name, type_="unique")
    op.drop_column("conversation_sessions", "reminder_scan_cursor_id")
    op.drop_column("conversation_sessions", "reminder_scan_cursor_created_at")
