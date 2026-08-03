"""Add governed research collection task runtime and immutable attempts.

Revision ID: 0024_research_collection_runtime
Revises: 0023_research_source_coverage
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_research_collection_runtime"
down_revision: str | None = "0023_research_source_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_collection_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("coverage_snapshot_id", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("eligible_connectors_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("task_hash", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("collection_run_id", sa.Text()),
        sa.Column("followup_snapshot_id", sa.Text()),
        sa.Column("result_reason_code", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING','CLAIMED','COMPLETED','PARTIAL','FAILED','SUPERSEDED')",
            name="ck_research_collection_task_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(
            ["coverage_snapshot_id"], ["research_coverage_snapshots.id"]
        ),
        sa.ForeignKeyConstraint(["collection_run_id"], ["research_collection_runs.id"]),
        sa.ForeignKeyConstraint(
            ["followup_snapshot_id"], ["research_coverage_snapshots.id"]
        ),
        sa.UniqueConstraint("task_hash", name="uq_research_collection_task_hash"),
    )
    op.create_index(
        "idx_research_collection_task_queue",
        "research_collection_tasks",
        ["portfolio_id", "status", "created_at"],
    )
    op.create_index(
        "idx_research_collection_task_snapshot",
        "research_collection_tasks",
        ["coverage_snapshot_id", "instrument_code", "evidence_type"],
    )

    op.create_table(
        "research_collection_attempts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("executor_ref", sa.Text(), nullable=False),
        sa.Column("lease_token_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.Text(), nullable=False),
        sa.Column("lease_expires_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text()),
        sa.Column("collection_run_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.CheckConstraint(
            "status IN ('ACTIVE','EXPIRED','SUCCEEDED','PARTIAL','FAILED')",
            name="ck_research_collection_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["research_collection_tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["collection_run_id"], ["research_collection_runs.id"]),
        sa.UniqueConstraint(
            "task_id", "attempt_number", name="uq_research_collection_task_attempt"
        ),
    )
    op.create_index(
        "idx_research_collection_attempt_active",
        "research_collection_attempts",
        ["task_id", "status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_research_collection_attempt_active",
        table_name="research_collection_attempts",
    )
    op.drop_table("research_collection_attempts")
    op.drop_index(
        "idx_research_collection_task_snapshot",
        table_name="research_collection_tasks",
    )
    op.drop_index(
        "idx_research_collection_task_queue",
        table_name="research_collection_tasks",
    )
    op.drop_table("research_collection_tasks")
