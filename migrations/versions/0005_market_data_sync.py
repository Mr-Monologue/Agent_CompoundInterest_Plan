"""Add market data provider health and synchronization runs.

Revision ID: 0005_market_data_sync
Revises: 0004_market_nav
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_market_data_sync"
down_revision: str | None = "0004_market_nav"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_data_source_health",
        sa.Column("provider_id", sa.Text(), primary_key=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("library_version", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("canary_status", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.Text(), nullable=False),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "canary_status IN ('PASS','FAIL')",
            name="ck_market_source_canary_status",
        ),
    )
    op.create_table(
        "market_sync_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("requested_as_of", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("succeeded_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PASS','PARTIAL','FAIL')",
            name="ck_market_sync_run_status",
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["market_data_source_health.provider_id"]),
    )
    op.create_index(
        "idx_market_sync_runs_latest",
        "market_sync_runs",
        ["provider_id", "completed_at"],
    )
    op.execute(
        "UPDATE schema_meta SET value='2', updated_at='2026-07-22T00:00:00Z' WHERE key='phase'"
    )


def downgrade() -> None:
    op.drop_index("idx_market_sync_runs_latest", table_name="market_sync_runs")
    op.drop_table("market_sync_runs")
    op.drop_table("market_data_source_health")
