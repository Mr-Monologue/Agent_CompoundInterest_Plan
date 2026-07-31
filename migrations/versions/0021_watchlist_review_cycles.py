"""Add deterministic research-watchlist review cycles.

Revision ID: 0021_watchlist_review_cycles
Revises: 0020_watchlist_research_outcomes
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_watchlist_review_cycles"
down_revision: str | None = "0020_watchlist_research_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("research_watchlist_entries") as batch_op:
        batch_op.add_column(sa.Column("observation_started_at", sa.Text()))
        batch_op.add_column(sa.Column("last_reviewed_at", sa.Text()))

    op.execute(
        """
        UPDATE research_watchlist_entries
        SET observation_started_at = (
            SELECT MIN(t.confirmed_at)
            FROM research_watchlist_transitions t
            WHERE t.entry_id = research_watchlist_entries.id
              AND t.new_state = 'OBSERVING'
        )
        """
    )
    op.execute(
        """
        UPDATE research_watchlist_entries
        SET last_reviewed_at = (
            SELECT MAX(t.confirmed_at)
            FROM research_watchlist_transitions t
            WHERE t.entry_id = research_watchlist_entries.id
              AND t.previous_state = 'REVIEW_DUE'
              AND t.new_state <> 'REVIEW_DUE'
        )
        """
    )

    op.create_table(
        "research_watchlist_review_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("due_count", sa.Integer(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('COMPLETED','REVIEW_REQUIRED')",
            name="ck_watchlist_review_snapshot_status",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_watchlist_review_snapshot_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint(
            "facts_hash",
            name="uq_watchlist_review_snapshot_facts",
        ),
    )
    op.create_index(
        "idx_watchlist_review_snapshot_portfolio",
        "research_watchlist_review_snapshots",
        ["portfolio_id", "as_of_date", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_watchlist_review_snapshot_portfolio",
        table_name="research_watchlist_review_snapshots",
    )
    op.drop_table("research_watchlist_review_snapshots")
    with op.batch_alter_table("research_watchlist_entries") as batch_op:
        batch_op.drop_column("last_reviewed_at")
        batch_op.drop_column("observation_started_at")
