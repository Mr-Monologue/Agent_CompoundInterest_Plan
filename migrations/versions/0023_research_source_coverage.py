"""Add governed research-source configs and evidence-coverage snapshots.

Revision ID: 0023_research_source_coverage
Revises: 0022_research_collection_review_quality
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_research_source_coverage"
down_revision: str | None = "0022_research_collection_review_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_source_config_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("evidence_types_json", sa.Text(), nullable=False),
        sa.Column("source_lineages_json", sa.Text(), nullable=False),
        sa.Column("credential_ref", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confirmation_token_digest", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_research_source_config_draft_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
    )
    op.create_index(
        "idx_research_source_config_draft_portfolio",
        "research_source_config_drafts",
        ["portfolio_id", "connector_key", "status"],
    )

    op.create_table(
        "research_source_configs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("draft_id", sa.Text(), nullable=False),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("evidence_types_json", sa.Text(), nullable=False),
        sa.Column("source_lineages_json", sa.Text(), nullable=False),
        sa.Column("credential_ref", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["research_source_config_drafts.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("draft_id", name="uq_research_source_config_draft"),
        sa.UniqueConstraint(
            "portfolio_id",
            "connector_key",
            "version",
            name="uq_research_source_config_version",
        ),
    )
    op.create_index(
        "idx_research_source_config_current",
        "research_source_configs",
        ["portfolio_id", "connector_key", "is_current"],
    )

    op.create_table(
        "research_coverage_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("max_age_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('COMPLETE','PARTIAL','DATA_BLOCKED')",
            name="ck_research_coverage_status",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_research_coverage_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("facts_hash", name="uq_research_coverage_facts"),
    )
    op.create_index(
        "idx_research_coverage_portfolio",
        "research_coverage_snapshots",
        ["portfolio_id", "as_of_date", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_research_coverage_portfolio", table_name="research_coverage_snapshots")
    op.drop_table("research_coverage_snapshots")
    op.drop_index("idx_research_source_config_current", table_name="research_source_configs")
    op.drop_table("research_source_configs")
    op.drop_index(
        "idx_research_source_config_draft_portfolio",
        table_name="research_source_config_drafts",
    )
    op.drop_table("research_source_config_drafts")
