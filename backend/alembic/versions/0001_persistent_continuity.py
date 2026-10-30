"""persistent continuity foundation"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
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
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True)

    create_owned_table(
        "conversation_sessions",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_owned_table(
        "messages",
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=20), nullable=False),
        constraints=(
            sa.ForeignKeyConstraint(["conversation_id"], ["conversation_sessions.id"]),
            sa.CheckConstraint("role in ('user','assistant','system')"),
        ),
    )
    create_owned_table(
        "threads",
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("derivation", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        constraints=(sa.CheckConstraint("state in ('active','dormant','abandoned')"),),
    )
    create_owned_table(
        "commitments",
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("deadline_hint", sa.Text(), nullable=True),
        sa.Column("effort_minutes", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("derivation", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        constraints=(
            sa.CheckConstraint("state in ('open','complete','abandoned','dormant')"),
        ),
    )
    create_owned_table(
        "episodes",
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("derivation", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        constraints=(
            sa.CheckConstraint("actor in ('user','assistant','joint','discussed_only')"),
        ),
    )
    create_owned_table(
        "agent_events",
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        constraints=(
            sa.UniqueConstraint("user_id", "external_id", name="uq_agent_events_user_external"),
        ),
    )
    create_owned_table(
        "provenance_links",
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("derivation", sa.String(length=100), nullable=False),
        sa.Column("metadata_only", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_provenance_links_target_id", "provenance_links", ["target_id"])
    create_owned_table(
        "skill_runs",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("skill_name", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        constraints=(sa.ForeignKeyConstraint(["event_id"], ["agent_events.id"]),),
    )
    op.create_table(
        "episode_threads",
        sa.Column("episode_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("episode_id", "thread_id"),
    )


def downgrade() -> None:
    op.drop_table("episode_threads")
    op.drop_index("ix_skill_runs_user_id", table_name="skill_runs")
    op.drop_table("skill_runs")
    op.drop_index("ix_provenance_links_target_id", table_name="provenance_links")
    op.drop_index("ix_provenance_links_user_id", table_name="provenance_links")
    op.drop_table("provenance_links")
    op.drop_index("ix_agent_events_user_id", table_name="agent_events")
    op.drop_table("agent_events")
    op.drop_index("ix_episodes_user_id", table_name="episodes")
    op.drop_table("episodes")
    op.drop_index("ix_commitments_user_id", table_name="commitments")
    op.drop_table("commitments")
    op.drop_index("ix_threads_user_id", table_name="threads")
    op.drop_table("threads")
    op.drop_index("ix_messages_user_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversation_sessions_user_id", table_name="conversation_sessions")
    op.drop_table("conversation_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
