"""Research-only versioned benchmarks; never used by investment risk actions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_research_benchmarks"
down_revision: str | None = "0039_research_notebook"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_benchmark_mappings",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("portfolio_id", "instrument_code", "version"),
    )
    op.create_table(
        "research_benchmark_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("research_benchmark_runs")
    op.drop_table("research_benchmark_mappings")
