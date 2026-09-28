"""D1 append-only review snapshots, tasks and handling history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_holding_review"
down_revision: str | None = "0041_thesis_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("holding_review_snapshots", "holding_review_tasks", "holding_review_events"):
        op.create_table(
            name,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("portfolio_id", sa.Text(), sa.ForeignKey("portfolios.id"), nullable=False),
            sa.Column("account_id", sa.Text(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    for name in ("holding_review_events", "holding_review_tasks", "holding_review_snapshots"):
        op.drop_table(name)
