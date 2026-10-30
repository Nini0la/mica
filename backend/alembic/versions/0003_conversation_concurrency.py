"""single conversation and transactional message ordering

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH canonical AS (
            SELECT
                id AS old_id,
                FIRST_VALUE(id) OVER (
                    PARTITION BY user_id ORDER BY created_at, id
                ) AS canonical_id
            FROM conversation_sessions
        )
        UPDATE messages
        SET conversation_id = (
            SELECT canonical_id
            FROM canonical
            WHERE canonical.old_id = messages.conversation_id
        )
        WHERE conversation_id IN (SELECT old_id FROM canonical)
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY conversation_id ORDER BY ordinal, created_at, id
                ) AS new_ordinal
            FROM messages
        )
        UPDATE messages
        SET ordinal = (
            SELECT new_ordinal FROM ranked WHERE ranked.id = messages.id
        )
        WHERE id IN (SELECT id FROM ranked)
        """
    )
    op.execute(
        """
        DELETE FROM conversation_sessions
        WHERE id NOT IN (
            SELECT canonical_id
            FROM (
                SELECT FIRST_VALUE(id) OVER (
                    PARTITION BY user_id ORDER BY created_at, id
                ) AS canonical_id
                FROM conversation_sessions
            ) AS canonical
        )
        """
    )
    with op.batch_alter_table("conversation_sessions") as batch:
        batch.create_unique_constraint("uq_conversation_sessions_user_id", ["user_id"])
    with op.batch_alter_table("messages") as batch:
        batch.create_unique_constraint(
            "uq_messages_conversation_ordinal", ["conversation_id", "ordinal"]
        )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("uq_messages_conversation_ordinal", type_="unique")
    with op.batch_alter_table("conversation_sessions") as batch:
        batch.drop_constraint("uq_conversation_sessions_user_id", type_="unique")
