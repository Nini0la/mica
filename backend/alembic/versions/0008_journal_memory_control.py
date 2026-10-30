"""journal correction and deletion controls

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("journal_entries", sa.Column("corrected_content", sa.Text(), nullable=True))
    op.add_column(
        "journal_entries", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "journal_entries",
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("journal_entries", "deleted")
    op.drop_column("journal_entries", "version")
    op.drop_column("journal_entries", "corrected_content")