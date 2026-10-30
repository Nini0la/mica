"""product UI preferences and thread detail fields

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    state_constraint = next(
        (
            item["name"]
            for item in sa.inspect(op.get_bind()).get_check_constraints("threads")
            if "state" in item["sqltext"].lower()
        ),
        None,
    )
    op.add_column(
        "users", sa.Column("morning_time", sa.String(5), nullable=False, server_default="08:00")
    )
    op.add_column(
        "users", sa.Column("evening_time", sa.String(5), nullable=False, server_default="20:00")
    )
    op.add_column(
        "users",
        sa.Column("onboarding_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("threads", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "threads", sa.Column("importance", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("threads", sa.Column("deadline", sa.Date(), nullable=True))
    op.add_column("threads", sa.Column("next_action", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "threads", sa.Column("watch", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    with op.batch_alter_table("threads") as batch:
        if state_constraint:
            batch.drop_constraint(state_constraint, type_="check")
        batch.create_check_constraint(
            "ck_threads_state_m5",
            "state in ('active','exploring','waiting','dormant','completed','abandoned')",
        )
        batch.create_check_constraint("ck_threads_importance", "importance between 0 and 3")


def downgrade() -> None:
    # M4 cannot represent the richer M5 lifecycle. Map every valid M5 state
    # deterministically before restoring its constraint: work still in motion
    # remains active, while completed work becomes dormant (rather than being
    # misrepresented as abandoned). This is intentionally lossy and documented.
    op.execute(
        "UPDATE threads SET state = CASE "
        "WHEN state IN ('exploring', 'waiting') THEN 'active' "
        "WHEN state = 'completed' THEN 'dormant' ELSE state END"
    )
    with op.batch_alter_table("threads") as batch:
        batch.drop_constraint("ck_threads_importance", type_="check")
        batch.drop_constraint("ck_threads_state_m5", type_="check")
        batch.create_check_constraint(
            "threads_state_check", "state in ('active','dormant','abandoned')"
        )
    for column in ("watch", "next_action", "deadline", "importance", "description"):
        op.drop_column("threads", column)
    for column in ("version", "onboarding_complete", "evening_time", "morning_time"):
        op.drop_column("users", column)
