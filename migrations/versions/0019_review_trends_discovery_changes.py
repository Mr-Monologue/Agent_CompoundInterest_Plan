"""Add immutable discovery changes and cross-period review trends.

Revision ID: 0019_review_trends_discovery_changes
Revises: 0018_review_market_discovery
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_review_trends_discovery_changes"
down_revision: str | None = "0018_review_market_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_discovery_changes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("previous_run_id", sa.Text()),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("previous_state", sa.Text()),
        sa.Column("current_state", sa.Text(), nullable=False),
        sa.Column("attention_required", sa.Boolean(), nullable=False),
        sa.Column("added_flags_json", sa.Text(), nullable=False),
        sa.Column("removed_flags_json", sa.Text(), nullable=False),
        sa.Column("metric_deltas_json", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "change_type IN ('INITIAL','UNCHANGED','CHANGED')",
            name="ck_market_discovery_change_type",
        ),
        sa.CheckConstraint(
            "current_state IN ('OBSERVE','REVIEW','DATA_BLOCKED')",
            name="ck_market_discovery_change_current_state",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["market_discovery_runs.id"]),
        sa.ForeignKeyConstraint(["previous_run_id"], ["market_discovery_runs.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint("run_id", "instrument_id", name="uq_market_discovery_change"),
    )
    op.create_index(
        "idx_market_discovery_changes_attention",
        "market_discovery_changes",
        ["run_id", "attention_required", "change_type"],
    )

    op.create_table(
        "review_trend_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("review_type", sa.Text(), nullable=False),
        sa.Column("lookback_reviews", sa.Integer(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "review_type IN ('ALL','MONTHLY','QUARTERLY','ANNUAL')",
            name="ck_review_trend_type",
        ),
        sa.CheckConstraint(
            "lookback_reviews BETWEEN 1 AND 120",
            name="ck_review_trend_lookback",
        ),
        sa.CheckConstraint(
            "status IN ('COMPLETED','DATA_BLOCKED')",
            name="ck_review_trend_status",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_review_trend_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("facts_hash", name="uq_review_trend_facts"),
    )
    op.create_index(
        "idx_review_trend_portfolio",
        "review_trend_snapshots",
        ["portfolio_id", "as_of_date", "review_type"],
    )


def downgrade() -> None:
    op.drop_index("idx_review_trend_portfolio", table_name="review_trend_snapshots")
    op.drop_table("review_trend_snapshots")
    op.drop_index(
        "idx_market_discovery_changes_attention",
        table_name="market_discovery_changes",
    )
    op.drop_table("market_discovery_changes")
