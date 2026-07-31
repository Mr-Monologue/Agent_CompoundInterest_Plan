"""Add audited research collection runs and review-quality snapshots.

Revision ID: 0022_research_collection_review_quality
Revises: 0021_watchlist_review_cycles
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_research_collection_review_quality"
down_revision: str | None = "0021_watchlist_review_cycles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_collection_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_lineage", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=False),
        sa.Column("execution_status", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("recorded_count", sa.Integer(), nullable=False),
        sa.Column("replayed_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "execution_status IN ('SUCCESS','PARTIAL','FAILED')",
            name="ck_research_collection_execution_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint(
            "manifest_hash",
            name="uq_research_collection_manifest_hash",
        ),
    )
    op.create_index(
        "idx_research_collection_portfolio",
        "research_collection_runs",
        ["portfolio_id", "finished_at", "execution_status"],
    )
    op.create_index(
        "idx_research_collection_connector",
        "research_collection_runs",
        ["connector_key", "source_lineage", "finished_at"],
    )

    op.create_table(
        "research_collection_items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("evidence_date", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("source_content_hash", sa.Text(), nullable=False),
        sa.Column("ingestion_status", sa.Text(), nullable=False),
        sa.Column("evidence_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "ingestion_status IN ('RECORDED','REPLAYED','REJECTED')",
            name="ck_research_collection_item_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_collection_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["market_research_evidence.id"],
        ),
        sa.UniqueConstraint(
            "run_id",
            "ordinal",
            name="uq_research_collection_item_ordinal",
        ),
    )
    op.create_index(
        "idx_research_collection_item_instrument",
        "research_collection_items",
        ["instrument_code", "evidence_type", "evidence_date"],
    )

    op.create_table(
        "review_quality_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("lookback_reviews", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('COMPLETE','PARTIAL','DATA_BLOCKED')",
            name="ck_review_quality_snapshot_status",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_review_quality_snapshot_data_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint(
            "facts_hash",
            name="uq_review_quality_snapshot_facts",
        ),
    )
    op.create_index(
        "idx_review_quality_snapshot_portfolio",
        "review_quality_snapshots",
        ["portfolio_id", "as_of_date", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_review_quality_snapshot_portfolio",
        table_name="review_quality_snapshots",
    )
    op.drop_table("review_quality_snapshots")
    op.drop_index(
        "idx_research_collection_item_instrument",
        table_name="research_collection_items",
    )
    op.drop_table("research_collection_items")
    op.drop_index(
        "idx_research_collection_connector",
        table_name="research_collection_runs",
    )
    op.drop_index(
        "idx_research_collection_portfolio",
        table_name="research_collection_runs",
    )
    op.drop_table("research_collection_runs")
