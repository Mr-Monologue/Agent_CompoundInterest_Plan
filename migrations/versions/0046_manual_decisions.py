"""Confirmable manual decisions; retain all existing research and financial facts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_manual_decisions"
down_revision: str | None = "0045_shadow_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_investment_decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("plan_id", sa.Text(), sa.ForeignKey("investment_plans.id"), nullable=False),
        sa.Column("request_key", sa.Text(), unique=True, nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("manual_investment_decisions")
