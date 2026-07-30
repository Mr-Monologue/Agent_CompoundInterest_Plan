"""Add sourced market discovery and governed review action decisions.

Revision ID: 0018_review_market_discovery
Revises: 0017_capital_data_resilience
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_review_market_discovery"
down_revision: str | None = "0017_capital_data_resilience"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_research_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("evidence_date", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("source_lineage", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "evidence_type IN ('FUND_PROFILE','HOLDINGS','MANAGER','FEES',"
            "'BENCHMARK','MARKET_REGIME','OTHER')",
            name="ck_market_research_evidence_type",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint("facts_hash", name="uq_market_research_evidence_facts"),
    )
    op.create_index(
        "idx_market_research_evidence_instrument",
        "market_research_evidence",
        ["instrument_id", "evidence_date", "evidence_type"],
    )

    op.create_table(
        "market_discovery_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column("instrument_codes_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("lookback_days BETWEEN 30 AND 730", name="ck_discovery_lookback"),
        sa.CheckConstraint(
            "status IN ('COMPLETED','DEGRADED','DATA_BLOCKED')",
            name="ck_market_discovery_status",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_market_discovery_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("facts_hash", name="uq_market_discovery_facts"),
    )
    op.create_index(
        "idx_market_discovery_portfolio",
        "market_discovery_runs",
        ["portfolio_id", "as_of_date"],
    )

    op.create_table(
        "market_discovery_items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("latest_nav_date", sa.Text()),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("return_20d_bps", sa.Integer()),
        sa.Column("return_60d_bps", sa.Integer()),
        sa.Column("return_120d_bps", sa.Integer()),
        sa.Column("max_drawdown_bps", sa.Integer()),
        sa.Column("annualized_volatility_bps", sa.Integer()),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('OBSERVE','REVIEW','DATA_BLOCKED')",
            name="ck_market_discovery_item_state",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["market_discovery_runs.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint("run_id", "instrument_id", name="uq_market_discovery_item"),
    )
    op.create_index(
        "idx_market_discovery_items_state",
        "market_discovery_items",
        ["run_id", "state"],
    )

    op.create_table(
        "review_action_decision_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("action_item_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confirmation_token_digest", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.CheckConstraint(
            "decision IN ('ACKNOWLEDGE','RESOLVE')",
            name="ck_review_action_decision",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_review_action_decision_draft_status",
        ),
        sa.ForeignKeyConstraint(["action_item_id"], ["review_action_items.id"]),
    )
    op.create_index(
        "idx_review_action_decision_drafts",
        "review_action_decision_drafts",
        ["action_item_id", "status"],
    )

    op.create_table(
        "review_action_decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("draft_id", sa.Text(), nullable=False),
        sa.Column("action_item_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_status", sa.Text(), nullable=False),
        sa.Column("new_status", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["review_action_decision_drafts.id"]),
        sa.ForeignKeyConstraint(["action_item_id"], ["review_action_items.id"]),
        sa.UniqueConstraint("draft_id", name="uq_review_action_decision_draft"),
    )


def downgrade() -> None:
    op.drop_table("review_action_decisions")
    op.drop_index(
        "idx_review_action_decision_drafts",
        table_name="review_action_decision_drafts",
    )
    op.drop_table("review_action_decision_drafts")
    op.drop_index("idx_market_discovery_items_state", table_name="market_discovery_items")
    op.drop_table("market_discovery_items")
    op.drop_index("idx_market_discovery_portfolio", table_name="market_discovery_runs")
    op.drop_table("market_discovery_runs")
    op.drop_index(
        "idx_market_research_evidence_instrument",
        table_name="market_research_evidence",
    )
    op.drop_table("market_research_evidence")
