"""Append-only research governance; no investment or ledger change."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_thesis_governance"
down_revision: str | None = "0040_research_benchmarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("research_thesis_actions", "research_thesis_observations"):
        op.create_table(
            name,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("portfolio_id", sa.Text(), sa.ForeignKey("portfolios.id"), nullable=False),
            sa.Column("instrument_code", sa.Text(), nullable=False),
            sa.Column("request_key", sa.Text(), nullable=False, unique=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("research_thesis_observations")
    op.drop_table("research_thesis_actions")
