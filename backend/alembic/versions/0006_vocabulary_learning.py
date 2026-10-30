"""longitudinal vocabulary learning

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vocabulary_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("normalized_word", sa.String(100), nullable=False),
        sa.Column("display_word", sa.String(100), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("natural_example", sa.Text(), nullable=False),
        sa.Column("introduced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("introduced_local_date", sa.Date(), nullable=False),
        sa.Column("recall_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correct_recalls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("user_sentences", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("exposure_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mastery_state", sa.String(20), nullable=False, server_default="new"),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_version", sa.String(30), nullable=False),
        sa.Column("policy_version", sa.String(30), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "normalized_word", name="uq_vocabulary_user_word"),
        sa.UniqueConstraint("user_id", "id", name="uq_vocabulary_user_id"),
        sa.CheckConstraint(
            "mastery_state in ('new','developing','familiar','mastered')",
            name="ck_vocabulary_mastery_state",
        ),
        sa.CheckConstraint(
            "recall_attempts >= 0 and correct_recalls >= 0", name="ck_vocabulary_recall_counts"
        ),
        sa.CheckConstraint("exposure_count >= 1", name="ck_vocabulary_exposure_count"),
    )
    op.create_index("ix_vocabulary_items_user_id", "vocabulary_items", ["user_id"])
    op.create_table(
        "vocabulary_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vocabulary_item_id", sa.String(36), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("user_answer", sa.Text(), nullable=True),
        sa.Column("correct", sa.Boolean(), nullable=True),
        sa.Column("user_sentence", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_window", sa.String(40), nullable=False),
        sa.Column("source_event_id", sa.String(36), nullable=False),
        sa.Column("policy_version", sa.String(30), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "vocabulary_item_id"],
            ["vocabulary_items.user_id", "vocabulary_items.id"],
            name="fk_vocabulary_attempt_user_item",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "source_event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_vocabulary_attempt_user_event",
        ),
        sa.UniqueConstraint(
            "user_id",
            "vocabulary_item_id",
            "review_window",
            "policy_version",
            name="uq_vocabulary_attempt_window",
        ),
    )
    op.create_index("ix_vocabulary_attempts_user_id", "vocabulary_attempts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_vocabulary_attempts_user_id", table_name="vocabulary_attempts")
    op.drop_table("vocabulary_attempts")
    op.drop_index("ix_vocabulary_items_user_id", table_name="vocabulary_items")
    op.drop_table("vocabulary_items")
