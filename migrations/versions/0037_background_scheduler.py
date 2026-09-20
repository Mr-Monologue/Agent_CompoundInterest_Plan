"""Persist scheduler ownership, governed cutovers and worker observations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_background_scheduler"
down_revision: str | None = "0036_no_investment_week"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("scheduler_control", "scheduler_change_drafts", "scheduler_worker_snapshots"):
        op.create_table(
            name,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    for name in ("scheduler_worker_snapshots", "scheduler_change_drafts", "scheduler_control"):
        op.drop_table(name)
