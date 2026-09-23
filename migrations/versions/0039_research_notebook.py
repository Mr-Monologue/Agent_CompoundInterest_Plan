"""Append-only advisory thesis versions and decision journal."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_research_notebook"
down_revision: str | None = "0038_execution_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_quota_observations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Text(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "research_case_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("portfolio_id", "instrument_code", "version"),
    )
    op.create_table(
        "research_decision_journal",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("execution_quota_observations")
    op.drop_table("research_decision_journal")
    op.drop_table("research_case_versions")
