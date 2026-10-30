"""Private, response-linked model input snapshots.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assistance_runs", sa.Column("context_trace", sa.JSON(), nullable=True))
    op.add_column("assistance_runs", sa.Column(
        "trace_status", sa.String(40), nullable=False, server_default="not_recorded",
    ))
    op.create_index("ix_assistance_runs_user_message", "assistance_runs", ["user_id", "message_id"])


def downgrade() -> None:
    op.drop_index("ix_assistance_runs_user_message", table_name="assistance_runs")
    op.drop_column("assistance_runs", "trace_status")
    op.drop_column("assistance_runs", "context_trace")
