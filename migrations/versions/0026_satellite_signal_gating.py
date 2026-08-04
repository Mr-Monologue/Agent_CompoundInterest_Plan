"""Add governed satellite valuation-signal policies and snapshots.

Revision ID: 0026_satellite_signal_gating
Revises: 0025_alert_recovery_resolution
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_satellite_signal_gating"
down_revision: str | None = "0025_alert_recovery_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "satellite_signal_policy_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_by", sa.Text()),
        sa.Column("committed_policy_id", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED','CANCELLED')",
            name="ck_satellite_signal_policy_draft_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
    )
    op.create_index(
        "idx_satellite_signal_policy_draft_portfolio",
        "satellite_signal_policy_drafts",
        ["portfolio_id", "status", "created_at"],
    )

    op.create_table(
        "satellite_signal_policies",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("entry_max_percentile_bps", sa.Integer(), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column("minimum_sample_count", sa.Integer(), nullable=False),
        sa.Column("maximum_observation_age_days", sa.Integer(), nullable=False),
        sa.Column("allow_warning_data", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_satellite_signal_policy_version"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','SUPERSEDED')",
            name="ck_satellite_signal_policy_status",
        ),
        sa.CheckConstraint("metric IN ('PE','PB')", name="ck_satellite_signal_policy_metric"),
        sa.CheckConstraint(
            "entry_max_percentile_bps BETWEEN 0 AND 10000",
            name="ck_satellite_signal_policy_percentile",
        ),
        sa.CheckConstraint("lookback_days >= 30", name="ck_satellite_signal_policy_lookback"),
        sa.CheckConstraint(
            "minimum_sample_count > 0",
            name="ck_satellite_signal_policy_sample_count",
        ),
        sa.CheckConstraint(
            "maximum_observation_age_days >= 0",
            name="ck_satellite_signal_policy_observation_age",
        ),
        sa.CheckConstraint(
            "allow_warning_data IN (0,1)",
            name="ck_satellite_signal_policy_warning_data",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("portfolio_id", "version", name="uq_satellite_signal_policy_version"),
        sa.UniqueConstraint("content_hash", name="uq_satellite_signal_policy_content"),
    )
    op.create_index(
        "idx_satellite_signal_policy_active",
        "satellite_signal_policies",
        ["portfolio_id", "status", "version"],
    )

    op.create_table(
        "satellite_signal_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("strategy_assignment_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("benchmark_instrument_id", sa.Text()),
        sa.Column("valuation_snapshot_id", sa.Text()),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("percentile_bps", sa.Integer()),
        sa.Column("sample_count", sa.Integer()),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('OPEN','CLOSED','BLOCKED','NOT_AUTHORIZED')",
            name="ck_satellite_signal_snapshot_state",
        ),
        sa.CheckConstraint(
            "percentile_bps IS NULL OR percentile_bps BETWEEN 0 AND 10000",
            name="ck_satellite_signal_snapshot_percentile",
        ),
        sa.CheckConstraint(
            "sample_count IS NULL OR sample_count >= 0",
            name="ck_satellite_signal_snapshot_sample_count",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR','NOT_AVAILABLE')",
            name="ck_satellite_signal_snapshot_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["satellite_signal_policies.id"]),
        sa.ForeignKeyConstraint(["strategy_assignment_id"], ["strategy_assignments.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(["benchmark_instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(["valuation_snapshot_id"], ["valuation_snapshots.id"]),
        sa.UniqueConstraint("facts_hash", name="uq_satellite_signal_snapshot_facts"),
    )
    op.create_index(
        "idx_satellite_signal_snapshot_latest",
        "satellite_signal_snapshots",
        ["portfolio_id", "policy_id", "as_of_date", "instrument_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_satellite_signal_snapshot_latest",
        table_name="satellite_signal_snapshots",
    )
    op.drop_table("satellite_signal_snapshots")
    op.drop_index(
        "idx_satellite_signal_policy_active",
        table_name="satellite_signal_policies",
    )
    op.drop_table("satellite_signal_policies")
    op.drop_index(
        "idx_satellite_signal_policy_draft_portfolio",
        table_name="satellite_signal_policy_drafts",
    )
    op.drop_table("satellite_signal_policy_drafts")
