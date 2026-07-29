"""Add deterministic performance snapshots and periodic reviews.

Revision ID: 0015_performance_reviews
Revises: 0014_notification_delivery_receipts
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_performance_reviews"
down_revision: str | None = "0014_notification_delivery_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "performance_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("period_type", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Text(), nullable=False),
        sa.Column("period_end", sa.Text(), nullable=False),
        sa.Column("start_value_minor", sa.Integer(), nullable=False),
        sa.Column("end_value_minor", sa.Integer(), nullable=False),
        sa.Column("net_external_flow_minor", sa.Integer(), nullable=False),
        sa.Column("modified_dietz_bps", sa.Integer()),
        sa.Column("xirr_bps", sa.Integer()),
        sa.Column("twr_bps", sa.Integer()),
        sa.Column("benchmark_return_bps", sa.Integer()),
        sa.Column("excess_return_bps", sa.Integer()),
        sa.Column("attribution_json", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("calculation_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "period_type IN ('CUSTOM','MONTHLY','QUARTERLY','ANNUAL','SINCE_INCEPTION')",
            name="ck_performance_period_type",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_performance_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint(
            "portfolio_id",
            "period_type",
            "period_start",
            "period_end",
            "facts_hash",
            name="uq_performance_input",
        ),
    )
    op.create_index(
        "idx_performance_portfolio_period",
        "performance_snapshots",
        ["portfolio_id", "period_end", "period_type"],
    )

    op.create_table(
        "periodic_reviews",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("review_type", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Text(), nullable=False),
        sa.Column("period_end", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("performance_snapshot_id", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "review_type IN ('MONTHLY','QUARTERLY','ANNUAL')",
            name="ck_periodic_review_type",
        ),
        sa.CheckConstraint(
            "status IN ('FINALIZED','DATA_BLOCKED')",
            name="ck_periodic_review_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_periodic_review_revision"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(
            ["performance_snapshot_id"],
            ["performance_snapshots.id"],
        ),
        sa.UniqueConstraint(
            "portfolio_id",
            "review_type",
            "period_start",
            "period_end",
            "revision",
            name="uq_periodic_review_revision",
        ),
        sa.UniqueConstraint("facts_hash", name="uq_periodic_review_facts"),
    )
    op.create_index(
        "idx_periodic_reviews_portfolio",
        "periodic_reviews",
        ["portfolio_id", "period_end", "review_type"],
    )

    op.create_table(
        "review_action_items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("review_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('INFO','WARNING','HIGH')",
            name="ck_review_action_severity",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','RESOLVED')",
            name="ck_review_action_status",
        ),
        sa.ForeignKeyConstraint(["review_id"], ["periodic_reviews.id"]),
        sa.UniqueConstraint("review_id", "code", name="uq_review_action_code"),
    )
    op.create_index(
        "idx_review_actions_status",
        "review_action_items",
        ["status", "severity"],
    )


def downgrade() -> None:
    op.drop_index("idx_review_actions_status", table_name="review_action_items")
    op.drop_table("review_action_items")
    op.drop_index("idx_periodic_reviews_portfolio", table_name="periodic_reviews")
    op.drop_table("periodic_reviews")
    op.drop_index("idx_performance_portfolio_period", table_name="performance_snapshots")
    op.drop_table("performance_snapshots")
